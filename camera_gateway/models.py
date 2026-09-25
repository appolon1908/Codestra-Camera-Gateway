"""API contract models. Credentials are represented only by OpenBao-compatible references."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from camera_gateway.secrets import SECRET_REF_PATTERN, SecretRef

# Matches Middleware V3 reference handles (identity_service_contract.REFERENCE).
REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
CAMERA_ID_PATTERN = r"^cam_[a-f0-9]{32}$"
TENANT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_RTSP_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.\-~%=&?;,+]{0,511}$")

Reference = Annotated[str, StringConstraints(pattern=REFERENCE_PATTERN)]
SecretRefStr = Annotated[
    str,
    StringConstraints(pattern=SECRET_REF_PATTERN, max_length=1024),
    Field(
        description="OpenBao-compatible secret reference: openbao://<mount>/<path>#<key>. "
        "Plaintext secrets are rejected.",
        examples=["openbao://kv/cameras/lobby-h8c#password"],
    ),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Vendor(StrEnum):
    generic = "generic"
    ezviz = "ezviz"


class StreamVariant(StrEnum):
    main = "main"
    sub = "sub"


class RtspTransport(StrEnum):
    tcp = "tcp"
    udp = "udp"


class HealthStatus(StrEnum):
    unknown = "unknown"
    online = "online"
    offline = "offline"
    auth_failed = "auth_failed"
    error = "error"


def _validate_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(value):
        raise ValueError("host must be a bare hostname or IP address (no scheme, port, path or credentials)")
    return value.lower()


def _validate_rtsp_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not _RTSP_PATH_RE.fullmatch(value) or ".." in value:
        raise ValueError("rtsp path must be an absolute path without credentials or traversal")
    return value


class CameraCredentials(Strict):
    username: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")]
    password_ref: SecretRefStr

    @field_validator("password_ref")
    @classmethod
    def _parse_ref(cls, v: str) -> str:
        return str(SecretRef.parse(v))


class RtspSettings(Strict):
    port: int = Field(default=554, ge=1, le=65535)
    path: str | None = Field(
        default=None,
        description="Stream path. Optional when a vendor default applies or ONVIF is enabled.",
        examples=["/h264/ch1/main/av_stream"],
    )
    transport: RtspTransport = RtspTransport.tcp
    stream: StreamVariant = StreamVariant.main

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        return _validate_rtsp_path(v)


class OnvifSettings(Strict):
    enabled: bool = False
    port: int = Field(default=80, ge=1, le=65535)
    device_service_path: str = "/onvif/device_service"

    @field_validator("device_service_path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not _RTSP_PATH_RE.fullmatch(v) or ".." in v or "?" in v:
            raise ValueError("device_service_path must be an absolute path")
        return v


class CameraCreate(Strict):
    camera_ref: Reference = Field(description="Opaque tenant-scoped handle used by Middleware V3.")
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    vendor: Vendor = Vendor.generic
    model: Annotated[str, StringConstraints(max_length=64)] | None = Field(default=None, examples=["H8c"])
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    rtsp: RtspSettings = Field(default_factory=RtspSettings)
    onvif: OnvifSettings = Field(default_factory=OnvifSettings)
    credentials: CameraCredentials | None = None
    enabled: bool = True
    labels: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.\-]{1,63}$")],
        Annotated[str, StringConstraints(max_length=128)],
    ] = Field(default_factory=dict, max_length=32)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return _validate_host(v)

    @model_validator(mode="after")
    def _stream_resolvable(self) -> CameraCreate:
        if self.vendor is Vendor.generic and self.rtsp.path is None and not self.onvif.enabled:
            raise ValueError("generic cameras require rtsp.path or onvif.enabled=true")
        return self


class CameraUpdate(Strict):
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    model: Annotated[str, StringConstraints(max_length=64)] | None = None
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)] | None = None
    rtsp: RtspSettings | None = None
    onvif: OnvifSettings | None = None
    credentials: CameraCredentials | None = None
    enabled: bool | None = None
    labels: (
        dict[
            Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.\-]{1,63}$")],
            Annotated[str, StringConstraints(max_length=128)],
        ]
        | None
    ) = Field(default=None, max_length=32)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str | None) -> str | None:
        return None if v is None else _validate_host(v)


class MediaInfo(BaseModel):
    codec: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pixel_format: str | None = None


class CameraHealth(BaseModel):
    status: HealthStatus = HealthStatus.unknown
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_error_code: str | None = None
    media: MediaInfo = Field(default_factory=MediaInfo)


class OnvifDeviceInfo(BaseModel):
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None


class OnvifProfile(BaseModel):
    ptz_configuration_token: str | None = None
    token: str
    name: str | None = None
    video_encoding: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    encoding_profile: str | None = None
    stream_path: str | None = Field(
        default=None,
        description="RTSP path from GetStreamUri, normalized to the registered camera host. "
        "Never contains credentials.",
    )
    reported_host_mismatch: bool = Field(
        default=False,
        description="True when the device advertised a stream host different from the registered host.",
    )


class OnvifProbeResult(BaseModel):
    probed_at: datetime
    device: OnvifDeviceInfo
    media_service_path: str
    profiles: list[OnvifProfile]


class Camera(BaseModel):
    id: Annotated[str, StringConstraints(pattern=CAMERA_ID_PATTERN)]
    tenant_id: str
    camera_ref: str
    name: str
    vendor: Vendor
    model: str | None
    host: str
    rtsp: RtspSettings
    onvif: OnvifSettings
    credentials: CameraCredentials | None
    enabled: bool
    labels: dict[str, str]
    created_at: datetime
    updated_at: datetime
    health: CameraHealth
    onvif_probe: OnvifProbeResult | None = None


class CameraList(BaseModel):
    items: list[Camera]
    total: int
    limit: int
    offset: int


class StatusSummary(BaseModel):
    total: int
    enabled: int
    by_status: dict[HealthStatus, int]


class StreamTestResult(BaseModel):
    ok: Literal[True] = True
    camera_id: str
    tested_at: datetime
    latency_ms: int
    stream: StreamVariant
    transport: RtspTransport
    media: MediaInfo


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str


class ReadinessCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[ReadinessCheck]


class Capabilities(BaseModel):
    service: str
    version: str
    api_version: Literal["v1"] = "v1"
    authority: str
    protocols: list[str]
    vendors: list[Vendor]
    secret_ref_schemes: list[str]
    features: dict[str, bool]
    limits: dict[str, float]


class DiscoveredDevice(BaseModel):
    endpoint_reference: str | None = None
    xaddrs: list[str]
    scopes: list[str]
    types: list[str]


class DiscoveryResult(BaseModel):
    discovered_at: datetime
    timeout_s: float
    devices: list[DiscoveredDevice]
