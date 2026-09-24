"""Request dependencies: service-token authentication and tenant scoping."""

from __future__ import annotations

import hmac
import re
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from camera_gateway.config import AuthMode
from camera_gateway.errors import InvalidRequest, Unauthorized
from camera_gateway.models import TENANT_PATTERN
from camera_gateway.service import CameraService

_bearer = HTTPBearer(auto_error=False, description="Service token presented by Middleware V3")
_TENANT_RE = re.compile(TENANT_PATTERN)


def require_service_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    state = request.app.state
    if state.settings.auth_mode is AuthMode.disabled:
        return
    expected: bytes = state.api_token
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthorized()
    if not hmac.compare_digest(credentials.credentials.encode(), expected):
        raise Unauthorized()


def tenant_id(
    x_tenant_id: Annotated[
        str,
        Header(
            alias="X-Tenant-ID",
            description="Tenant scope asserted by Middleware V3. Cross-tenant access returns 404.",
            min_length=1,
            max_length=128,
        ),
    ],
) -> str:
    if not _TENANT_RE.fullmatch(x_tenant_id):
        raise InvalidRequest("X-Tenant-ID has an invalid format")
    return x_tenant_id


def get_service(request: Request) -> CameraService:
    return request.app.state.service


Tenant = Annotated[str, Depends(tenant_id)]
Service = Annotated[CameraService, Depends(get_service)]
