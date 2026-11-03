from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

warnings.filterwarnings("ignore", message=".*httpx2.*")

from fastapi.testclient import TestClient  # noqa: E402

from camera_gateway.app import create_app  # noqa: E402
from camera_gateway.config import Settings  # noqa: E402
from tests.fakes import SECRET, TENANT, TOKEN, FakeRunner, OnvifDevice  # noqa: E402


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "secrets"
    (root / "kv" / "cameras" / "lobby").mkdir(parents=True)
    (root / "kv" / "cameras" / "lobby" / "password").write_text(SECRET + "\n")
    return root


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "api-token"
    path.write_text(TOKEN)
    return path


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def onvif_device() -> OnvifDevice:
    return OnvifDevice()


@pytest.fixture
def make_settings(tmp_path: Path, secrets_dir: Path, token_file: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "env": "test",
            "auth_mode": "token",
            "api_token_file": token_file,
            "database_path": tmp_path / "db" / "cameras.db",
            "secrets_dir": secrets_dir,
            "rtsp_timeout_s": 2,
            "onvif_timeout_s": 2,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def make_client(
    make_settings: Callable[..., Settings], runner: FakeRunner, onvif_device: OnvifDevice
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        app = create_app(
            make_settings(**overrides),
            runner=runner,
            onvif_transport=onvif_device.transport(),
            configure_logs=False,
        )
        client = TestClient(app)
        client.__enter__()
        client.headers.update({"Authorization": f"Bearer {TOKEN}", "X-Tenant-ID": TENANT})
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def create_camera(client: TestClient) -> Callable[..., dict[str, Any]]:
    def factory(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "camera_ref": "lobby-h8c",
            "name": "Lobby H8c",
            "vendor": "ezviz",
            "model": "H8c",
            "host": "192.168.1.50",
            "credentials": {"username": "admin", "password_ref": "openbao://kv/cameras/lobby#password"},
        }
        body.update(overrides)
        resp = client.post("/v1/cameras", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    return factory
