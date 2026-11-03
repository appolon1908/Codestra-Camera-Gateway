from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from camera_gateway.adapters.rtsp import classify_ffmpeg_error, jpeg_dimensions
from camera_gateway.errors import CameraProtocolError, CameraTimeout
from tests.fakes import SECRET, FakeRunner, make_jpeg


def _health(client: TestClient, cam_id: str) -> dict:
    return client.get(f"/v1/cameras/{cam_id}/health").json()


class TestSnapshot:
    def test_success_records_frame_metadata(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        cam = create_camera()
        runner.ok_snapshot(make_jpeg(2304, 1296))
        resp = client.get(f"/v1/cameras/{cam['id']}/snapshot")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-frame-width"] == "2304"
        assert resp.headers["x-frame-height"] == "1296"
        assert resp.content.startswith(b"\xff\xd8")

        # EZVIZ vendor default path, credentials URL-encoded, TCP transport.
        url = runner.url_of()
        assert url == f"rtsp://admin:{quote(SECRET, safe='')}@192.168.1.50:554/h264/ch1/main/av_stream"
        assert runner.calls[-1][runner.calls[-1].index("-rtsp_transport") + 1] == "tcp"

        health = _health(client, cam["id"])
        assert health["status"] == "online"
        assert health["last_frame_at"] == resp.headers["x-frame-timestamp"].replace("+00:00", "Z")
        assert health["media"]["width"] == 2304 and health["media"]["height"] == 1296

    def test_sub_stream_and_explicit_path(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        runner.ok_snapshot()
        sub = create_camera(camera_ref="sub", rtsp={"stream": "sub"})
        client.get(f"/v1/cameras/{sub['id']}/snapshot")
        assert runner.url_of().endswith("/h264/ch1/sub/av_stream")
        explicit = create_camera(camera_ref="exp", vendor="generic", rtsp={"path": "/live/ch0", "port": 8554})
        client.get(f"/v1/cameras/{explicit['id']}/snapshot")
        assert runner.url_of().endswith("@192.168.1.50:8554/live/ch0")

    def test_camera_without_credentials(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        runner.ok_snapshot()
        cam = create_camera(credentials=None, host="fe80::1")
        assert client.get(f"/v1/cameras/{cam['id']}/snapshot").status_code == 200
        assert runner.url_of() == "rtsp://[fe80::1]:554/h264/ch1/main/av_stream"

    def test_non_jpeg_output_is_protocol_error(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        cam = create_camera()
        runner.ok_snapshot(b"<html>not an image</html>")
        resp = client.get(f"/v1/cameras/{cam['id']}/snapshot")
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "camera_protocol_error"
        assert _health(client, cam["id"])["status"] == "error"


NEGATIVE_CASES = [
    # (stderr, http status, error code, health status)
    ("Connection to tcp://192.168.1.50:554 failed: Connection refused", 502, "camera_unreachable", "offline"),
    ("Connection to tcp://192.168.1.50:554 failed: No route to host", 502, "camera_unreachable", "offline"),
    ("Connection to tcp://192.168.1.50:554 failed: Connection timed out", 504, "camera_timeout", "offline"),
    (
        "[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\nrtsp://admin:{secret}@192.168.1.50: "
        "Server returned 401 Unauthorized (authorization failed)",
        502,
        "camera_auth_failed",
        "auth_failed",
    ),
    ("[rtsp @ 0x1] method DESCRIBE failed: 404 Not Found", 502, "camera_stream_not_found", "error"),
    ("rtsp://x: Invalid data found when processing input", 502, "camera_protocol_error", "error"),
    ("something nobody anticipated", 502, "camera_protocol_error", "error"),
]


@pytest.mark.parametrize(("stderr", "status", "code", "health"), NEGATIVE_CASES)
@pytest.mark.parametrize("operation", ["snapshot", "stream-test"])
def test_upstream_failures_fail_closed(
    client: TestClient,
    runner: FakeRunner,
    create_camera: Callable[..., dict],
    caplog: pytest.LogCaptureFixture,
    stderr: str,
    status: int,
    code: str,
    health: str,
    operation: str,
) -> None:
    cam = create_camera()
    stderr = stderr.format(secret=SECRET)
    runner.fail("ffmpeg", stderr)
    runner.fail("ffprobe", stderr)
    with caplog.at_level(logging.DEBUG):
        if operation == "snapshot":
            resp = client.get(f"/v1/cameras/{cam['id']}/snapshot")
        else:
            resp = client.post(f"/v1/cameras/{cam['id']}/stream-test")
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert SECRET not in resp.text
    assert quote(SECRET, safe="") not in resp.text
    # Child-process output is redacted before it reaches logs.
    for record in caplog.records:
        rendered = record.getMessage() + " " + str(record.__dict__)
        assert SECRET not in rendered
        assert quote(SECRET, safe="") not in rendered
    h = _health(client, cam["id"])
    assert h["status"] == health
    assert h["last_error_code"] == code
    assert h["last_checked_at"] is not None


def test_process_timeout(client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]) -> None:
    cam = create_camera()
    runner.results["ffprobe"] = CameraTimeout()
    resp = client.post(f"/v1/cameras/{cam['id']}/stream-test")
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "camera_timeout"


def test_bad_secret_ref_fails_before_connecting(
    client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
) -> None:
    cam = create_camera(
        credentials={"username": "admin", "password_ref": "openbao://kv/cameras/missing#password"}
    )
    runner.ok_snapshot()
    runner.ok_probe()
    for resp in (
        client.get(f"/v1/cameras/{cam['id']}/snapshot"),
        client.post(f"/v1/cameras/{cam['id']}/stream-test"),
    ):
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "secret_unavailable"
    assert runner.calls == []  # no connection attempted without credentials
    assert _health(client, cam["id"])["last_error_code"] == "secret_unavailable"


def test_unconfigured_secret_backend_fails_closed(
    make_client: Callable[..., TestClient], runner: FakeRunner
) -> None:
    client = make_client(secrets_dir=None)
    cam = client.post(
        "/v1/cameras",
        json={
            "camera_ref": "c",
            "name": "c",
            "vendor": "ezviz",
            "host": "10.0.0.2",
            "credentials": {"username": "admin", "password_ref": "openbao://kv/cameras/lobby#password"},
        },
    ).json()
    resp = client.get(f"/v1/cameras/{cam['id']}/snapshot")
    assert resp.status_code == 503
    assert runner.calls == []


class TestStreamTest:
    def test_success_reports_codec_profile(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        cam = create_camera()
        runner.ok_probe()
        resp = client.post(f"/v1/cameras/{cam['id']}/stream-test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["media"] == {
            "codec": "h264",
            "profile": "Main",
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "pixel_format": "yuvj420p",
        }
        health = _health(client, cam["id"])
        assert health["status"] == "online"
        assert health["media"]["codec"] == "h264"

    @pytest.mark.parametrize("payload", [b"not json", b'{"streams": [{"codec_type": "audio"}]}', b"[]"])
    def test_malformed_probe_output(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict], payload: bytes
    ) -> None:
        cam = create_camera()
        runner.ok_probe(payload)
        resp = client.post(f"/v1/cameras/{cam['id']}/stream-test")
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "camera_protocol_error"

    def test_disabled_camera_refused(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        cam = create_camera(enabled=False)
        resp = client.post(f"/v1/cameras/{cam['id']}/stream-test")
        assert resp.status_code == 409
        assert runner.calls == []

    def test_health_reset_on_connection_change(
        self, client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
    ) -> None:
        cam = create_camera()
        runner.ok_probe()
        client.post(f"/v1/cameras/{cam['id']}/stream-test")
        assert _health(client, cam["id"])["status"] == "online"
        client.patch(f"/v1/cameras/{cam['id']}", json={"name": "renamed"})
        assert _health(client, cam["id"])["status"] == "online"
        client.patch(f"/v1/cameras/{cam['id']}", json={"host": "192.168.1.51"})
        assert _health(client, cam["id"])["status"] == "unknown"


def test_metrics_record_outcomes(
    client: TestClient, runner: FakeRunner, create_camera: Callable[..., dict]
) -> None:
    cam = create_camera()
    runner.fail("ffprobe", "401 Unauthorized")
    client.post(f"/v1/cameras/{cam['id']}/stream-test")
    text = client.get("/metrics").text
    assert (
        'camera_gateway_camera_operations_total{operation="stream_test",outcome="camera_auth_failed"} 1.0'
        in text
    )
    assert 'camera_gateway_cameras{status="auth_failed"} 1.0' in text
    assert cam["id"] not in text and "192.168.1.50" not in text


def test_jpeg_dimensions_rejects_garbage() -> None:
    assert jpeg_dimensions(make_jpeg(640, 480)) == (640, 480)
    for bad in (b"", b"\xff\xd8", b"\xff\xd8\xff\xd9", b"GIF89a"):
        with pytest.raises(CameraProtocolError):
            jpeg_dimensions(bad)


def test_classifier_defaults_to_protocol_error() -> None:
    assert classify_ffmpeg_error("").code == "camera_protocol_error"
