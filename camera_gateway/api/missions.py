"""Local metadata and private Middleware-wrappable effect routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from camera_gateway.api.deps import Service, Tenant, require_service_auth
from camera_gateway.api.routes import CameraId, _errors
from camera_gateway.mission_models import (
    Clip,
    ClipRequest,
    CommandResult,
    Coverage,
    CoverageInput,
    Group,
    GroupInput,
    HealthEvent,
    Membership,
    PtzCapability,
    PtzCommand,
)

router = APIRouter(
    prefix="/v1",
    dependencies=[Depends(require_service_auth)],
    responses=_errors(401, 403, 404, 409, 422, 429, 502, 503, 504),
)
GroupId = Annotated[str, Path(pattern=r"^grp_[a-f0-9]{32}$")]
CoverageId = Annotated[str, Path(pattern=r"^cov_[a-f0-9]{32}$")]
ClipId = Annotated[str, Path(pattern=r"^clip_[a-f0-9]{32}$")]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


@router.post("/camera-groups", response_model=Group, status_code=201, tags=["camera-groups"])
def create_group(body: GroupInput, tenant: Tenant, service: Service):
    return service.missions.store.save_group(tenant, body)


@router.get("/camera-groups", response_model=list[Group], tags=["camera-groups"])
def list_groups(tenant: Tenant, service: Service, limit: Limit = 50, offset: Offset = 0):
    return service.missions.store.groups(tenant, limit, offset)


@router.get("/camera-groups/{group_id}", response_model=Group, tags=["camera-groups"])
def get_group(group_id: GroupId, tenant: Tenant, service: Service):
    return service.missions.store.group(tenant, group_id)


@router.put("/camera-groups/{group_id}", response_model=Group, tags=["camera-groups"])
def replace_group(group_id: GroupId, body: GroupInput, tenant: Tenant, service: Service):
    return service.missions.store.save_group(tenant, body, group_id)


@router.delete("/camera-groups/{group_id}", status_code=204, tags=["camera-groups"])
def delete_group(group_id: GroupId, tenant: Tenant, service: Service):
    service.missions.store.delete_group(tenant, group_id)
    return Response(status_code=204)


@router.get("/camera-groups/{group_id}/members", response_model=list[Membership], tags=["camera-groups"])
def list_members(group_id: GroupId, tenant: Tenant, service: Service, limit: Limit = 50, offset: Offset = 0):
    return service.missions.store.members(tenant, group_id, limit, offset)


@router.put("/camera-groups/{group_id}/members/{camera_id}", status_code=204, tags=["camera-groups"])
def add_member(group_id: GroupId, camera_id: CameraId, tenant: Tenant, service: Service):
    service.missions.store.membership(tenant, group_id, camera_id, True)
    return Response(status_code=204)


@router.delete("/camera-groups/{group_id}/members/{camera_id}", status_code=204, tags=["camera-groups"])
def remove_member(group_id: GroupId, camera_id: CameraId, tenant: Tenant, service: Service):
    service.missions.store.membership(tenant, group_id, camera_id, False)
    return Response(status_code=204)


@router.post("/cameras/{camera_id}/coverage", response_model=Coverage, status_code=201, tags=["coverage"])
def create_coverage(camera_id: CameraId, body: CoverageInput, tenant: Tenant, service: Service):
    return service.missions.store.save_coverage(tenant, camera_id, body)


@router.get("/cameras/{camera_id}/coverage", response_model=list[Coverage], tags=["coverage"])
def list_coverage(
    camera_id: CameraId, tenant: Tenant, service: Service, limit: Limit = 50, offset: Offset = 0
):
    return service.missions.store.coverage(tenant, camera_id, limit=limit, offset=offset)


@router.get("/cameras/{camera_id}/coverage/{coverage_id}", response_model=Coverage, tags=["coverage"])
def get_coverage(camera_id: CameraId, coverage_id: CoverageId, tenant: Tenant, service: Service):
    return service.missions.store.coverage(tenant, camera_id, coverage_id)


@router.put("/cameras/{camera_id}/coverage/{coverage_id}", response_model=Coverage, tags=["coverage"])
def replace_coverage(
    camera_id: CameraId, coverage_id: CoverageId, body: CoverageInput, tenant: Tenant, service: Service
):
    return service.missions.store.save_coverage(tenant, camera_id, body, coverage_id)


@router.delete("/cameras/{camera_id}/coverage/{coverage_id}", status_code=204, tags=["coverage"])
def delete_coverage(camera_id: CameraId, coverage_id: CoverageId, tenant: Tenant, service: Service):
    service.missions.store.delete_coverage(tenant, camera_id, coverage_id)
    return Response(status_code=204)


@router.get("/cameras/{camera_id}/ptz", response_model=PtzCapability, tags=["ptz"])
async def ptz_capabilities(camera_id: CameraId, tenant: Tenant, service: Service):
    return await service.missions.ptz_capability(tenant, camera_id)


@router.post(
    "/internal/cameras/{camera_id}/ptz/commands", response_model=CommandResult, tags=["private-effects"]
)
async def ptz_command(camera_id: CameraId, body: PtzCommand, tenant: Tenant, service: Service):
    """Private only. Middleware owns policy, command ledger and reconciliation.

    No continuous moves. Every absolute/preset command attempts Stop after its bounded window.
    A completed result means command and Stop acknowledged, not position verified.
    Never automatically retry an uncertain command with a new command_ref.
    """
    return await service.missions.ptz(tenant, camera_id, body)


@router.post(
    "/internal/cameras/{camera_id}/clips", response_model=Clip, status_code=201, tags=["private-effects"]
)
async def capture_clip(camera_id: CameraId, body: ClipRequest, tenant: Tenant, service: Service):
    """Capture video from the registered stream starting now. No pre-event buffer or arbitrary inputs."""
    return await service.missions.capture(tenant, camera_id, body)


@router.get("/cameras/{camera_id}/clips", response_model=list[Clip], tags=["clips"])
def list_clips(camera_id: CameraId, tenant: Tenant, service: Service, limit: Limit = 50, offset: Offset = 0):
    return service.missions.clips(tenant, camera_id, limit, offset)


@router.get("/cameras/{camera_id}/clips/{clip_id}", response_model=Clip, tags=["clips"])
def clip_metadata(camera_id: CameraId, clip_id: ClipId, tenant: Tenant, service: Service):
    return service.missions.clip(tenant, camera_id, clip_id)


@router.delete("/cameras/{camera_id}/clips/{clip_id}", status_code=204, tags=["clips"])
def delete_clip(camera_id: CameraId, clip_id: ClipId, tenant: Tenant, service: Service):
    service.missions.delete_clip(tenant, camera_id, clip_id)
    return Response(status_code=204)


@router.get("/cameras/{camera_id}/health/history", response_model=list[HealthEvent], tags=["health"])
def health_history(camera_id: CameraId, tenant: Tenant, service: Service, limit: Limit = 50):
    """Newest first, up to 200 observations per camera. Heuristic events do not prove tampering."""
    camera = service.store.get(tenant, camera_id)
    service.missions.check_stale(camera)
    return service.missions.store.history(tenant, camera_id, limit)
