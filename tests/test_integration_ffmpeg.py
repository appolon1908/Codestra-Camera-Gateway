"""Real ffmpeg/ffprobe against local fake RTSP endpoints (no camera required)."""

from __future__ import annotations

import asyncio
import shutil
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import pytest

from camera_gateway.adapters.net import build_rtsp_url
from camera_gateway.adapters.process import AsyncProcessRunner
from camera_gateway.adapters.rtsp import RtspClient, RtspTarget
from camera_gateway.errors import GatewayError
from camera_gateway.models import RtspTransport
from tests.fakes import SECRET

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
    ),
]

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader) -> str:
    data = await reader.readuntil(b"\r\n\r\n")
    return data.decode(errors="replace")


def _cseq(request: str) -> str:
    for line in request.split("\r\n"):
        if line.lower().startswith("cseq:"):
            return line.split(":", 1)[1].strip()
    return "1"


async def unauthorized(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            req = await _read_request(reader)
            cseq = _cseq(req)
            if req.startswith("OPTIONS"):
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: DESCRIBE\r\n\r\n".encode())
            else:
                response = (
                    f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n'
                    'WWW-Authenticate: Basic realm="cam"\r\n\r\n'
                )
                writer.write(response.encode())
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()


async def garbage(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>not rtsp</html>\r\n\r\n")
    await writer.drain()
    writer.close()


async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await asyncio.sleep(30)


async def _run(handler: Handler | None, op: str, timeout_s: float = 2) -> GatewayError:
    server = None
    if handler is None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens here any more
    else:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
    client = RtspClient(
        AsyncProcessRunner(),
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        timeout_s=timeout_s,
        snapshot_max_bytes=10**6,
    )
    target = RtspTarget(
        url=build_rtsp_url("127.0.0.1", port, "/live", "admin", SECRET),
        transport=RtspTransport.tcp,
        secrets=(SECRET, quote(SECRET, safe="")),
    )
    try:
        with pytest.raises(GatewayError) as info:
            if op == "clip":
                await client.clip(target, duration_s=1, max_bytes=1024 * 1024)
            else:
                await (client.probe(target) if op == "probe" else client.snapshot(target))
        return info.value
    finally:
        if server is not None:
            server.close()


@pytest.mark.parametrize("op", ["probe", "snapshot", "clip"])
@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (None, "camera_unreachable"),
        (unauthorized, "camera_auth_failed"),
        (garbage, "camera_protocol_error"),
        (silent, "camera_timeout"),
    ],
)
def test_real_ffmpeg_failure_classification(
    handler: Handler | None, code: str, op: str, caplog: pytest.LogCaptureFixture
) -> None:
    error = asyncio.run(_run(handler, op, timeout_s=1))
    assert error.code == code
    for record in caplog.records:
        rendered = record.getMessage() + str(record.__dict__)
        assert SECRET not in rendered and quote(SECRET, safe="") not in rendered
