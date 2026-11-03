from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_gateway.adapters.onvif import OnvifCredentials, ws_security_header
from tests.fakes import AUTH_FAULT, SECRET, FakeRunner, OnvifDevice


@pytest.fixture
def onvif_camera(create_camera: Callable[..., dict]) -> dict:
    return create_camera(vendor="generic", model=None, onvif={"enabled": True, "port": 8000})


def test_probe_returns_device_and_profiles(
    client: TestClient, onvif_device: OnvifDevice, onvif_camera: dict
) -> None:
    resp = client.post(f"/v1/cameras/{onvif_camera['id']}/onvif/probe")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device"]["manufacturer"] == "EZVIZ"
    assert body["device"]["model"] == "CS-H8c"
    # Media XAddr pointed at another host; only its path is used against the registered host.
    assert body["media_service_path"] == "/onvif/Media"
    main, sub = body["profiles"]
    assert main["token"] == "Profile_1"
    assert (main["width"], main["height"], main["fps"], main["bitrate_kbps"]) == (1920, 1080, 25.0, 2048)
    assert main["video_encoding"] == "H264" and main["encoding_profile"] == "Main"
    assert main["stream_path"] == "/Streaming/Channels/101"
    assert main["reported_host_mismatch"] is False
    assert sub["stream_path"] == "/Streaming/Channels/102?transportmode=unicast"
    assert sub["reported_host_mismatch"] is True
    assert "leaked" not in resp.text

    for request in onvif_device.requests:
        assert request.url.host == "192.168.1.50" and request.url.port == 8000
    authed = [r for r in onvif_device.requests if OnvifDevice.action(r) != "GetSystemDateAndTime"]
    assert all(b"PasswordDigest" in r.content for r in authed)
    assert all(SECRET.encode() not in r.content for r in onvif_device.requests)

    camera = client.get(f"/v1/cameras/{onvif_camera['id']}").json()
    assert camera["onvif_probe"]["device"]["serial_number"] == "BD1234567"
    assert camera["health"]["status"] == "online"


def test_stream_path_discovered_via_onvif(client: TestClient, runner: FakeRunner, onvif_camera: dict) -> None:
    runner.ok_probe()
    resp = client.post(f"/v1/cameras/{onvif_camera['id']}/stream-test")
    assert resp.status_code == 200
    assert runner.url_of().endswith("@192.168.1.50:554/Streaming/Channels/101")


def _respond(
    status: int, body: str = "", headers: dict | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(status, text=body, headers=headers or {})


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    return handler


XXE = """<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>&x;</s:Body></s:Envelope>"""


@pytest.mark.parametrize(
    ("handler", "status", "code", "health"),
    [
        (_respond(401), 502, "camera_auth_failed", "auth_failed"),
        (_respond(400, AUTH_FAULT), 502, "camera_auth_failed", "auth_failed"),
        (_respond(200, "this is not xml <<<"), 502, "camera_protocol_error", "error"),
        (_respond(200, ""), 502, "camera_protocol_error", "error"),
        (_respond(200, "<html><body>login</body></html>"), 502, "camera_protocol_error", "error"),
        (_respond(200, XXE), 502, "camera_protocol_error", "error"),
        (_respond(302, "", {"location": "http://evil.example/"}), 502, "camera_protocol_error", "error"),
        (_respond(500, "<oops/>"), 502, "camera_protocol_error", "error"),
        (_raise(httpx.ConnectError("refused")), 502, "camera_unreachable", "offline"),
        (_raise(httpx.ReadTimeout("slow")), 504, "camera_timeout", "offline"),
    ],
)
def test_probe_negative(
    client: TestClient,
    onvif_device: OnvifDevice,
    onvif_camera: dict,
    handler: Callable[[httpx.Request], httpx.Response],
    status: int,
    code: str,
    health: str,
) -> None:
    onvif_device.overrides["GetDeviceInformation"] = handler
    resp = client.post(f"/v1/cameras/{onvif_camera['id']}/onvif/probe")
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code
    h = client.get(f"/v1/cameras/{onvif_camera['id']}/health").json()
    assert h["status"] == health and h["last_error_code"] == code


def test_probe_rejects_non_rtsp_stream_uri(
    client: TestClient, onvif_device: OnvifDevice, onvif_camera: dict
) -> None:
    from tests.fakes import stream_uri

    onvif_device.overrides["GetStreamUri"] = _respond(200, stream_uri("http://evil.example/x"))
    resp = client.post(f"/v1/cameras/{onvif_camera['id']}/onvif/probe")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "camera_protocol_error"


def test_probe_no_profiles(client: TestClient, onvif_device: OnvifDevice, onvif_camera: dict) -> None:
    empty = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"><s:Body>'
        "<trt:GetProfilesResponse/></s:Body></s:Envelope>"
    )
    onvif_device.overrides["GetProfiles"] = _respond(200, empty)
    resp = client.post(f"/v1/cameras/{onvif_camera['id']}/onvif/probe")
    assert resp.status_code == 502


def test_probe_bad_secret_ref(
    client: TestClient, onvif_device: OnvifDevice, create_camera: Callable[..., dict]
) -> None:
    cam = create_camera(
        onvif={"enabled": True},
        credentials={"username": "admin", "password_ref": "openbao://kv/cameras/absent#password"},
    )
    resp = client.post(f"/v1/cameras/{cam['id']}/onvif/probe")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "secret_unavailable"
    assert onvif_device.requests == []


def test_probe_requires_onvif_enabled(client: TestClient, create_camera: Callable[..., dict]) -> None:
    cam = create_camera()
    resp = client.post(f"/v1/cameras/{cam['id']}/onvif/probe")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "feature_disabled"


def test_clock_sync_applies_device_offset(
    client: TestClient, onvif_device: OnvifDevice, onvif_camera: dict
) -> None:
    dt = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body><GetSystemDateAndTimeResponse>'
        "<SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>1</tt:Hour><tt:Minute>2</tt:Minute>"
        "<tt:Second>3</tt:Second></tt:Time><tt:Date><tt:Year>2020</tt:Year><tt:Month>1</tt:Month>"
        "<tt:Day>2</tt:Day></tt:Date></tt:UTCDateTime></SystemDateAndTime>"
        "</GetSystemDateAndTimeResponse></s:Body></s:Envelope>"
    )
    onvif_device.overrides["GetSystemDateAndTime"] = _respond(200, dt)
    assert client.post(f"/v1/cameras/{onvif_camera['id']}/onvif/probe").status_code == 200
    info = next(r for r in onvif_device.requests if OnvifDevice.action(r) == "GetDeviceInformation")
    assert b"<wsu:Created>2020-01-02T01:02:0" in info.content


def test_ws_security_digest_vector() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    nonce = b"0123456789abcdef"
    header = ws_security_header(OnvifCredentials("admin", "pw<&>"), now, nonce)
    expected = base64.b64encode(
        hashlib.sha1(nonce + b"2026-01-01T12:00:00.000Z" + b"pw<&>", usedforsecurity=False).digest()
    ).decode()
    assert expected in header
    assert "pw<&>" not in header
    assert "<wsse:Username>admin</wsse:Username>" in header
