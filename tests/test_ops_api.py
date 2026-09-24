from __future__ import annotations

import json
import logging
from collections.abc import Callable

from fastapi.testclient import TestClient

from camera_gateway.logging import JsonFormatter
from camera_gateway.redaction import redact
from tests.fakes import SECRET, FakeRunner


def test_healthz_is_unauthenticated(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"Authorization": ""})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.headers["X-Request-ID"]


def test_readyz_ready(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_readyz_not_ready_without_media_tools(client: TestClient, runner: FakeRunner) -> None:
    runner.binaries_available = False
    resp = client.get("/readyz")
    assert resp.status_code == 503
    failing = {c["name"] for c in resp.json()["checks"] if not c["ok"]}
    assert failing == {"ffmpeg", "ffprobe"}


def test_readyz_not_ready_without_secret_backend(make_client: Callable[..., TestClient]) -> None:
    client = make_client(secrets_dir=None)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    check = next(c for c in resp.json()["checks"] if c["name"] == "secret_backend")
    assert check["ok"] is False


def test_metrics_exposition(client: TestClient) -> None:
    client.get("/v1/capabilities")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "camera_gateway_http_requests_total" in body
    assert 'route="/v1/capabilities"' in body
    assert "camera_gateway_cameras" in body


def test_capabilities(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["protocols"] == ["onvif-profile-s", "rtsp"]
    assert set(body["vendors"]) == {"generic", "ezviz"}
    assert body["secret_ref_schemes"] == ["openbao"]
    assert body["features"]["mjpeg_preview"] is False
    assert body["features"]["middleware_command_adapter"] is False
    assert "Middleware V3" in body["authority"]


class TestServiceAuth:
    def test_missing_token(self, client: TestClient) -> None:
        resp = client.get("/v1/capabilities", headers={"Authorization": ""})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_wrong_token(self, client: TestClient) -> None:
        resp = client.get("/v1/cameras", headers={"Authorization": "Bearer " + "x" * 40})
        assert resp.status_code == 401

    def test_auth_disabled_only_outside_production(self, make_client: Callable[..., TestClient]) -> None:
        client = make_client(env="development", auth_mode="disabled")
        assert client.get("/v1/capabilities", headers={"Authorization": ""}).status_code == 200


def test_tenant_header_required(client: TestClient) -> None:
    resp = client.get("/v1/cameras", headers={"X-Tenant-ID": ""})
    assert resp.status_code == 422
    resp = client.get("/v1/cameras", headers={"X-Tenant-ID": "bad tenant!"})
    assert resp.status_code == 422


def test_request_id_propagation(client: TestClient) -> None:
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id\n"}).headers["X-Request-ID"]
    assert generated != "bad id\n" and len(generated) == 32


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_redaction() -> None:
    assert redact("rtsp://admin:pw@1.2.3.4:554/x") == "rtsp://***@1.2.3.4:554/x"
    assert (
        redact("<wsse:Password Type='x'>abc</wsse:Password>") == "<wsse:Password Type='x'>***</wsse:Password>"
    )
    assert SECRET not in redact(f"failed with {SECRET}", [SECRET])


def test_json_log_formatter_redacts() -> None:
    record = logging.makeLogRecord(
        {"msg": "open rtsp://admin:pw@cam/x", "levelname": "INFO", "name": "t", "detail": "rtsp://u:p@h/"}
    )
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "open rtsp://***@cam/x"
    assert out["detail"] == "rtsp://***@h/"
    assert out["service"] == "codestra-camera-gateway"
