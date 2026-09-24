"""Credential redaction for any text that may leave the process (logs, errors, API bodies)."""

from __future__ import annotations

import re
from collections.abc import Iterable

# scheme://user:password@host -> scheme://***@host
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]*@")
_PASSWORD_ELEMENT = re.compile(r"(<(?:\w+:)?Password\b[^>]*>)[^<]*(</(?:\w+:)?Password>)")
REDACTED = "***"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove URL userinfo, WS-Security password elements and any known secret values."""
    out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    out = _PASSWORD_ELEMENT.sub(rf"\1{REDACTED}\2", out)
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, REDACTED)
    return out
