"""Private bounded effects and service-local health observations."""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from camera_gateway.config import AuthMode
from camera_gateway.errors import (
    CameraNotFound,
    CameraTimeout,
    Conflict,
    FeatureDisabled,
    InvalidRequest,
)
from camera_gateway.mission_models import Clip, CommandResult, HealthEvent, PtzCommand
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
        self.store.event(camera.id, HealthEvent(observed_at=now(), signal=signal, heuristic=heuristic))
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

    async def maintain(self):
        while True:
            await asyncio.sleep(30)
            self.maintenance()
