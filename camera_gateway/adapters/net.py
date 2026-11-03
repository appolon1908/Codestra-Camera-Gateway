"""Small network helpers shared by the protocol adapters."""

from __future__ import annotations

import ipaddress
from urllib.parse import quote


def format_host(host: str) -> str:
    """Bracket IPv6 literals for use in URLs."""
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    except ValueError:
        pass
    return host


def build_rtsp_url(
    host: str, port: int, path: str, username: str | None = None, password: str | None = None
) -> str:
    userinfo = ""
    if username is not None:
        userinfo = quote(username, safe="")
        if password is not None:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    return f"rtsp://{userinfo}{format_host(host)}:{port}{path}"
