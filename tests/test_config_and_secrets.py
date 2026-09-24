from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from camera_gateway.config import Settings
from camera_gateway.errors import SecretUnavailable
from camera_gateway.secrets import FileSecretResolver, SecretRef, UnconfiguredSecretResolver
from tests.fakes import SECRET


class TestFailClosedConfig:
    def test_production_rejects_disabled_auth(self) -> None:
        with pytest.raises(ValidationError, match="not permitted"):
            Settings(env="production", auth_mode="disabled")

    def test_token_mode_requires_token_file(self) -> None:
        with pytest.raises(ValidationError, match="API_TOKEN_FILE"):
            Settings(env="production", auth_mode="token", api_token_file=None)

    def test_production_rejects_ws_discovery(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="WS-Discovery"):
            Settings(env="production", api_token_file=tmp_path / "t", enable_ws_discovery=True)

    def test_defaults_are_closed(self, tmp_path: Path) -> None:
        s = Settings(api_token_file=tmp_path / "t")
        assert s.env.value == "production"
        assert not s.enable_mjpeg_preview and not s.enable_ws_discovery


class TestSecretRef:
    @pytest.mark.parametrize(
        "raw",
        [
            "openbao://kv/cameras/lobby#password",
            "openbao://secret/site-1/cam_2/creds#verification_code",
        ],
    )
    def test_valid(self, raw: str) -> None:
        assert str(SecretRef.parse(raw)) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "hunter2",  # plaintext
            "vault://kv/cameras/lobby#password",  # wrong scheme
            "openbao://kv#password",  # no path
            "openbao://kv/cameras/lobby",  # no key
            "openbao://kv/../etc/passwd#x",
            "openbao://kv/cameras/..#password",
            "openbao://kv/cameras/lobby#pass word",
            "openbao://kv/cameras/lobby#password?x=1",
            "",
        ],
    )
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            SecretRef.parse(raw)


class TestFileResolver:
    def test_resolves_and_strips_newline(self, secrets_dir: Path) -> None:
        resolver = FileSecretResolver(secrets_dir)
        assert resolver.ready()
        assert resolver.resolve(SecretRef.parse("openbao://kv/cameras/lobby#password")) == SECRET

    def test_missing_secret_fails_closed(self, secrets_dir: Path) -> None:
        with pytest.raises(SecretUnavailable):
            FileSecretResolver(secrets_dir).resolve(SecretRef.parse("openbao://kv/cameras/nope#password"))

    def test_symlink_escape_rejected(self, secrets_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.write_text("stolen")
        os.symlink(outside, secrets_dir / "kv" / "cameras" / "lobby" / "escape")
        with pytest.raises(SecretUnavailable):
            FileSecretResolver(secrets_dir).resolve(SecretRef.parse("openbao://kv/cameras/lobby#escape"))

    def test_oversize_and_empty_rejected(self, secrets_dir: Path) -> None:
        base = secrets_dir / "kv" / "cameras" / "lobby"
        (base / "big").write_text("x" * 100)
        (base / "empty").write_text("\n")
        (base / "multi").write_text("a\nb\n")
        resolver = FileSecretResolver(secrets_dir, max_bytes=50)
        for key in ("big", "empty", "multi"):
            with pytest.raises(SecretUnavailable):
                resolver.resolve(SecretRef.parse(f"openbao://kv/cameras/lobby#{key}"))

    def test_directory_is_not_a_secret(self, secrets_dir: Path) -> None:
        with pytest.raises(SecretUnavailable):
            FileSecretResolver(secrets_dir).resolve(SecretRef.parse("openbao://kv/cameras#lobby"))

    def test_unconfigured_resolver_fails_closed(self) -> None:
        resolver = UnconfiguredSecretResolver()
        assert not resolver.ready()
        with pytest.raises(SecretUnavailable):
            resolver.resolve(SecretRef.parse("openbao://kv/cameras/lobby#password"))
