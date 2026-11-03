"""Fail-closed error taxonomy. Every error maps to a stable code; messages never carry secrets."""

from __future__ import annotations


class GatewayError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Internal error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class Unauthorized(GatewayError):
    status_code = 401
    code = "unauthorized"
    default_message = "Missing or invalid service credentials"


class FeatureDisabled(GatewayError):
    status_code = 403
    code = "feature_disabled"
    default_message = "Feature is disabled by configuration"


class CameraNotFound(GatewayError):
    status_code = 404
    code = "camera_not_found"
    default_message = "Camera not found"


class Conflict(GatewayError):
    status_code = 409
    code = "conflict"
    default_message = "Resource conflict"


class InvalidRequest(GatewayError):
    status_code = 422
    code = "validation_error"
    default_message = "Invalid request"


class Busy(GatewayError):
    status_code = 429
    code = "busy"
    default_message = "Too many concurrent camera operations"


class SecretUnavailable(GatewayError):
    """Secret reference could not be resolved. No upstream connection is attempted."""

    status_code = 503
    code = "secret_unavailable"
    default_message = "Camera credential reference could not be resolved"


class UpstreamError(GatewayError):
    status_code = 502
    code = "camera_error"
    default_message = "Camera operation failed"


class CameraUnreachable(UpstreamError):
    code = "camera_unreachable"
    default_message = "Camera is unreachable"


class CameraTimeout(UpstreamError):
    status_code = 504
    code = "camera_timeout"
    default_message = "Camera did not respond before the timeout"


class CameraAuthFailed(UpstreamError):
    code = "camera_auth_failed"
    default_message = "Camera rejected the configured credentials"


class CameraProtocolError(UpstreamError):
    code = "camera_protocol_error"
    default_message = "Camera returned an invalid or unexpected response"


class CameraStreamNotFound(UpstreamError):
    code = "camera_stream_not_found"
    default_message = "Camera stream path was not found"
