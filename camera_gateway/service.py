"""Camera operations: resolves secret references, drives protocol adapters, records health."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

import httpx

from camera_gateway.adapters.discovery import ws_discover
from camera_gateway.adapters.net import build_rtsp_url
from camera_gateway.adapters.onvif import OnvifClient, OnvifCredentials
from camera_gateway.adapters.rtsp import RtspClient, RtspTarget
from camera_gateway.config import Settings
from camera_gateway.errors import (
    Busy,
    CameraAuthFailed,
    CameraProtocolError,
    CameraTimeout,
    CameraUnreachable,
    Conflict,
    FeatureDisabled,
    GatewayError,
    SecretUnavailable,
)
from camera_gateway.metrics import Metrics
from camera_gateway.missions import Missions
from camera_gateway.models import (
    Camera,
    CameraHealth,
    DiscoveryResult,
    HealthStatus,
    MediaInfo,
    OnvifProbeResult,
    StreamTestResult,
    StreamVariant,
    _validate_rtsp_path,
)
from camera_gateway.secrets import SecretRef, SecretResolver
from camera_gateway.store import CameraStore
from camera_gateway.vendors import default_rtsp_path

log = logging.getLogger(__name__)
T = TypeVar("T")
_LOCAL_REFUSALS = (Busy, FeatureDisabled, Conflict)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _status_for(error: GatewayError) -> HealthStatus:
    if isinstance(error, CameraAuthFailed):
        return HealthStatus.auth_failed
    if isinstance(error, CameraUnreachable | CameraTimeout):
        return HealthStatus.offline
    return HealthStatus.error


@dataclass(frozen=True)
class Snapshot:
    jpeg: bytes
    width: int
    height: int
    captured_at: datetime


class CameraService:
    def __init__(
        self,
        settings: Settings,
        store: CameraStore,
        secrets: SecretResolver,
        rtsp: RtspClient,
        metrics: Metrics,
        onvif_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self.rtsp = rtsp
        self.metrics = metrics
        self._onvif_transport = onvif_transport
        self._media_slots = asyncio.Semaphore(settings.max_concurrent_media_ops)
        self._preview_slots = asyncio.Semaphore(settings.mjpeg_max_sessions)
        self.missions = Missions(self)
        self.refresh_status_gauge()

    # -- helpers -------------------------------------------------------------------

    def refresh_status_gauge(self) -> None:
        for status, count in self.store.global_status_counts().items():
            self.metrics.cameras_by_status.labels(status=status.value).set(count)

    def _password(self, camera: Camera) -> str | None:
        if camera.credentials is None:
            return None
        try:
            value = self.secrets.resolve(SecretRef.parse(camera.credentials.password_ref))
        except SecretUnavailable:
            self.metrics.secret_resolutions.labels(outcome="unavailable").inc()
            raise
        except ValueError:
            self.metrics.secret_resolutions.labels(outcome="invalid_ref").inc()
            raise SecretUnavailable("Camera credential reference is invalid") from None
        self.metrics.secret_resolutions.labels(outcome="resolved").inc()
        return value

    @staticmethod
    def _require_enabled(camera: Camera) -> None:
        if not camera.enabled:
            raise Conflict("Camera is disabled")

    @asynccontextmanager
    async def _slot(self, sem: asyncio.Semaphore) -> AsyncIterator[None]:
        if sem.locked():
            raise Busy()
        async with sem:
            yield

    async def _observe(
        self,
        operation: str,
        camera: Camera,
        fn: Callable[[], Awaitable[T]],
        on_success: Callable[[T, CameraHealth], None] | None = None,
    ) -> T:
        """Run an upstream operation, recording metrics, structured logs and camera health.

        Health is persisted for both outcomes so status always reflects the latest
        observation. Local refusals (busy/disabled) do not alter camera health.
        """
        start = time.monotonic()
        health = camera.health.model_copy(deep=True)
        health.last_checked_at = _utcnow()
        try:
            result = await fn()
        except GatewayError as exc:
            duration = time.monotonic() - start
            self.metrics.camera_op_latency.labels(operation=operation).observe(duration)
            self.metrics.camera_ops.labels(operation=operation, outcome=exc.code).inc()
            if not isinstance(exc, _LOCAL_REFUSALS):
                health.status = _status_for(exc)
                health.last_error_code = exc.code
                self._save_health(camera, health)
            log.warning(
                "camera operation failed",
                extra={
                    "operation": operation,
                    "camera_id": camera.id,
                    "error_code": exc.code,
                    "duration_ms": int(duration * 1000),
                },
            )
            raise
        duration = time.monotonic() - start
        self.metrics.camera_op_latency.labels(operation=operation).observe(duration)
        self.metrics.camera_ops.labels(operation=operation, outcome="ok").inc()
        health.status = HealthStatus.online
        health.last_success_at = health.last_checked_at
        health.last_error_code = None
        if on_success is not None:
            on_success(result, health)
        self._save_health(camera, health)
        log.info(
            "camera operation succeeded",
            extra={"operation": operation, "camera_id": camera.id, "duration_ms": int(duration * 1000)},
        )
        return result

    def _save_health(self, camera: Camera, health: CameraHealth) -> None:
        self.store.save_health(camera.tenant_id, camera.id, health)
        self.missions.observe_health(camera, health)
        self.refresh_status_gauge()

    # -- ONVIF ---------------------------------------------------------------------

    def _onvif_client(self, camera: Camera) -> OnvifClient:
        password = self._password(camera)
        creds = (
            OnvifCredentials(camera.credentials.username, password)
            if camera.credentials and password is not None
            else None
        )
        return OnvifClient(
            camera.host,
            camera.onvif.port,
            camera.onvif.device_service_path,
            creds,
            timeout_s=self.settings.onvif_timeout_s,
            transport=self._onvif_transport,
        )

    async def probe_onvif(self, tenant_id: str, camera_id: str) -> OnvifProbeResult:
        camera = self.store.get(tenant_id, camera_id)
        self._require_enabled(camera)
        if not camera.onvif.enabled:
            raise FeatureDisabled("ONVIF is not enabled for this camera")

        async def run() -> OnvifProbeResult:
            async with self._onvif_client(camera) as client:
                await client.sync_clock()
                device = await client.device_information()
                media_path = await client.media_service_path()
                profiles = await client.profiles(media_path)
                for profile in profiles:
                    path, mismatch = await client.stream_path(media_path, profile.token)
                    try:
                        profile.stream_path = _validate_rtsp_path(path)
                    except ValueError:
                        profile.stream_path = None
                    profile.reported_host_mismatch = mismatch
            return OnvifProbeResult(
                probed_at=_utcnow(), device=device, media_service_path=media_path, profiles=profiles
            )

        result = await self._observe("onvif_probe", camera, run)
        self.store.save_probe(tenant_id, camera_id, result)
        return result

    # -- RTSP ----------------------------------------------------------------------

    async def _stream_path(self, camera: Camera) -> str:
        if camera.rtsp.path:
            return camera.rtsp.path
        vendor_path = default_rtsp_path(camera.vendor, camera.rtsp.stream)
        if vendor_path and not camera.onvif.enabled:
            return vendor_path
        if camera.onvif.enabled:
            probe = camera.onvif_probe or await self.probe_onvif(camera.tenant_id, camera.id)
            usable = [p for p in probe.profiles if p.stream_path]
            index = 0 if camera.rtsp.stream is StreamVariant.main else 1
            if usable:
                return usable[min(index, len(usable) - 1)].stream_path or ""
        if vendor_path:
            return vendor_path
        raise CameraProtocolError("No RTSP stream path could be determined for this camera")

    async def _rtsp_target(self, camera: Camera) -> RtspTarget:
        password = self._password(camera)
        path = await self._stream_path(camera)
        username = camera.credentials.username if camera.credentials else None
        url = build_rtsp_url(camera.host, camera.rtsp.port, path, username, password)
        return RtspTarget(url=url, transport=camera.rtsp.transport, secrets=(password,) if password else ())

    async def stream_test(self, tenant_id: str, camera_id: str) -> StreamTestResult:
        camera = self.store.get(tenant_id, camera_id)
        self._require_enabled(camera)
        started = time.monotonic()

        async def run() -> MediaInfo:
            async with self._slot(self._media_slots):
                target = await self._rtsp_target(camera)
                return await self.rtsp.probe(target)

        def update(media: MediaInfo, health: CameraHealth) -> None:
            health.media = media

        media = await self._observe("stream_test", camera, run, update)
        return StreamTestResult(
            camera_id=camera.id,
            tested_at=_utcnow(),
            latency_ms=int((time.monotonic() - started) * 1000),
            stream=camera.rtsp.stream,
            transport=camera.rtsp.transport,
            media=media,
        )

    async def snapshot(self, tenant_id: str, camera_id: str) -> Snapshot:
        camera = self.store.get(tenant_id, camera_id)
        self._require_enabled(camera)

        async def run() -> Snapshot:
            async with self._slot(self._media_slots):
                target = await self._rtsp_target(camera)
                jpeg, width, height = await self.rtsp.snapshot(target)
            return Snapshot(jpeg=jpeg, width=width, height=height, captured_at=_utcnow())

        def update(snap: Snapshot, health: CameraHealth) -> None:
            self.missions.observe_frame(camera, snap)
            health.last_frame_at = snap.captured_at
            health.media.width = snap.width
            health.media.height = snap.height

        return await self._observe("snapshot", camera, run, update)

    async def preview(self, tenant_id: str, camera_id: str) -> AsyncIterator[bytes]:
        if not self.settings.enable_mjpeg_preview:
            raise FeatureDisabled("MJPEG preview is disabled")
        camera = self.store.get(tenant_id, camera_id)
        self._require_enabled(camera)
        if self._preview_slots.locked():
            raise Busy("Too many active preview sessions")
        await self._preview_slots.acquire()
        try:
            # Pre-flight probe classifies failures (auth/unreachable/...) before streaming starts.
            await self.stream_test(tenant_id, camera_id)
            target = await self._rtsp_target(self.store.get(tenant_id, camera_id))
        except BaseException:
            self._preview_slots.release()
            raise
        return self._preview_stream(target)

    async def _preview_stream(self, target: RtspTarget) -> AsyncIterator[bytes]:
        self.metrics.preview_sessions.inc()
        try:
            async for chunk in self.rtsp.mjpeg(
                target,
                fps=self.settings.mjpeg_fps,
                max_width=self.settings.mjpeg_max_width,
                duration_s=self.settings.mjpeg_max_duration_s,
            ):
                yield chunk
        finally:
            self.metrics.preview_sessions.dec()
            self._preview_slots.release()

    # -- discovery -----------------------------------------------------------------

    async def discover(self) -> DiscoveryResult:
        if not self.settings.enable_ws_discovery:
            raise FeatureDisabled("WS-Discovery is disabled")
        timeout = self.settings.ws_discovery_timeout_s
        devices = await ws_discover(timeout)
        self.metrics.camera_ops.labels(operation="ws_discovery", outcome="ok").inc()
        return DiscoveryResult(discovered_at=_utcnow(), timeout_s=timeout, devices=devices)
