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
