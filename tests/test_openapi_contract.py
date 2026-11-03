"""The committed openapi.yaml must equal the spec generated from the implementation."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from scripts.export_openapi import render

ROOT = Path(__file__).resolve().parents[1]


def test_committed_openapi_matches_implementation() -> None:
    committed = (ROOT / "openapi.yaml").read_text()
    assert committed == render(), "openapi.yaml is stale: run `python -m scripts.export_openapi`"


def test_every_operation_has_id_and_error_envelope(client: TestClient) -> None:
    spec = client.app.openapi()
    for path, item in spec["paths"].items():
        for method, op in item.items():
            assert op.get("operationId"), f"{method} {path}"
            if path.startswith("/v1/"):
                assert "401" in op["responses"], f"{method} {path}"


def test_contract_has_no_plaintext_password_fields() -> None:
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.get("properties", {}).items():
                assert key.lower() not in {"password", "secret", "passwd"}, key
                walk(value)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    creds = spec["components"]["schemas"]["CameraCredentials"]
    assert set(creds["properties"]) == {"username", "password_ref"}
    assert creds["additionalProperties"] is False


def test_required_endpoints_present() -> None:
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    for path in (
        "/healthz",
        "/readyz",
        "/metrics",
        "/v1/capabilities",
        "/v1/cameras",
        "/v1/cameras/status",
        "/v1/cameras/{camera_id}",
        "/v1/cameras/{camera_id}/health",
        "/v1/cameras/{camera_id}/onvif/probe",
        "/v1/cameras/{camera_id}/snapshot",
        "/v1/cameras/{camera_id}/stream-test",
        "/v1/cameras/{camera_id}/preview.mjpeg",
        "/v1/onvif/discovery",
    ):
        assert path in spec["paths"], path
