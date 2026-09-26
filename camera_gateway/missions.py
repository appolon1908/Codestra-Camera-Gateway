"""Private bounded effects and service-local health observations."""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from camera_gateway import SERVICE_NAME, __version__
from camera_gateway.config import AuthMode
from camera_gateway.errors import (
    CameraNotFound,
    CameraTimeout,
    Conflict,
    FeatureDisabled,
    InvalidRequest,
)
from camera_gateway.mission_models import (
    CameraSearchResult,
    Clip,
    ClockDriftHealth,
    CommandResult,
    CompatibilityStatus,
    ConfigurationSnapshot,
    ConnectivityEvidence,
    ContractVersionStatus,
    CoveredInterval,
    CredentialReloadResult,
    CredentialRotationStatus,
    DeviceInventory,
    DiagnosticsSnapshot,
    EventCursorPage,
    HealthEvent,
    HealthSloStatus,
    MaintenanceState,
    NormalizedCameraEvent,
    PreEventManifest,
    PtzCommand,
    RetentionPolicyStatus,
    RollingBufferStatus,
    SecretReferenceHealth,
    StreamQualityStatus,
)
from camera_gateway.mission_store import MissionStore


def now():
    return datetime.now(UTC)


class Missions:
    def __init__(self, service):
        self.service = service
        self.store = MissionStore(service.store)
        self.root = service.settings.database_path.parent / "clips"
        self.clip_lock = asyncio.Lock()
        self.ptz_locks = set()
        self.frames = {}
        self.stale = set()

    def require_effects(self, enabled):
        if not enabled or self.service.settings.auth_mode is not AuthMode.token:
            raise FeatureDisabled("Private effect requires feature enablement and service authentication")

    def camera(self, tenant, camera_id, *, stop=False):
        camera = self.service.store.get(tenant, camera_id)
        if not stop:
            self.service._require_enabled(camera)
        if not camera.onvif.enabled:
            raise FeatureDisabled("ONVIF is not enabled for this camera")
        return camera

    async def ptz_capability(self, tenant, camera_id):
        camera = self.camera(tenant, camera_id)

        async def run():
            async with self.service._onvif_client(camera) as client:
                await client.sync_clock()
                capability, _ = await client.ptz_discover()
                return capability

        return await self.service._observe("ptz_discover", camera, run)

    async def ptz(self, tenant, camera_id, command):
        self.require_effects(self.service.settings.enable_ptz)
        camera = self.camera(tenant, camera_id, stop=command.action == "stop")
        key = (tenant, camera_id)
        if command.action != "stop" and key in self.ptz_locks:
            raise Conflict("PTZ command already active")
        # No awaits between checking and acquiring this single-worker guard.
        if command.action != "stop":
            self.ptz_locks.add(key)
        try:
            previous = self.store.reserve(
                tenant, camera_id, command.command_ref, "ptz:" + command.model_dump_json()
            )
            if previous:
                return CommandResult.model_validate(previous)

            async def run():
                async with self.service._onvif_client(camera) as client:
                    await client.sync_clock()
                    cap, path = await client.ptz_discover()
                    if not cap.supported or path is None:
                        raise FeatureDisabled("Camera does not advertise usable PTZ")
                    if command.action == "absolute" and (
                        (command.pan is not None and not cap.absolute_pan_tilt)
                        or (command.zoom is not None and not cap.absolute_zoom)
                    ):
                        raise FeatureDisabled("Requested normalized PTZ space is not supported")
                    if command.action == "preset" and command.preset not in cap.presets:
                        raise InvalidRequest("Preset is not advertised by this camera")
                    if command.action == "stop":
                        await client.ptz_action(path, cap.profile_token, command)
                        return
                    started = asyncio.get_running_loop().time()
                    try:
                        async with asyncio.timeout(command.timeout_s):
                            await client.ptz_action(path, cap.profile_token, command)
                        remaining = command.timeout_s - (asyncio.get_running_loop().time() - started)
                        await asyncio.sleep(max(0, remaining))
                    except TimeoutError:
                        raise CameraTimeout("PTZ move acknowledgement timed out; reconcile camera") from None
                    finally:
                        # Shield against caller cancellation. Stop has its own bounded deadline.
                        async def stop():
                            try:
                                async with asyncio.timeout(self.service.settings.onvif_timeout_s + 1):
                                    await client.ptz_action(
                                        path,
                                        cap.profile_token,
                                        PtzCommand(command_ref=command.command_ref, action="stop"),
                                    )
                            except TimeoutError:
                                raise CameraTimeout(
                                    "PTZ Stop acknowledgement timed out; reconcile camera"
                                ) from None

                        task = asyncio.create_task(stop())
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await task
                            raise

            await self.service._observe("ptz_command", camera, run)
            result = CommandResult(command_ref=command.command_ref)
            self.store.complete(tenant, camera_id, command.command_ref, result)
            return result
        finally:
            if command.action != "stop":
                self.ptz_locks.discard(key)

    async def capture(self, tenant, camera_id, command):
        settings = self.service.settings
        self.require_effects(settings.enable_clip_capture)
        camera = self.service.store.get(tenant, camera_id)
        self.service._require_enabled(camera)
        if command.duration_s > settings.clip_max_duration_s or command.max_bytes > settings.clip_max_bytes:
            raise InvalidRequest("Clip request exceeds configured limits")
        async with self.service._slot(self.service._media_slots), self.clip_lock:
            self.cleanup()
            previous = self.store.reserve(
                tenant, camera_id, command.command_ref, "clip:" + command.model_dump_json()
            )
            if previous:
                return self.clip(tenant, camera_id, previous["id"])
            with self.store.lock:
                count = self.store.conn.execute("SELECT count(*) FROM camera_clips").fetchone()[0]
            if count >= settings.clip_max_files:
                raise Conflict("Private clip storage is full")
            started = now()

            async def run():
                return await self.service.rtsp.clip(
                    await self.service._rtsp_target(camera), command.duration_s, command.max_bytes
                )

            data = await self.service._observe("clip_capture", camera, run)
            completed = now()
            clip = Clip(
                id="clip_" + uuid.uuid4().hex,
                camera_id=camera_id,
                command_ref=command.command_ref,
                started_at=started,
                completed_at=completed,
                expires_at=completed + timedelta(seconds=settings.clip_retention_s),
                requested_duration_s=command.duration_s,
                size_bytes=len(data),
            )
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.root / (clip.id + ".ts")
            try:
                # Share cleanup's lock so it cannot remove an in-progress file.
                with self.store.lock:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    self.store.conn.execute(
                        "INSERT INTO camera_clips VALUES (?, ?, ?, ?)",
                        (clip.id, tenant, camera_id, clip.model_dump_json()),
                    )
                self.store.complete(tenant, camera_id, command.command_ref, clip)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return clip

    def clip(self, tenant, camera_id, clip_id):
        self.service.store.get(tenant, camera_id)
        self.cleanup()
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT body FROM camera_clips WHERE id=? AND tenant=? AND camera_id=?",
                (clip_id, tenant, camera_id),
            ).fetchone()
        if row is None:
            raise CameraNotFound("Clip not found or expired")
        return Clip.model_validate_json(row["body"])

    def clips(self, tenant, camera_id, limit, offset):
        self.service.store.get(tenant, camera_id)
        self.cleanup()
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT body FROM camera_clips WHERE tenant=? AND camera_id=? ORDER BY id LIMIT ? OFFSET ?",
                (tenant, camera_id, limit, offset),
            ).fetchall()
        return [Clip.model_validate_json(r["body"]) for r in rows]

    def delete_clip(self, tenant, camera_id, clip_id):
        self.clip(tenant, camera_id, clip_id)
        with self.store.lock:
            (self.root / (clip_id + ".ts")).unlink(missing_ok=True)
            self.store.conn.execute("DELETE FROM camera_clips WHERE id=?", (clip_id,))

    def cleanup(self):
        # Called at startup, periodically, and before reads/capture. Files are never served by HTTP.
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT c.id, c.body, r.id AS registered FROM camera_clips c "
                "LEFT JOIN cameras r ON c.camera_id=r.id"
            ).fetchall()
            live = set()
            for row in rows:
                clip = Clip.model_validate_json(row["body"])
                path = self.root / (clip.id + ".ts")
                if clip.expires_at <= now() or row["registered"] is None or not path.is_file():
                    path.unlink(missing_ok=True)
                    self.store.conn.execute("DELETE FROM camera_clips WHERE id=?", (clip.id,))
                else:
                    live.add(path.name)
            if self.root.exists():
                for path in self.root.glob("clip_*.ts"):
                    if path.name not in live:
                        path.unlink(missing_ok=True)

    def signal(self, camera, signal, heuristic=False):
        observed = now()
        self.store.event(camera.id, HealthEvent(observed_at=observed, signal=signal, heuristic=heuristic))
        if signal in {"scene_change_heuristic", "repeated_frame_heuristic"}:
            kind = "scene_change"
            source = "frame_delta"
        else:
            kind = "health"
            source = "gateway_health"
        self.store.normalized_event(
            camera.tenant_id,
            camera.id,
            NormalizedCameraEvent(
                id=0,
                camera_id=camera.id,
                observed_at=observed,
                kind=kind,
                source=source,
                heuristic=heuristic,
                confidence_kind="heuristic" if heuristic else "observed",
                signal=signal,
            ),
        )
        self.service.metrics.health_signals.labels(signal=signal).inc()

    def observe_health(self, camera, health):
        signal = (
            "protocol_error" if health.last_error_code == "camera_protocol_error" else health.status.value
        )
        if signal != "unknown":
            self.signal(camera, signal)

    def observe_frame(self, camera, snap):
        fingerprint = hashlib.sha256(snap.jpeg).digest()
        previous = self.frames.get(camera.id)
        if previous:
            digest, size, repeats = previous
            repeats = repeats + 1 if digest == fingerprint else 0
            if repeats == 3:
                self.signal(camera, "repeated_frame_heuristic", True)
            # Compressed-size change is a coarse scene-change proxy, never a CV verdict.
            if abs(len(snap.jpeg) - size) / max(size, 1) > 0.5:
                self.signal(camera, "scene_change_heuristic", True)
        else:
            repeats = 0
        self.frames[camera.id] = (fingerprint, len(snap.jpeg), repeats)
        if camera.id in self.stale:
            self.stale.remove(camera.id)
            self.signal(camera, "frame_recovered")

    def check_stale(self, camera):
        frame = camera.health.last_frame_at
        if camera.enabled and frame and (now() - frame).total_seconds() > self.service.settings.frame_stale_s:
            if camera.id not in self.stale:
                self.stale.add(camera.id)
                self.signal(camera, "frame_stale")

    def maintenance(self):
        self.cleanup()
        with self.store.lock:
            rows = self.store.conn.execute("SELECT tenant_id, id FROM cameras").fetchall()
        registered = {r["id"] for r in rows}
        self.frames = {k: v for k, v in self.frames.items() if k in registered}
        self.stale.intersection_update(registered)
        for row in rows:
            try:
                self.check_stale(self.service.store.get(row["tenant_id"], row["id"]))
            except CameraNotFound:
                continue  # Concurrent registry deletion is harmless.

    def buffer_status(self, tenant, camera_id):
        totals = self.store.rolling_totals(tenant, camera_id)

        def parse(value):
            return datetime.fromisoformat(value) if value else None

        return RollingBufferStatus(
            camera_id=camera_id,
            enabled=self.service.settings.enable_rolling_buffer,
            stored_bytes=int(totals["bytes"]),
            reserved_bytes=0,
            segment_count=int(totals["n"]),
            oldest_observed_at=parse(totals["oldest"]),
            newest_observed_at=parse(totals["newest"]),
            max_bytes=self.service.settings.rolling_buffer_max_bytes,
            retention_s=self.service.settings.rolling_buffer_retention_s,
            segment_duration_s=self.service.settings.rolling_buffer_segment_duration_s,
        )

    def pre_event(self, tenant, camera_id, request):
        self.service.store.get(tenant, camera_id)
        previous = self.store.manifest_by_command(tenant, camera_id, request.command_ref)
        if previous is not None:
            return previous
        if request.max_bytes > self.service.settings.rolling_buffer_max_bytes:
            raise InvalidRequest("Pre-event byte budget exceeds configured rolling-buffer quota")
        start = request.event_at - timedelta(seconds=request.pre_event_s)
        rows = self.store.rolling_segments(tenant, camera_id, start, request.event_at)
        selected = []
        used = 0
        covered = []
        for row in rows:
            size = int(row["size_bytes"])
            if used + size > request.max_bytes:
                break
            used += size
            selected.append(row["id"])
            covered.append(
                CoveredInterval(
                    started_at=datetime.fromisoformat(row["started_at"]),
                    ended_at=datetime.fromisoformat(row["ended_at"]),
                )
            )
        gaps = [] if covered else [CoveredInterval(started_at=start, ended_at=request.event_at)]
        manifest = PreEventManifest(
            id="pre_" + uuid.uuid4().hex,
            camera_id=camera_id,
            command_ref=request.command_ref,
            requested_at=now(),
            event_at=request.event_at,
            requested_pre_event_s=request.pre_event_s,
            status="available" if selected else "unavailable",
            segment_ids=selected,
            covered_intervals=covered,
            gaps=gaps,
            expires_at=now() + timedelta(seconds=self.service.settings.rolling_buffer_retention_s),
        )
        self.store.save_manifest(tenant, manifest)
        return manifest

    def events(self, tenant, camera_id, limit, offset):
        return self.store.normalized_events(tenant, camera_id, limit, offset)

    def inventory(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        probe = camera.onvif_probe
        if probe is None:
            return DeviceInventory(camera_id=camera_id)
        age = (now() - probe.probed_at).total_seconds()
        capabilities = ["onvif-device-information"]
        if probe.profiles:
            capabilities.append("media-profiles")
        if any(p.ptz_configuration_token for p in probe.profiles):
            capabilities.append("ptz")
        return DeviceInventory(
            camera_id=camera_id,
            manufacturer=probe.device.manufacturer,
            model=probe.device.model,
            firmware_version=probe.device.firmware_version,
            serial_number=probe.device.serial_number,
            hardware_id=probe.device.hardware_id,
            capabilities=capabilities,
            refreshed_at=probe.probed_at,
            stale=age > self.service.settings.inventory_stale_s,
        )

    def credential_status(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        ref = camera.credentials.password_ref if camera.credentials else None
        fingerprint = hashlib.sha256(str(ref).encode()).hexdigest()[:16] if ref else None
        last = camera.health.last_success_at
        next_due = (
            last + timedelta(seconds=self.service.settings.credential_rotation_interval_s) if last else None
        )
        return CredentialRotationStatus(
            camera_id=camera_id,
            secret_ref_present=ref is not None,
            secret_ref_fingerprint=fingerprint,
            last_successful_auth=last,
            next_rotation_due=next_due,
        )

    async def reload_credentials(self, tenant, camera_id, request):
        self.require_effects(True)
        camera = self.service.store.get(tenant, camera_id)
        if camera.credentials is None:
            raise InvalidRequest("Camera has no credential reference")
        previous = self.store.reserve(
            tenant, camera_id, request.command_ref, "credential-reload:" + request.model_dump_json()
        )
        if previous:
            return CredentialReloadResult.model_validate(previous)
        # Resolves the reference privately and proves authentication against the
        # registered stream. No secret material is returned or logged.
        await self.service.stream_test(tenant, camera_id)
        result = CredentialReloadResult(command_ref=request.command_ref, authenticated_at=now())
        self.store.complete(tenant, camera_id, request.command_ref, result)
        return result

    async def clock_drift(self, tenant, camera_id):
        camera = self.camera(tenant, camera_id)
        observed = now()
        async with self.service._onvif_client(camera) as client:
            await client.sync_clock()
            offset = client.clock_offset_seconds
        return ClockDriftHealth(
            camera_id=camera_id,
            supported=offset is not None,
            observed_at=observed,
            drift_ms=None if offset is None else int(offset * 1000),
            stale=False,
        )

    def configuration_snapshot(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        safe = {
            "camera_ref": camera.camera_ref,
            "name": camera.name,
            "vendor": camera.vendor.value,
            "model": camera.model,
            "host": camera.host,
            "rtsp": camera.rtsp.model_dump(mode="json"),
            "onvif": camera.onvif.model_dump(mode="json"),
            "enabled": camera.enabled,
            "labels": camera.labels,
            "credential_ref_present": camera.credentials is not None,
        }
        digest = hashlib.sha256(
            __import__("json").dumps(safe, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ConfigurationSnapshot(
            camera_id=camera_id,
            observed_at=now(),
            config_sha256=digest,
            enabled=camera.enabled,
            vendor=camera.vendor.value,
            rtsp_transport=camera.rtsp.transport.value,
            onvif_enabled=camera.onvif.enabled,
            credential_ref_present=camera.credentials is not None,
            labels_count=len(camera.labels),
        )

    def connectivity_evidence(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        recent = self.store.history(tenant, camera_id, 20)
        return ConnectivityEvidence(
            camera_id=camera_id,
            observed_at=now(),
            status=camera.health.status.value,
            last_success_at=camera.health.last_success_at,
            last_error_code=camera.health.last_error_code,
            recent_event_count=len(recent),
        )

    def stream_quality(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        media = camera.health.media
        stale = (
            camera.health.last_frame_at is None
            or (now() - camera.health.last_frame_at).total_seconds() > self.service.settings.frame_stale_s
        )
        known = any([media.codec, media.width, media.height, media.fps])
        return StreamQualityStatus(
            camera_id=camera_id,
            observed_at=now(),
            status="known" if known else "unknown",
            codec=media.codec,
            width=media.width,
            height=media.height,
            fps=media.fps,
            last_frame_at=camera.health.last_frame_at,
            stale=stale,
        )

    def maintenance_state(self, tenant, camera_id):
        state = self.store.maintenance_state(tenant, camera_id)
        return MaintenanceState(
            camera_id=camera_id,
            enabled=bool(state.get("enabled", False)),
            reason_ref=state.get("reason_ref"),
            changed_at=datetime.fromisoformat(state["changed_at"]) if state.get("changed_at") else None,
        )

    def update_maintenance_state(self, tenant, camera_id, request):
        previous = self.store.reserve(
            tenant,
            camera_id,
            request.command_ref,
            "maintenance:" + request.model_dump_json(),
        )
        if previous:
            return MaintenanceState.model_validate(previous)
        body = {
            "enabled": request.enabled,
            "reason_ref": request.reason_ref,
            "changed_at": now().isoformat(),
        }
        self.store.save_maintenance_state(tenant, camera_id, body)
        result = MaintenanceState(camera_id=camera_id, **body)
        self.store.complete(tenant, camera_id, request.command_ref, result)
        return result

    def search_inventory(self, tenant, label_key=None, label_value=None, vendor=None, enabled=None):
        items, _ = self.service.store.list(tenant, 200, 0)
        result = []
        for camera in items:
            if label_key is not None:
                if label_key not in camera.labels:
                    continue
                if label_value is not None and camera.labels.get(label_key) != label_value:
                    continue
            if vendor is not None and camera.vendor.value != vendor:
                continue
            if enabled is not None and camera.enabled is not enabled:
                continue
            result.append(camera.id)
        return CameraSearchResult(items=result, total=len(result))

    def compatibility(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        checks = {
            "host_valid": bool(camera.host),
            "stream_resolvable": bool(camera.rtsp.path or camera.onvif.enabled),
            "credential_reference_only": camera.credentials is None
            or str(camera.credentials.password_ref).startswith("openbao://"),
            "middleware_authority": True,
        }
        warnings = []
        if not camera.onvif.enabled:
            warnings.append("onvif_disabled")
        if camera.credentials is None:
            warnings.append("credentials_not_configured")
        return CompatibilityStatus(
            camera_id=camera_id,
            compatible=all(checks.values()),
            checks=checks,
            warnings=warnings,
        )

    def event_cursor(self, tenant, camera_id, limit, offset):
        items = self.events(tenant, camera_id, limit, offset)
        return EventCursorPage(
            items=items,
            next_offset=(offset + len(items)) if len(items) == limit else None,
            limit=limit,
            offset=offset,
        )

    def health_slo(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        observed = now()
        health_fresh = (
            camera.health.last_checked_at is not None
            and (observed - camera.health.last_checked_at).total_seconds()
            <= self.service.settings.frame_stale_s
        )
        frame_fresh = (
            camera.health.last_frame_at is not None
            and (observed - camera.health.last_frame_at).total_seconds()
            <= self.service.settings.frame_stale_s
        )
        if camera.health.last_checked_at is None:
            status = "unknown"
        else:
            status = (
                "healthy"
                if health_fresh and (frame_fresh or camera.health.last_frame_at is None)
                else "degraded"
            )
        return HealthSloStatus(
            camera_id=camera_id,
            observed_at=observed,
            health_fresh=health_fresh,
            frame_fresh=frame_fresh,
            last_success_at=camera.health.last_success_at,
            last_frame_at=camera.health.last_frame_at,
            status=status,
        )

    def secret_reference_health(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        if camera.credentials is None:
            return SecretReferenceHealth(
                camera_id=camera_id,
                configured=False,
                reference_valid=True,
                last_successful_auth=camera.health.last_success_at,
            )
        ref = str(camera.credentials.password_ref)
        return SecretReferenceHealth(
            camera_id=camera_id,
            configured=True,
            scheme="openbao",
            fingerprint=hashlib.sha256(ref.encode()).hexdigest()[:16],
            reference_valid=ref.startswith("openbao://"),
            last_successful_auth=camera.health.last_success_at,
        )

    def retention_policy(self, tenant, camera_id):
        self.service.store.get(tenant, camera_id)
        return RetentionPolicyStatus(
            camera_id=camera_id,
            clip_retention_s=self.service.settings.clip_retention_s,
            rolling_buffer_retention_s=self.service.settings.rolling_buffer_retention_s,
            health_event_limit=200,
            normalized_event_limit=self.service.settings.camera_event_retention_count,
        )

    async def diagnostics(self, tenant, camera_id):
        camera = self.service.store.get(tenant, camera_id)
        inventory = self.inventory(tenant, camera_id)
        maintenance = self.maintenance_state(tenant, camera_id)
        drift = await self.clock_drift(tenant, camera_id) if camera.onvif.enabled else None
        return DiagnosticsSnapshot(
            camera_id=camera_id,
            observed_at=now(),
            health_status=camera.health.status.value,
            inventory_stale=inventory.stale,
            credentials_configured=camera.credentials is not None,
            rolling_buffer_enabled=self.service.settings.enable_rolling_buffer,
            maintenance_enabled=maintenance.enabled,
            clock_drift_ms=None if drift is None else drift.drift_ms,
        )

    def contract_version(self):
        capabilities = {
            "rolling_buffer": self.service.settings.enable_rolling_buffer,
            "events": True,
            "inventory": True,
            "clock_drift": True,
            "missions_11_22": True,
        }
        digest = hashlib.sha256(
            __import__("json").dumps(capabilities, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ContractVersionStatus(
            service=SERVICE_NAME,
            service_version=__version__,
            capability_sha256=digest,
        )

    async def maintain(self):
        while True:
            await asyncio.sleep(30)
            self.maintenance()
