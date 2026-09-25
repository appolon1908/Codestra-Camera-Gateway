"""Service-local metadata and private command contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from camera_gateway.models import Reference, Strict

Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Tags = Annotated[list[Reference], Field(max_length=32)]


class GroupInput(Strict):
    name: Name
    tags: Tags = Field(default_factory=list)


class Group(GroupInput):
    id: str


class CoverageInput(Strict):
    zone_ref: Reference = Field(description="Opaque external reference; gateway does not own zones.")
    view_name: Name
    priority: int = Field(default=0, ge=0, le=1000)
    enabled: bool = True
    tags: Tags = Field(default_factory=list)


class Coverage(CoverageInput):
    id: str
    camera_id: str


class Membership(BaseModel):
    camera_id: str


class PtzCapability(BaseModel):
    supported: bool = False
    profile_token: str | None = None
    absolute_pan_tilt: bool = False
    absolute_zoom: bool = False
    presets: list[str] = Field(default_factory=list)


class PtzCommand(Strict):
    command_ref: Reference
    action: Literal["absolute", "preset", "stop"]
    pan: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    tilt: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    zoom: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    preset: Reference | None = None
    timeout_s: float = Field(default=2, ge=0.1, le=5, allow_inf_nan=False)

    @model_validator(mode="after")
    def shape(self):
        if self.action == "absolute":
            if (self.pan is None) != (self.tilt is None):
                raise ValueError("pan and tilt must be supplied together")
            if self.pan is None and self.zoom is None:
                raise ValueError("absolute requires a position")
            if self.preset is not None:
                raise ValueError("absolute cannot include preset")
        elif self.pan is not None or self.tilt is not None or self.zoom is not None:
            raise ValueError("position is valid only for absolute")
        if (self.action == "preset") != (self.preset is not None):
            raise ValueError("preset action requires preset only")
        return self


class CommandResult(BaseModel):
    command_ref: str
    status: Literal["completed"] = "completed"


class ClipRequest(Strict):
    command_ref: Reference
    duration_s: int = Field(default=5, ge=1, le=30)
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)


class Clip(BaseModel):
    id: str
    camera_id: str
    command_ref: str
    started_at: datetime
    completed_at: datetime
    expires_at: datetime
    requested_duration_s: int
    size_bytes: int
    media_type: Literal["video/mp2t"] = "video/mp2t"


class HealthEvent(BaseModel):
    observed_at: datetime
    signal: Literal[
        "online",
        "offline",
        "auth_failed",
        "protocol_error",
        "error",
        "frame_stale",
        "frame_recovered",
        "scene_change_heuristic",
        "repeated_frame_heuristic",
    ]
    heuristic: bool = False


class RollingBufferStatus(BaseModel):
    camera_id: str
    enabled: bool
    stored_bytes: int = 0
    reserved_bytes: int = 0
    segment_count: int = 0
    oldest_observed_at: datetime | None = None
    newest_observed_at: datetime | None = None
    max_bytes: int
    retention_s: int
    segment_duration_s: int
    last_error_code: str | None = None


class PreEventRequest(Strict):
    command_ref: Reference
    event_at: datetime
    pre_event_s: int = Field(default=10, ge=1, le=300)
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)


class CoveredInterval(BaseModel):
    started_at: datetime
    ended_at: datetime


class PreEventManifest(BaseModel):
    id: str
    camera_id: str
    command_ref: str
    requested_at: datetime
    event_at: datetime
    requested_pre_event_s: int
    status: Literal["available", "unavailable"]
    segment_ids: list[str] = Field(default_factory=list)
    covered_intervals: list[CoveredInterval] = Field(default_factory=list)
    gaps: list[CoveredInterval] = Field(default_factory=list)
    expires_at: datetime


class NormalizedCameraEvent(BaseModel):
    id: int
    camera_id: str
    observed_at: datetime
    kind: Literal["motion", "scene_change", "tamper", "health"]
    source: Literal["gateway_health", "frame_delta", "camera_native"] = "gateway_health"
    source_version: str = "camera-gateway.v1"
    heuristic: bool = False
    confidence_kind: Literal["heuristic", "observed"] = "observed"
    score: float | None = Field(default=None, ge=0, le=1)
    signal: str


class DeviceInventory(BaseModel):
    camera_id: str
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    refreshed_at: datetime | None = None
    stale: bool = True


class CredentialRotationStatus(BaseModel):
    camera_id: str
    secret_ref_present: bool
    secret_ref_fingerprint: str | None = None
    current_ref_version: str | None = None
    lease_expires_at: datetime | None = None
    last_successful_auth: datetime | None = None
    next_rotation_due: datetime | None = None
    reload_supported: bool = True


class CredentialReloadRequest(Strict):
    command_ref: Reference


class CredentialReloadResult(BaseModel):
    command_ref: str
    status: Literal["completed"] = "completed"
    authenticated_at: datetime


class ClockDriftHealth(BaseModel):
    camera_id: str
    supported: bool
    observed_at: datetime
    drift_ms: int | None = None
    stale: bool = False
    source: Literal["onvif-get-system-date-time"] = "onvif-get-system-date-time"
    mutation_performed: Literal[False] = False


class ConfigurationSnapshot(BaseModel):
    camera_id: str
    observed_at: datetime
    config_sha256: str
    enabled: bool
    vendor: str
    rtsp_transport: str
    onvif_enabled: bool
    credential_ref_present: bool
    labels_count: int


class ConnectivityEvidence(BaseModel):
    camera_id: str
    observed_at: datetime
    status: str
    last_success_at: datetime | None = None
    last_error_code: str | None = None
    recent_event_count: int


class StreamQualityStatus(BaseModel):
    camera_id: str
    observed_at: datetime
    status: Literal["known", "unknown"]
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    last_frame_at: datetime | None = None
    stale: bool


class MaintenanceState(BaseModel):
    camera_id: str
    enabled: bool
    reason_ref: str | None = None
    changed_at: datetime | None = None


class MaintenanceUpdate(Strict):
    command_ref: Reference
    enabled: bool
    reason_ref: Reference | None = None


class CameraSearchResult(BaseModel):
    items: list[str]
    total: int


class CompatibilityStatus(BaseModel):
    camera_id: str
    compatible: bool
    checks: dict[str, bool]
    warnings: list[str] = Field(default_factory=list)


class EventCursorPage(BaseModel):
    items: list[NormalizedCameraEvent]
    next_offset: int | None = None
    limit: int
    offset: int


class HealthSloStatus(BaseModel):
    camera_id: str
    observed_at: datetime
    health_fresh: bool
    frame_fresh: bool
    last_success_at: datetime | None = None
    last_frame_at: datetime | None = None
    status: Literal["healthy", "degraded", "unknown"]


class SecretReferenceHealth(BaseModel):
    camera_id: str
    configured: bool
    scheme: Literal["openbao"] | None = None
    fingerprint: str | None = None
    reference_valid: bool
    last_successful_auth: datetime | None = None


class RetentionPolicyStatus(BaseModel):
    camera_id: str
    clip_retention_s: int
    rolling_buffer_retention_s: int
    health_event_limit: int
    normalized_event_limit: int
    raw_frames_persisted: Literal[False] = False


class DiagnosticsSnapshot(BaseModel):
    camera_id: str
    observed_at: datetime
    health_status: str
    inventory_stale: bool
    credentials_configured: bool
    rolling_buffer_enabled: bool
    maintenance_enabled: bool
    clock_drift_ms: int | None = None
    secrets_exposed: Literal[False] = False


class ContractVersionStatus(BaseModel):
    service: str
    service_version: str
    api_version: Literal["v1"] = "v1"
    contract_revision: Literal["camera-gateway.missions-11-22.v1"] = "camera-gateway.missions-11-22.v1"
    capability_sha256: str
    middleware_authority: Literal[True] = True
