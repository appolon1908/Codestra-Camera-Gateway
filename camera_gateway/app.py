"""Application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from camera_gateway import SERVICE_NAME, __version__
from camera_gateway.adapters.process import AsyncProcessRunner, ProcessRunner
from camera_gateway.adapters.rtsp import RtspClient
from camera_gateway.api.routes import ops, v1
from camera_gateway.config import AuthMode, Settings, get_settings
from camera_gateway.errors import GatewayError
from camera_gateway.logging import configure_logging, request_id_var
from camera_gateway.metrics import Metrics
from camera_gateway.models import ErrorDetail, ErrorResponse
from camera_gateway.secrets import FileSecretResolver, SecretResolver, UnconfiguredSecretResolver
from camera_gateway.service import CameraService
from camera_gateway.store import CameraStore

log = logging.getLogger("camera_gateway.http")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

DESCRIPTION = """\
Standalone ONVIF/RTSP camera gateway. Owns camera connectivity only.

Middleware V3 is the sole cross-system integration authority: callers reach this API only
through Caddy -> Kong -> Middleware V3. Camera credentials are OpenBao secret references;
plaintext passwords are never accepted, stored or returned.
"""


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, request_id=request_id_var.get()))
    return JSONResponse(status_code=status, content=body.model_dump(), headers={"Cache-Control": "no-store"})


def _load_token(settings: Settings) -> bytes:
    if settings.auth_mode is AuthMode.disabled:
        return b""
    assert settings.api_token_file is not None
    token = settings.api_token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("API token must be at least 32 characters")
    return token.encode()


def create_app(
    settings: Settings | None = None,
    *,
    store: CameraStore | None = None,
    secrets: SecretResolver | None = None,
    runner: ProcessRunner | None = None,
    onvif_transport: httpx.AsyncBaseTransport | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(settings.log_level)

    store = store or CameraStore(settings.database_path)
    if secrets is None:
        secrets = (
            FileSecretResolver(settings.secrets_dir, settings.max_secret_bytes)
            if settings.secrets_dir
            else UnconfiguredSecretResolver()
        )
    runner = runner or AsyncProcessRunner()
    metrics = Metrics()
    rtsp = RtspClient(
        runner,
        ffmpeg=settings.ffmpeg_path,
        ffprobe=settings.ffprobe_path,
        timeout_s=settings.rtsp_timeout_s,
        snapshot_max_bytes=settings.snapshot_max_bytes,
    )
    service = CameraService(settings, store, secrets, rtsp, metrics, onvif_transport=onvif_transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("service starting", extra={"version": __version__, "env": settings.env.value})
        service.missions.maintenance()
        maintenance = asyncio.create_task(service.missions.maintain())
        try:
            yield
        finally:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
            store.close()
        log.info("service stopped")

    app = FastAPI(
        title="Codestra Camera Gateway API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.env.value != "production" else None,
        redoc_url=None,
        servers=[{"url": "http://localhost:8080", "description": "Local development"}],
    )
    app.state.settings = settings
    app.state.api_token = _load_token(settings)
    app.state.store = store
    app.state.secrets = secrets
    app.state.runner = runner
    app.state.metrics = metrics
    app.state.service = service

    @app.middleware("http")
    async def observe(request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            log.exception("unhandled error")
            response = _error(500, "internal_error", "Internal error")
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")
            duration = time.monotonic() - start
            metrics.http_requests.labels(request.method, template, str(status)).inc()
            metrics.http_latency.labels(request.method, template).observe(duration)
            if template not in ("/healthz", "/readyz", "/metrics"):
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "route": template,
                        "status": status,
                        "duration_ms": int(duration * 1000),
                    },
                )
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo request input: it could contain a plaintext secret sent in error.
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return _error(422, "validation_error", "Invalid request: " + ", ".join(fields)[:500])

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"exc_type": type(exc).__name__})
        return _error(500, "internal_error", "Internal error")

    app.include_router(ops)
    app.include_router(v1)
    from camera_gateway.api.missions import router

    app.include_router(router)
    return app


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(
        "camera_gateway.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container network; exposure is governed by ingress policy
        port=8080,
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )


__all__ = ["SERVICE_NAME", "create_app", "main"]
