from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable

import pytest
from fastapi.testclient import TestClient

from camera_gateway.adapters.discovery import parse_probe_matches, ws_discover
from camera_gateway.adapters.rtsp import RtspClient, RtspTarget
from tests.fakes import FakeRunner

PROBE_MATCH = b"""<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches><d:ProbeMatch>
<w:EndpointReference><w:Address>urn:uuid:abc</w:Address></w:EndpointReference>
<d:Types>dn:NetworkVideoTransmitter</d:Types>
<d:Scopes>onvif://www.onvif.org/name/EZVIZ onvif://www.onvif.org/hardware/CS-H8c</d:Scopes>
<d:XAddrs>http://192.168.1.50/onvif/device_service</d:XAddrs>
</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"""


def test_parse_probe_matches() -> None:
    (device,) = parse_probe_matches(PROBE_MATCH)
    assert device.endpoint_reference == "urn:uuid:abc"
    assert device.xaddrs == ["http://192.168.1.50/onvif/device_service"]
    assert "onvif://www.onvif.org/hardware/CS-H8c" in device.scopes
    assert parse_probe_matches(b"garbage") == []
    assert parse_probe_matches(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>') == []


def test_ws_discover_against_local_responder() -> None:
    async def scenario() -> list:
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.bind(("127.0.0.1", 0))
        responder.setblocking(False)
        loop = asyncio.get_running_loop()

        async def reply() -> None:
            data, addr = await loop.sock_recvfrom(responder, 65536)
            assert b"NetworkVideoTransmitter" in data
            await loop.sock_sendto(responder, PROBE_MATCH, addr)

        task = asyncio.create_task(reply())
        try:
            return await ws_discover(0.5, target=responder.getsockname())
        finally:
            await task
            responder.close()

    devices = asyncio.run(scenario())
    assert [d.endpoint_reference for d in devices] == ["urn:uuid:abc"]


def test_discovery_endpoint_disabled_by_default(client: TestClient) -> None:
    resp = client.post("/v1/onvif/discovery")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "feature_disabled"


class TestMjpegPreview:
    def test_disabled_by_default(self, client: TestClient, create_camera: Callable[..., dict]) -> None:
        cam = create_camera()
        resp = client.get(f"/v1/cameras/{cam['id']}/preview.mjpeg")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "feature_disabled"

    @pytest.fixture
    def preview_client(
        self, make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
    ) -> TestClient:
        async def fake_mjpeg(self: RtspClient, target: RtspTarget, **kw: int) -> AsyncIterator[bytes]:
            assert kw["fps"] == 5 and kw["max_width"] == 640 and kw["duration_s"] == 60
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xd9\r\n"

        monkeypatch.setattr(RtspClient, "mjpeg", fake_mjpeg)
        return make_client(enable_mjpeg_preview=True, mjpeg_max_sessions=1)

    def test_streams_when_enabled(self, preview_client: TestClient, runner: FakeRunner) -> None:
        runner.ok_probe()
        cam = preview_client.post(
            "/v1/cameras",
            json={"camera_ref": "p", "name": "p", "host": "10.0.0.3", "rtsp": {"path": "/live"}},
        ).json()
        resp = preview_client.get(f"/v1/cameras/{cam['id']}/preview.mjpeg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
        assert resp.content.startswith(b"--frame")
        # Session slot released after the stream ends.
        assert preview_client.get(f"/v1/cameras/{cam['id']}/preview.mjpeg").status_code == 200

    def test_preflight_failure_is_classified(self, preview_client: TestClient, runner: FakeRunner) -> None:
        runner.fail("ffprobe", "401 Unauthorized")
        cam = preview_client.post(
            "/v1/cameras",
            json={"camera_ref": "p", "name": "p", "host": "10.0.0.3", "rtsp": {"path": "/live"}},
        ).json()
        resp = preview_client.get(f"/v1/cameras/{cam['id']}/preview.mjpeg")
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "camera_auth_failed"
        # Slot was released on failure.
        runner.ok_probe()
        assert preview_client.get(f"/v1/cameras/{cam['id']}/preview.mjpeg").status_code == 200
