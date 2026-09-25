"""Runtime configuration. All values come from CAMERA_GATEWAY_* environment variables."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    development = "development"
    test = "test"
    production = "production"


class AuthMode(StrEnum):
    token = "token"  # noqa: S105 - enum label, not a credential
    disabled = "disabled"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAMERA_GATEWAY_", extra="ignore")

    env: Environment = Environment.production
    log_level: str = "INFO"

    # Service-to-service authentication. Middleware V3 (or its worker) presents a bearer token.
    auth_mode: AuthMode = AuthMode.token
    api_token_file: Path | None = None

    # Camera registry persistence (SQLite). Never contains plaintext camera credentials.
    database_path: Path = Path("/var/lib/camera-gateway/cameras.db")

    # OpenBao-compatible secret references are resolved from files rendered by an
    # OpenBao/Vault agent (or CSI driver) into this directory. No network coupling.
    secrets_dir: Path | None = None
    max_secret_bytes: int = Field(default=4096, ge=16, le=65536)

    # Upstream camera timeouts (seconds).
    onvif_timeout_s: float = Field(default=5.0, gt=0, le=60)
    rtsp_timeout_s: float = Field(default=10.0, gt=0, le=60)
    snapshot_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_concurrent_media_ops: int = Field(default=4, ge=1, le=64)

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # Optional operator-only features; disabled by default (fail closed).
    enable_mjpeg_preview: bool = False
    mjpeg_max_duration_s: int = Field(default=60, ge=5, le=600)
    mjpeg_max_sessions: int = Field(default=2, ge=1, le=16)
    mjpeg_fps: int = Field(default=5, ge=1, le=15)
    mjpeg_max_width: int = Field(default=640, ge=160, le=1920)

    enable_ptz: bool = False
    enable_clip_capture: bool = False
    clip_max_duration_s: int = Field(default=30, ge=1, le=30)
    clip_max_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    clip_retention_s: int = Field(default=3600, ge=60, le=86400)
    clip_max_files: int = Field(default=100, ge=1, le=1000)
    frame_stale_s: int = Field(default=60, ge=1, le=86400)

    enable_ws_discovery: bool = False
    ws_discovery_timeout_s: float = Field(default=3.0, gt=0, le=15)

    @model_validator(mode="after")
    def _fail_closed(self) -> Settings:
        if self.auth_mode is AuthMode.disabled and self.env is Environment.production:
            raise ValueError("auth_mode=disabled is not permitted when env=production")
        if self.auth_mode is AuthMode.token and self.api_token_file is None:
            raise ValueError("auth_mode=token requires CAMERA_GATEWAY_API_TOKEN_FILE")
        if self.env is Environment.production and self.enable_ws_discovery:
            raise ValueError("WS-Discovery network scanning is not permitted when env=production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
