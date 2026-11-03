"""OpenBao-compatible secret references.

Contracts carry references of the form ``openbao://<mount>/<path...>#<key>``; plaintext
secrets are never accepted, stored, logged or returned. At runtime a reference is resolved
from a file rendered by an OpenBao (or Vault-compatible) agent/CSI driver:

    <secrets_dir>/<mount>/<path...>/<key>

The gateway therefore has no network or SDK dependency on OpenBao itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from camera_gateway.errors import SecretUnavailable

SCHEME = "openbao"
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
SECRET_REF_PATTERN = rf"^{SCHEME}://{_SEGMENT}(?:/{_SEGMENT}){{1,15}}#{_SEGMENT}$"
_SECRET_REF_RE = re.compile(SECRET_REF_PATTERN)


@dataclass(frozen=True)
class SecretRef:
    mount: str
    path: tuple[str, ...]
    key: str

    @classmethod
    def parse(cls, raw: str) -> SecretRef:
        if not isinstance(raw, str) or not _SECRET_REF_RE.fullmatch(raw):
            raise ValueError("secret reference must match openbao://<mount>/<path>#<key>")
        body, key = raw[len(SCHEME) + 3 :].split("#", 1)
        mount, *path = body.split("/")
        if any(seg in {".", ".."} or seg.startswith("..") for seg in [mount, *path, key]):
            raise ValueError("secret reference contains a relative path segment")
        return cls(mount=mount, path=tuple(path), key=key)

    def __str__(self) -> str:
        return f"{SCHEME}://{self.mount}/{'/'.join(self.path)}#{self.key}"


class SecretResolver(Protocol):
    def resolve(self, ref: SecretRef) -> str: ...

    def ready(self) -> bool: ...


class UnconfiguredSecretResolver:
    """Used when no secrets directory is configured: every resolution fails closed."""

    def resolve(self, ref: SecretRef) -> str:
        raise SecretUnavailable("No secret backend is configured")

    def ready(self) -> bool:
        return False


class FileSecretResolver:
    def __init__(self, root: Path, max_bytes: int = 4096) -> None:
        self._root = root.resolve()
        self._max_bytes = max_bytes

    def ready(self) -> bool:
        return self._root.is_dir() and os.access(self._root, os.R_OK | os.X_OK)

    def resolve(self, ref: SecretRef) -> str:
        candidate = self._root.joinpath(ref.mount, *ref.path, ref.key)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise SecretUnavailable() from None
        if not resolved.is_relative_to(self._root) or not resolved.is_file():
            raise SecretUnavailable()
        try:
            with resolved.open("rb") as fh:
                data = fh.read(self._max_bytes + 1)
        except OSError:
            raise SecretUnavailable() from None
        if len(data) > self._max_bytes:
            raise SecretUnavailable("Secret value exceeds the configured size limit")
        try:
            value = data.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise SecretUnavailable("Secret value is not valid UTF-8") from None
        if not value or any(ch in value for ch in "\r\n\x00"):
            raise SecretUnavailable("Secret value is empty or malformed")
        return value
