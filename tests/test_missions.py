"""Local metadata isolation, bounded effects, retries and heuristic health contracts."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from camera_gateway.adapters.process import ProcessResult
from camera_gateway.errors import CameraTimeout
from camera_gateway.mission_models import PtzCommand
from camera_gateway.store import CameraStore
from tests.fakes import CAPABILITIES, PROFILES, TOKEN


def register(client, **overrides):
    body = dict(
        camera_ref="camera",
        name="Camera",
        host="192.168.1.50",
        vendor="ezviz",
        onvif={"enabled": True},
        credentials={"username": "admin", "password_ref": "openbao://kv/cameras/lobby#password"},
    )
    body.update(overrides)
    response = client.post("/v1/cameras", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_groups_memberships_isolation_and_persistence(client, create_camera):
    camera = create_camera()["id"]
    group = client.post("/v1/camera-groups", json={"name": "Lobby", "tags": ["entry"]}).json()
    path = "/v1/camera-groups/" + group["id"]
    member = path + "/members/" + camera
    assert client.put(member).status_code == 204
    assert client.put(member).status_code == 204
    assert client.get(path + "/members").json() == [{"camera_id": camera}]
    assert client.get("/v1/camera-groups?limit=1").json() == [group]
    assert client.put(path, json={"name": "Renamed"}).json()["name"] == "Renamed"
    for target in [path, path + "/members"]:
        assert client.get(target, headers={"X-Tenant-ID": "other"}).status_code == 404
    assert client.put(member, headers={"X-Tenant-ID": "other"}).status_code == 404
    store = CameraStore(client.app.state.settings.database_path)
    from camera_gateway.mission_store import MissionStore

    assert MissionStore(store).group("tenant-a", group["id"]).name == "Renamed"
    store.close()
    assert client.delete(member).status_code == 204
    assert client.get(path + "/members").json() == []
    assert client.put(member).status_code == 204
    assert client.delete("/v1/cameras/" + camera).status_code == 204
    assert client.get(path + "/members").json() == []
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_coverage_crud_and_cascade(client, create_camera):
    camera = create_camera()["id"]
    root = f"/v1/cameras/{camera}/coverage"
    body = dict(zone_ref="external_zone", view_name="North", priority=10, enabled=False, tags=["door"])
    result = client.post(root, json=body)
    assert result.status_code == 201
    path = root + "/" + result.json()["id"]
    assert client.get(root).json() == [result.json()]
    assert client.get(path).json()["zone_ref"] == "external_zone"
    assert client.put(path, json={**body, "priority": 20}).json()["priority"] == 20
    assert client.get(path, headers={"X-Tenant-ID": "other"}).status_code == 404
    assert client.put(path, json={**body, "zone_ref": "../zones"}).status_code == 422
    assert client.post(root, json={**body, "zone_id": 42}).status_code == 422
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    client.post(root, json=body)
    client.delete("/v1/cameras/" + camera)
    assert client.app.state.store._conn.execute("SELECT count(*) FROM camera_coverage").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "a", "password": "secret"},
        {"name": "a", "tags": ["a"] * 33},
    ],
)
def test_group_invalid(client, body):
    assert client.post("/v1/camera-groups", json=body).status_code == 422


def soap(body):
    return httpx.Response(
        200,
        text=(
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:p="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f"<s:Body>{body}</s:Body></s:Envelope>"
        ),
    )


def ptz_device(device):
    device.overrides["GetCapabilities"] = lambda _: httpx.Response(
        200,
        text=CAPABILITIES.replace(
            "</tds:Capabilities>",
            "<tt:PTZ><tt:XAddr>http://evil.example/onvif/ptz</tt:XAddr></tt:PTZ></tds:Capabilities>",
        ),
    )
    device.overrides["GetProfiles"] = lambda _: httpx.Response(
        200,
        text=PROFILES.replace(
            "<tt:Name>mainStream</tt:Name>",
            '<tt:Name>mainStream</tt:Name><tt:PTZConfiguration token="cfg1"/>',
        ),
    )
    device.overrides["GetConfigurationOptions"] = lambda _: soap(
        "<p:GetConfigurationOptionsResponse><p:PTZConfigurationOptions><tt:Spaces>"
        "<tt:AbsolutePanTiltPositionSpace><tt:URI>"
        "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace</tt:URI>"
        "<tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:YRange>"
        "</tt:AbsolutePanTiltPositionSpace><tt:AbsoluteZoomPositionSpace><tt:URI>"
        "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace</tt:URI>"
        "<tt:XRange><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:XRange>"
        "</tt:AbsoluteZoomPositionSpace></tt:Spaces></p:PTZConfigurationOptions>"
        "</p:GetConfigurationOptionsResponse>"
    )
    device.overrides["GetPresets"] = lambda _: soap(
        '<p:GetPresetsResponse><p:Preset token="home"/></p:GetPresetsResponse>'
    )
    for action in ["AbsoluteMove", "GotoPreset", "Stop"]:
        device.overrides[action] = lambda _, a=action: soap(f"<p:{a}Response/>")


def test_ptz_discovery_moves_stop_retry(make_client, onvif_device):
    ptz_device(onvif_device)
    client = make_client(enable_ptz=True)
    camera = register(client)
    cap = client.get(f"/v1/cameras/{camera}/ptz").json()
    assert cap["supported"] and cap["absolute_pan_tilt"] and cap["absolute_zoom"]
    assert cap["presets"] == ["home"]
    root = f"/v1/internal/cameras/{camera}/ptz/commands"
    body = dict(command_ref="move1", action="absolute", pan=0.2, tilt=-0.3, zoom=0.5, timeout_s=0.1)
    assert client.post(root, json=body).status_code == 200
    actions = [onvif_device.action(r) for r in onvif_device.requests]
    assert actions[-2:] == ["AbsoluteMove", "Stop"]
    count = len(actions)
    assert client.post(root, json=body).status_code == 200
    assert len(onvif_device.requests) == count
    assert client.post(root, json={**body, "pan": 0.8}).status_code == 409
    assert all(r.url.host == "192.168.1.50" for r in onvif_device.requests)
    assert (
        client.post(
            root, json=dict(command_ref="preset", action="preset", preset="home", timeout_s=0.1)
        ).status_code
        == 200
    )
    assert (
        client.post(root, json=dict(command_ref="badpreset", action="preset", preset="no")).status_code == 422
    )
    assert client.post(root, json=dict(command_ref="stop", action="stop")).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        dict(action="continuous", pan=0.1),
        dict(action="absolute", pan=2, tilt=0),
        dict(action="absolute", pan=0),
        dict(action="absolute"),
        dict(action="absolute", zoom=-1),
        dict(action="stop", pan=0, tilt=0),
        dict(action="preset"),
        dict(action="preset", preset="../a"),
        dict(action="stop", timeout_s=6),
        dict(action="absolute", zoom=0.5, url="http://evil"),
    ],
)
def test_invalid_ptz(client, create_camera, body):
    camera = create_camera()["id"]
    assert (
        client.post(
            f"/v1/internal/cameras/{camera}/ptz/commands", json={"command_ref": "x", **body}
        ).status_code
        == 422
    )


def test_effect_auth_and_disabled(client, create_camera, make_client):
    camera = create_camera()["id"]
    root = f"/v1/internal/cameras/{camera}"
    assert client.post(root + "/ptz/commands", json={"command_ref": "x", "action": "stop"}).status_code == 403
    assert client.post(root + "/clips", json={"command_ref": "x"}).status_code == 403
    assert (
        client.post(
            root + "/clips", json={"command_ref": "x"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    dev = make_client(auth_mode="disabled", enable_ptz=True, enable_clip_capture=True)
    assert dev.post(root + "/ptz/commands", json={"command_ref": "x", "action": "stop"}).status_code == 403


def test_unsupported_ptz(make_client):
    client = make_client(enable_ptz=True)
    camera = register(client)
    assert client.get(f"/v1/cameras/{camera}/ptz").json()["supported"] is False
    assert (
        client.post(
            f"/v1/internal/cameras/{camera}/ptz/commands", json=dict(command_ref="x", action="stop")
        ).status_code
        == 403
    )


def test_ptz_failure_still_stops_and_blocks_retry(make_client, onvif_device):
    ptz_device(onvif_device)
    client = make_client(enable_ptz=True)
    camera = register(client)
    onvif_device.overrides["AbsoluteMove"] = lambda _: httpx.Response(401)
    path = f"/v1/internal/cameras/{camera}/ptz/commands"
    body = dict(command_ref="x", action="absolute", zoom=0.2, timeout_s=0.1)
    assert client.post(path, json=body).status_code == 502
    assert onvif_device.action(onvif_device.requests[-1]) == "Stop"
    count = len(onvif_device.requests)
    assert client.post(path, json=body).status_code == 409
    assert len(onvif_device.requests) == count


def test_ptz_stop_failure_is_not_completed(make_client, onvif_device):
    ptz_device(onvif_device)
    onvif_device.overrides["Stop"] = lambda _: httpx.Response(500)
    client = make_client(enable_ptz=True)
    camera = register(client)
    path = f"/v1/internal/cameras/{camera}/ptz/commands"
    body = dict(command_ref="x", action="absolute", zoom=0.2, timeout_s=0.1)
    assert client.post(path, json=body).status_code == 502
    assert client.post(path, json=body).status_code == 409


def test_ptz_cancellation_runs_stop(make_client, onvif_device):
    ptz_device(onvif_device)
    client = make_client(enable_ptz=True)
    camera = register(client)
    missions = client.app.state.service.missions

    async def scenario():
        moving = asyncio.Event()

        def moved(_):
            moving.set()
            return soap("<p:AbsoluteMoveResponse/>")

        onvif_device.overrides["AbsoluteMove"] = moved
        task = asyncio.create_task(
            missions.ptz(
                "tenant-a", camera, PtzCommand(command_ref="cancel", action="absolute", zoom=0.3, timeout_s=5)
            )
        )
        await asyncio.wait_for(moving.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert onvif_device.action(onvif_device.requests[-1]) == "Stop"
        assert not missions.ptz_locks

    asyncio.run(scenario())


def ts_bytes():
    return (b"\x47" + bytes(187)) * 8


def test_clip_capture_metadata_expiry_and_retry(make_client, runner):
    client = make_client(enable_clip_capture=True)
    camera = register(client, onvif={"enabled": False})
    runner.results["ffmpeg"] = ProcessResult(0, ts_bytes(), "")
    root = f"/v1/cameras/{camera}/clips"
    capture = f"/v1/internal/cameras/{camera}/clips"
    body = dict(command_ref="clip1", duration_s=1, max_bytes=2048)
    response = client.post(capture, json=body)
    assert response.status_code == 201, response.text
    clip = response.json()
    assert not any(k in clip for k in ["path", "url", "password"])
    path = client.app.state.service.missions.root / (clip["id"] + ".ts")
    assert path.read_bytes() == ts_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.post(capture, json=body).json() == clip
    assert len(runner.calls) == 1
    assert client.get(root).json() == [clip]
    assert client.get(root + "/" + clip["id"], headers={"X-Tenant-ID": "other"}).status_code == 404
    assert client.post(capture, json=body, headers={"X-Tenant-ID": "other"}).status_code == 404
    clip["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    client.app.state.store._conn.execute("UPDATE camera_clips SET body=?", (json.dumps(clip),))
    assert client.get(root).json() == []
    assert not path.exists()
    assert client.post(capture, json=body).status_code == 404
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"duration_s": 31},
        {"duration_s": 0},
        {"max_bytes": 33554433},
        {"max_bytes": 10},
        {"url": "rtsp://evil"},
        {"path": "/etc/passwd"},
        {"output_path": "../file"},
        {"pre_event_s": 5},
    ],
)
def test_clip_rejects_inputs(client, create_camera, body):
    camera = create_camera()["id"]
    assert (
        client.post(f"/v1/internal/cameras/{camera}/clips", json={"command_ref": "c", **body}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(0, b"", ""),
        ProcessResult(0, b"x" * 3000, ""),
        ProcessResult(0, b"bad", ""),
        CameraTimeout(),
    ],
)
def test_clip_failure_leaves_no_file(make_client, runner, result):
    client = make_client(enable_clip_capture=True)
    camera = register(client, onvif={"enabled": False})
    runner.results["ffmpeg"] = result
    assert client.post(
        f"/v1/internal/cameras/{camera}/clips", json=dict(command_ref="c", max_bytes=2048)
    ).status_code in (502, 504)
    assert list(client.app.state.service.missions.root.glob("*")) == []


def test_clip_quota_delete_and_camera_cleanup(make_client, runner):
    client = make_client(enable_clip_capture=True, clip_max_files=1, clip_max_duration_s=2)
    camera = register(client, onvif={"enabled": False})
    root = f"/v1/internal/cameras/{camera}/clips"
    runner.results["ffmpeg"] = ProcessResult(0, ts_bytes(), "")
    assert client.post(root, json={"command_ref": "bad", "duration_s": 3}).status_code == 422
    clip = client.post(root, json={"command_ref": "a", "duration_s": 1}).json()
    assert client.post(root, json={"command_ref": "b", "duration_s": 1}).status_code == 409
    assert client.delete(f"/v1/cameras/{camera}/clips/" + clip["id"]).status_code == 204
    assert client.post(root, json={"command_ref": "c", "duration_s": 1}).status_code == 201
    client.delete("/v1/cameras/" + camera)
    assert list(client.app.state.service.missions.root.glob("*")) == []


def test_health_history_signals_and_bounds(client, create_camera, runner):
    camera = create_camera()["id"]
    root = f"/v1/cameras/{camera}"
    for stderr, signal in [
        ("Connection refused", "offline"),
        ("401 Unauthorized", "auth_failed"),
        ("invalid data", "protocol_error"),
    ]:
        runner.fail("ffprobe", stderr)
        client.post(root + "/stream-test")
        assert client.get(root + "/health/history").json()[0]["signal"] == signal
    runner.ok_snapshot()
    for _ in range(4):
        assert client.get(root + "/snapshot").status_code == 200
    events = client.get(root + "/health/history").json()
    assert any(e["signal"] == "repeated_frame_heuristic" and e["heuristic"] for e in events)
    runner.ok_snapshot(runner.results["ffmpeg"].stdout + b"x" * 200)
    client.get(root + "/snapshot")
    assert any(e["signal"] == "scene_change_heuristic" for e in client.get(root + "/health/history").json())
    service = client.app.state.service
    cam = service.store.get("tenant-a", camera)
    cam.health.last_frame_at = datetime.now(UTC) - timedelta(seconds=120)
    service.store.save_health("tenant-a", camera, cam.health)
    assert client.get(root + "/health/history").json()[0]["signal"] == "frame_stale"
    assert len([e for e in client.get(root + "/health/history").json() if e["signal"] == "frame_stale"]) == 1
    client.get(root + "/snapshot")
    assert any(e["signal"] == "frame_recovered" for e in client.get(root + "/health/history").json())
    for _ in range(210):
        service.missions.signal(cam, "online")
    assert len(client.get(root + "/health/history?limit=200").json()) == 200
    assert client.get(root + "/health/history", headers={"X-Tenant-ID": "other"}).status_code == 404
    assert client.get(root + "/health/history?limit=201").status_code == 422
    metrics = client.get("/metrics").text
    assert 'camera_gateway_health_signals_total{signal="frame_stale"} 1.0' in metrics
    assert camera not in metrics and TOKEN not in metrics


def test_process_hard_output_bound_and_stderr_drain():
    import sys

    from camera_gateway.adapters.process import AsyncProcessRunner
    from camera_gateway.errors import CameraProtocolError

    async def scenario():
        runner = AsyncProcessRunner()
        with pytest.raises(CameraProtocolError):
            await runner.run(
                [
                    sys.executable,
                    "-c",
                    'import sys,time;sys.stdout.write("x"*100000);sys.stdout.flush();time.sleep(10)',
                ],
                timeout_s=2,
                max_stdout=1024,
            )
        result = await runner.run(
            [sys.executable, "-c", 'import sys;sys.stderr.write("x"*200000);sys.stdout.write("ok")'],
            timeout_s=2,
            max_stdout=1024,
        )
        assert result.stdout == b"ok"
        assert len(result.stderr) == 65536

    asyncio.run(scenario())


def test_ptz_move_timeout_stops(make_client, onvif_device):
    ptz_device(onvif_device)
    client = make_client(enable_ptz=True)
    camera = register(client)

    def timeout(request):
        raise httpx.ReadTimeout("private upstream context", request=request)

    onvif_device.overrides["AbsoluteMove"] = timeout
    response = client.post(
        f"/v1/internal/cameras/{camera}/ptz/commands",
        json=dict(command_ref="timeout", action="absolute", zoom=0.5, timeout_s=0.1),
    )
    assert response.status_code == 504
    assert "private upstream context" not in response.text
    assert onvif_device.action(onvif_device.requests[-1]) == "Stop"


def test_ptz_unsupported_space_and_tenant(make_client, onvif_device):
    ptz_device(onvif_device)
    onvif_device.overrides["GetConfigurationOptions"] = lambda _: soap("<p:GetConfigurationOptionsResponse/>")
    client = make_client(enable_ptz=True)
    camera = register(client)
    path = f"/v1/internal/cameras/{camera}/ptz/commands"
    body = dict(command_ref="space", action="absolute", pan=0, tilt=0)
    assert client.post(path, json=body, headers={"X-Tenant-ID": "other"}).status_code == 404
    assert client.post(path, json=body).status_code == 403
    assert not any(onvif_device.action(r) == "AbsoluteMove" for r in onvif_device.requests)


def test_clip_orphan_cleanup(make_client):
    client = make_client()
    root = client.app.state.service.missions.root
    root.mkdir(mode=0o700)
    orphan = root / ("clip_" + "a" * 32 + ".ts")
    orphan.write_bytes(b"partial")
    client.app.state.service.missions.maintenance()
    assert not orphan.exists()


def test_missions_6_buffer_manifest_is_bounded_and_path_free(client, create_camera):
    camera = create_camera()["id"]
    status = client.get(f"/v1/cameras/{camera}/buffer")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is False
    assert body["segment_count"] == 0
    assert "path" not in body
    request = {
        "command_ref": "pre-event-1",
        "event_at": datetime.now(UTC).isoformat(),
        "pre_event_s": 10,
        "max_bytes": 4096,
    }
    first = client.post(f"/v1/internal/cameras/{camera}/pre-event", json=request)
    assert first.status_code == 200
    manifest = first.json()
    assert manifest["status"] == "unavailable"
    assert manifest["segment_ids"] == []
    assert manifest["gaps"]
    assert not any(key in manifest for key in ("path", "url", "filename"))
    assert client.post(f"/v1/internal/cameras/{camera}/pre-event", json=request).json() == manifest


def test_missions_7_normalized_event_feed_marks_heuristics(client, create_camera):
    camera_id = create_camera()["id"]
    camera = client.app.state.store.get("tenant-a", camera_id)
    client.app.state.service.missions.signal(camera, "scene_change_heuristic", True)
    response = client.get(f"/v1/cameras/{camera_id}/events?limit=10&offset=0")
    assert response.status_code == 200
    event = response.json()[0]
    assert event["kind"] == "scene_change"
    assert event["source"] == "frame_delta"
    assert event["heuristic"] is True
    assert event["confidence_kind"] == "heuristic"
    assert "subject_id" not in event and "person" not in event


def test_missions_8_inventory_refresh_contains_no_credentials(client, onvif_device):
    camera_id = register(client)
    before = client.get(f"/v1/cameras/{camera_id}/inventory")
    assert before.status_code == 200 and before.json()["stale"] is True
    refreshed = client.post(f"/v1/cameras/{camera_id}/inventory/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["camera_id"] == camera_id
    assert body["refreshed_at"]
    serialized = json.dumps(body).lower()
    assert "password" not in serialized and "credential" not in serialized


def test_missions_9_rotation_status_never_exposes_secret_ref(client):
    camera_id = register(client)
    response = client.get(f"/v1/cameras/{camera_id}/credentials/rotation-status")
    assert response.status_code == 200
    body = response.json()
    assert body["secret_ref_present"] is True
    assert body["secret_ref_fingerprint"]
    serialized = json.dumps(body)
    assert "openbao://" not in serialized
    assert "lobby#password" not in serialized


def test_missions_10_clock_drift_is_read_only(client, onvif_device):
    camera_id = register(client)
    response = client.get(f"/v1/cameras/{camera_id}/clock-drift")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["camera_id"] == camera_id
    assert body["mutation_performed"] is False
    assert body["source"] == "onvif-get-system-date-time"
    actions = [onvif_device.action(r) for r in onvif_device.requests]
    assert "SetSystemDateAndTime" not in actions


def test_mission_11_configuration_snapshot_is_sanitized(client, create_camera):
    camera = create_camera(labels={"site": "hq"})["id"]
    response = client.get(f"/v1/cameras/{camera}/configuration-snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["camera_id"] == camera
    assert body["config_sha256"]
    assert body["labels_count"] == 1
    assert "password" not in json.dumps(body).lower()


def test_mission_12_connectivity_evidence(client, create_camera):
    camera = create_camera()["id"]
    body = client.get(f"/v1/cameras/{camera}/connectivity-evidence").json()
    assert body["camera_id"] == camera
    assert body["status"] in {"unknown", "online", "offline", "auth_failed", "error"}
    assert body["recent_event_count"] >= 0


def test_mission_13_stream_quality_is_read_only(client, create_camera):
    camera = create_camera()["id"]
    body = client.get(f"/v1/cameras/{camera}/stream-quality").json()
    assert body["camera_id"] == camera
    assert body["status"] in {"known", "unknown"}
    assert "path" not in body and "url" not in body


def test_mission_14_maintenance_metadata_is_idempotent(client, create_camera):
    camera = create_camera()["id"]
    path = f"/v1/internal/cameras/{camera}/maintenance"
    payload = {"command_ref": "maint-1", "enabled": True, "reason_ref": "planned"}
    first = client.post(path, json=payload)
    assert first.status_code == 200
    second = client.post(path, json=payload)
    assert second.status_code == 200 and second.json() == first.json()
    assert client.get(f"/v1/cameras/{camera}/maintenance").json()["enabled"] is True


def test_mission_15_inventory_search_is_tenant_scoped(client, create_camera):
    camera = create_camera(labels={"site": "hq", "zone": "north"})["id"]
    body = client.get("/v1/camera-inventory/search?label_key=site&label_value=hq").json()
    assert camera in body["items"] and body["total"] >= 1
    other = client.get(
        "/v1/camera-inventory/search?label_key=site&label_value=hq",
        headers={"X-Tenant-ID": "other"},
    ).json()
    assert camera not in other["items"]


def test_mission_16_compatibility_is_explicit(client, create_camera):
    camera = create_camera()["id"]
    body = client.get(f"/v1/cameras/{camera}/compatibility").json()
    assert body["camera_id"] == camera
    assert body["checks"]["middleware_authority"] is True
    assert "compatible" in body


def test_mission_17_event_cursor_page(client, create_camera):
    camera_id = create_camera()["id"]
    camera = client.app.state.store.get("tenant-a", camera_id)
    client.app.state.service.missions.signal(camera, "online")
    page = client.get(f"/v1/cameras/{camera_id}/events/page?limit=1&offset=0")
    assert page.status_code == 200
    body = page.json()
    assert body["limit"] == 1 and body["offset"] == 0
    assert len(body["items"]) == 1


def test_mission_18_health_slo(client, create_camera):
    camera = create_camera()["id"]
    body = client.get(f"/v1/cameras/{camera}/health/slo").json()
    assert body["camera_id"] == camera
    assert body["status"] in {"healthy", "degraded", "unknown"}


def test_mission_19_secret_reference_health_redacts_reference(client):
    camera = register(client)
    body = client.get(f"/v1/cameras/{camera}/credentials/reference-health").json()
    assert body["configured"] is True
    assert body["scheme"] == "openbao"
    assert body["fingerprint"]
    assert "openbao://" not in json.dumps(body)
    assert "password" not in json.dumps(body).lower()


def test_mission_20_retention_policy_readback(client, create_camera):
    camera = create_camera()["id"]
    body = client.get(f"/v1/cameras/{camera}/retention-policy").json()
    assert body["raw_frames_persisted"] is False
    assert body["clip_retention_s"] > 0
    assert body["normalized_event_limit"] > 0


def test_mission_21_diagnostics_are_sanitized(client, create_camera):
    camera = create_camera()["id"]
    response = client.get(f"/v1/cameras/{camera}/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["camera_id"] == camera
    assert body["secrets_exposed"] is False
    serialized = json.dumps(body).lower()
    assert "password" not in serialized and "openbao://" not in serialized


def test_mission_22_contract_version_is_stable(client):
    response = client.get("/v1/contract-version")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["contract_revision"] == "camera-gateway.missions-11-22.v1"
    assert len(body["capability_sha256"]) == 64
    assert body["middleware_authority"] is True
