"""HTTP routes. OpenAPI is generated from these definitions and committed as openapi.yaml."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from camera_gateway import SERVICE_NAME, __version__
from camera_gateway.api.deps import Service, Tenant, require_service_auth
from camera_gateway.models import (
    CAMERA_ID_PATTERN,
    Camera,
    CameraCreate,
    CameraHealth,
    CameraList,
    CameraUpdate,
    Capabilities,
    DiscoveryResult,
    ErrorResponse,
    Health,
    OnvifProbeResult,
    Readiness,
    ReadinessCheck,
    StatusSummary,
    StreamTestResult,
    Vendor,
)

CameraId = Annotated[str, Path(pattern=CAMERA_ID_PATTERN, description="Gateway camera ID")]


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        401: "Missing or invalid service token",
        403: "Feature disabled by configuration",
        404: "Camera not found for this tenant",
        409: "Conflict (duplicate camera_ref or camera disabled)",
        422: "Validation error",
        429: "Too many concurrent camera operations",
        502: "Camera unreachable, auth failure, stream not found or invalid protocol response",
        503: "Camera credential reference could not be resolved",
        504: "Camera timed out",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


_UPSTREAM = (404, 409, 422, 429, 502, 503, 504)

ops = APIRouter(tags=["operations"])
v1 = APIRouter(
    prefix="/v1",
    dependencies=[Depends(require_service_auth)],
    responses=_errors(401),
)


@ops.get("/healthz", operation_id="health", response_model=Health)
def healthz() -> Health:
    """Liveness: the process is running. Does not contact cameras."""
    return Health(service=SERVICE_NAME, version=__version__)


@ops.get("/health/ready", include_in_schema=True, operation_id="healthReadyAlias")
@ops.get(
    "/readyz",
    operation_id="ready",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "Not ready"}},
)
def readyz(request: Request, response: Response) -> Readiness:
    """Readiness: registry, media tooling and secret backend are available."""
    state = request.app.state
    settings = state.settings
    checks = [
        ReadinessCheck(name="camera_store", ok=state.store.ping()),
        ReadinessCheck(name="ffmpeg", ok=state.runner.available(settings.ffmpeg_path)),
        ReadinessCheck(name="ffprobe", ok=state.runner.available(settings.ffprobe_path)),
        ReadinessCheck(
            name="secret_backend",
            ok=state.secrets.ready(),
            detail=None if settings.secrets_dir else "CAMERA_GATEWAY_SECRETS_DIR not configured",
        ),
    ]
    ready = all(c.ok for c in checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "not_ready", checks=checks)


@ops.get(
    "/metrics",
    operation_id="metrics",
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE_LATEST: {}}, "description": "Prometheus metrics"}},
)
def metrics(request: Request) -> Response:
    """Prometheus exposition. Private network only; never route through public ingress."""
    return Response(generate_latest(request.app.state.metrics.registry), media_type=CONTENT_TYPE_LATEST)


@v1.get("/capabilities", operation_id="capabilities", response_model=Capabilities, tags=["operations"])
def capabilities(request: Request) -> Capabilities:
    settings = request.app.state.settings
    return Capabilities(
        service=SERVICE_NAME,
        version=__version__,
        authority="Middleware V3 is the sole cross-system integration authority",
        protocols=["onvif-profile-s", "rtsp"],
        vendors=list(Vendor),
        secret_ref_schemes=["openbao"],
        features={
            "camera_registry": True,
            "onvif_probe": True,
            "rtsp_snapshot": True,
            "rtsp_stream_test": True,
            "mjpeg_preview": settings.enable_mjpeg_preview,
            "ws_discovery": settings.enable_ws_discovery,
            "middleware_command_adapter": False,
        },
        limits={
            "onvif_timeout_s": settings.onvif_timeout_s,
            "rtsp_timeout_s": settings.rtsp_timeout_s,
            "max_concurrent_media_ops": settings.max_concurrent_media_ops,
            "snapshot_max_bytes": settings.snapshot_max_bytes,
            "mjpeg_max_duration_s": settings.mjpeg_max_duration_s,
            "mjpeg_max_sessions": settings.mjpeg_max_sessions,
        },
    )


cameras = APIRouter(prefix="/cameras", tags=["cameras"])


@cameras.get("/status", operation_id="serviceStatus", response_model=StatusSummary, responses=_errors(422))
def camera_status_summary(tenant: Tenant, service: Service) -> StatusSummary:
    """Tenant-scoped summary of camera health as last observed by this service."""
    total, enabled, by_status = service.store.status_counts(tenant)
    return StatusSummary(total=total, enabled=enabled, by_status=by_status)


@cameras.post(
    "",
    operation_id="createCamera",
    status_code=status.HTTP_201_CREATED,
    response_model=Camera,
    responses=_errors(409, 422),
)
def create_camera(body: CameraCreate, tenant: Tenant, service: Service) -> Camera:
    """Register a camera. Credentials are accepted only as OpenBao secret references;
    any plaintext password field is rejected with 422. camera_ref is unique per tenant,
    making retries safe (a duplicate returns 409 with no effect)."""
    camera = service.store.create(tenant, body)
    service.refresh_status_gauge()
    return camera


@cameras.get("", operation_id="listCameras", response_model=CameraList, responses=_errors(422))
def list_cameras(
    tenant: Tenant,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CameraList:
    items, total = service.store.list(tenant, limit, offset)
    return CameraList(items=items, total=total, limit=limit, offset=offset)


@cameras.get("/{camera_id}", operation_id="getCamera", response_model=Camera, responses=_errors(404, 422))
def get_camera(camera_id: CameraId, tenant: Tenant, service: Service) -> Camera:
    return service.store.get(tenant, camera_id)


@cameras.patch(
    "/{camera_id}", operation_id="updateCamera", response_model=Camera, responses=_errors(404, 422)
)
def update_camera(camera_id: CameraId, body: CameraUpdate, tenant: Tenant, service: Service) -> Camera:
    """Partially update a camera. Changing host, rtsp, onvif or credentials resets health."""
    camera = service.store.update(tenant, camera_id, body)
    service.refresh_status_gauge()
    return camera


@cameras.delete(
    "/{camera_id}",
    operation_id="deleteCamera",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_errors(404, 422),
)
def delete_camera(camera_id: CameraId, tenant: Tenant, service: Service) -> Response:
    service.store.delete(tenant, camera_id)
    service.refresh_status_gauge()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@cameras.get(
    "/{camera_id}/health",
    operation_id="getCameraHealth",
    response_model=CameraHealth,
    responses=_errors(404, 422),
)
def camera_health(camera_id: CameraId, tenant: Tenant, service: Service) -> CameraHealth:
    """Last observed health: status, last frame timestamp, dimensions and codec/profile."""
    return service.store.get(tenant, camera_id).health


@cameras.post(
    "/{camera_id}/onvif/probe",
    operation_id="probeCameraOnvif",
    response_model=OnvifProbeResult,
    responses=_errors(403, *_UPSTREAM),
)
async def probe_onvif(camera_id: CameraId, tenant: Tenant, service: Service) -> OnvifProbeResult:
    """Query the camera over ONVIF for device information, media profiles and stream paths.
    Advertised addresses are reduced to paths on the registered host (no SSRF)."""
    return await service.probe_onvif(tenant, camera_id)


@cameras.post(
    "/{camera_id}/stream-test",
    operation_id="testCameraStream",
    response_model=StreamTestResult,
    responses=_errors(403, *_UPSTREAM),
)
async def stream_test(camera_id: CameraId, tenant: Tenant, service: Service) -> StreamTestResult:
    """Open the RTSP stream and report codec, profile, dimensions and frame rate."""
    return await service.stream_test(tenant, camera_id)


@cameras.get(
    "/{camera_id}/snapshot",
    operation_id="getCameraSnapshot",
    response_class=Response,
    responses={
        200: {
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
            "description": "A single JPEG frame. X-Frame-* headers carry capture metadata.",
            "headers": {
                "X-Frame-Timestamp": {"schema": {"type": "string", "format": "date-time"}},
                "X-Frame-Width": {"schema": {"type": "integer"}},
                "X-Frame-Height": {"schema": {"type": "integer"}},
            },
        },
        **_errors(*_UPSTREAM),
    },
)
async def snapshot(camera_id: CameraId, tenant: Tenant, service: Service) -> Response:
    """Capture one frame over RTSP. Frames are never stored by the gateway."""
    snap = await service.snapshot(tenant, camera_id)
    return Response(
        content=snap.jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Timestamp": snap.captured_at.isoformat(),
            "X-Frame-Width": str(snap.width),
            "X-Frame-Height": str(snap.height),
        },
    )


@cameras.get(
    "/{camera_id}/preview.mjpeg",
    operation_id="previewCameraMjpeg",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"multipart/x-mixed-replace": {"schema": {"type": "string", "format": "binary"}}},
            "description": "Operator-only, time-limited, low-rate MJPEG preview (boundary=frame).",
        },
        **_errors(403, *_UPSTREAM),
    },
)
async def preview(camera_id: CameraId, tenant: Tenant, service: Service) -> StreamingResponse:
    """LOCAL/OPERATOR USE ONLY. Disabled unless CAMERA_GATEWAY_ENABLE_MJPEG_PREVIEW=true.
    Bounded by session count, duration, frame rate and resolution. Not an integration
    surface for Middleware or other services."""
    stream = await service.preview(tenant, camera_id)
    return StreamingResponse(
        stream,
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@v1.post(
    "/onvif/discovery",
    operation_id="discoverOnvifDevices",
    response_model=DiscoveryResult,
    tags=["onvif"],
    responses=_errors(403),
)
async def discover(service: Service) -> DiscoveryResult:
    """WS-Discovery multicast probe of the local segment. Development/operator use only;
    disabled by default and refused in production configuration."""
    return await service.discover()


v1.include_router(cameras)
