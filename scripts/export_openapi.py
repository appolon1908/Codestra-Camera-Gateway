"""Render the OpenAPI document from the application: `python -m scripts.export_openapi`."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

from camera_gateway.app import create_app
from camera_gateway.config import Settings
from camera_gateway.store import CameraStore

OUTPUT = Path(__file__).resolve().parents[1] / "openapi.yaml"


def render() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        token = Path(tmp) / "token"
        token.write_text("x" * 32)
        settings = Settings(env="production", api_token_file=token, database_path=Path(tmp) / "db")
        app = create_app(settings, store=CameraStore(":memory:"), configure_logs=False)
        spec = app.openapi()
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if OUTPUT.read_text() != content:
            print("openapi.yaml is stale", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
