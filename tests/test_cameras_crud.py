from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes import OTHER_TENANT, PASSWORD_REF, SECRET

BASE: dict[str, Any] = {
    "camera_ref": "cam-1",
    "name": "Cam 1",
    "host": "192.168.1.60",
    "rtsp": {"path": "/stream1"},
}


def test_create_get_list_update_delete(client: TestClient, create_camera: Callable[..., dict]) -> None:
    cam = create_camera()
    assert cam["id"].startswith("cam_")
    assert cam["credentials"] == {"username": "admin", "password_ref": PASSWORD_REF}
    assert cam["health"]["status"] == "unknown"

    assert client.get(f"/v1/cameras/{cam['id']}").json()["camera_ref"] == "lobby-h8c"
    listing = client.get("/v1/cameras").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == cam["id"]

    patched = client.patch(f"/v1/cameras/{cam['id']}", json={"name": "Front door", "labels": {"zone": "a"}})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Front door"
    assert patched.json()["labels"] == {"zone": "a"}
    assert patched.json()["model"] == "H8c"

    assert client.delete(f"/v1/cameras/{cam['id']}").status_code == 204
    assert client.get(f"/v1/cameras/{cam['id']}").status_code == 404
    assert client.delete(f"/v1/cameras/{cam['id']}").status_code == 404


def test_duplicate_camera_ref_conflicts(client: TestClient, create_camera: Callable[..., dict]) -> None:
    create_camera()
    resp = client.post("/v1/cameras", json={**BASE, "camera_ref": "lobby-h8c"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_tenant_isolation(client: TestClient, create_camera: Callable[..., dict]) -> None:
    cam = create_camera()
    other = {"X-Tenant-ID": OTHER_TENANT}
    assert client.get(f"/v1/cameras/{cam['id']}", headers=other).status_code == 404
    assert client.patch(f"/v1/cameras/{cam['id']}", json={"name": "x"}, headers=other).status_code == 404
    assert client.delete(f"/v1/cameras/{cam['id']}", headers=other).status_code == 404
    assert client.get("/v1/cameras", headers=other).json()["total"] == 0
    assert client.get(f"/v1/cameras/{cam['id']}/snapshot", headers=other).status_code == 404
    # Same camera_ref is independent per tenant.
    resp = client.post("/v1/cameras", json={**BASE, "camera_ref": "lobby-h8c"}, headers=other)
    assert resp.status_code == 201


@pytest.mark.parametrize(
    "mutation",
    [
        {"credentials": {"username": "admin", "password": SECRET}},
        {"credentials": {"username": "admin", "password_ref": SECRET}},
        {"credentials": {"username": "admin", "password_ref": PASSWORD_REF, "password": SECRET}},
        {"password": SECRET},
        {"host": f"admin:{SECRET}@192.168.1.60"},
        {"host": "rtsp://192.168.1.60"},
        {"rtsp": {"path": f"//admin:{SECRET}@evil/stream"}},
        {"rtsp": {"path": "/../../etc"}},
        {"rtsp": {"path": "/ok", "password": SECRET}},
        {"labels": {"password": SECRET}},
    ],
)
def test_plaintext_credentials_and_unsafe_fields_rejected(client: TestClient, mutation: dict) -> None:
    resp = client.post("/v1/cameras", json={**BASE, **mutation})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    # The rejected value is never echoed back.
    assert SECRET not in resp.text


def test_generic_camera_requires_stream_source(client: TestClient) -> None:
    resp = client.post("/v1/cameras", json={**BASE, "rtsp": {}})
    assert resp.status_code == 422
    resp = client.post("/v1/cameras", json={**BASE, "rtsp": {}, "onvif": {"enabled": True}})
    assert resp.status_code == 201


def test_update_revalidates_and_rejects_plaintext(
    client: TestClient, create_camera: Callable[..., dict]
) -> None:
    cam = create_camera()
    resp = client.patch(
        f"/v1/cameras/{cam['id']}", json={"credentials": {"username": "a", "password": SECRET}}
    )
    assert resp.status_code == 422
    assert SECRET not in resp.text
    resp = client.patch(f"/v1/cameras/{cam['id']}", json={"camera_ref": "renamed"})
    assert resp.status_code == 422


def test_secret_value_never_persisted(
    client: TestClient, create_camera: Callable[..., dict], tmp_path: Path
) -> None:
    create_camera()
    raw = b""
    for f in (tmp_path / "db").iterdir():
        raw += f.read_bytes()
    assert SECRET.encode() not in raw
    conn = sqlite3.connect(tmp_path / "db" / "cameras.db")
    (config,) = conn.execute("SELECT config_json FROM cameras").fetchone()
    assert PASSWORD_REF in config and '"password"' not in config


def test_invalid_camera_id_path(client: TestClient) -> None:
    assert client.get("/v1/cameras/not-an-id").status_code == 422


def test_status_summary(client: TestClient, create_camera: Callable[..., dict]) -> None:
    create_camera()
    create_camera(camera_ref="two", enabled=False)
    body = client.get("/v1/cameras/status").json()
    assert body == {
        "total": 2,
        "enabled": 1,
        "by_status": {"unknown": 2, "online": 0, "offline": 0, "auth_failed": 0, "error": 0},
    }


def test_pagination(client: TestClient, create_camera: Callable[..., dict]) -> None:
    for i in range(5):
        create_camera(camera_ref=f"c{i}")
    page = client.get("/v1/cameras", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert client.get("/v1/cameras", params={"limit": 0}).status_code == 422
