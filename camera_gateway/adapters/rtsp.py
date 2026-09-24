"""Vendor-neutral RTSP operations implemented with ffmpeg/ffprobe.

Credentials are embedded in the RTSP URL only for the lifetime of the child process and
are redacted from every piece of child output before it is logged or classified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass

from camera_gateway.adapters.process import ProcessRunner, terminate
from camera_gateway.errors import (
    CameraAuthFailed,
    CameraProtocolError,
    CameraStreamNotFound,
    CameraTimeout,
    CameraUnreachable,
    UpstreamError,
)
from camera_gateway.models import MediaInfo, RtspTransport
from camera_gateway.redaction import redact

log = logging.getLogger(__name__)

_UNREACHABLE = (
    "connection refused",
    "no route to host",
    "network is unreachable",
    "host is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "cannot assign requested address",
)
_TIMEOUT = ("timed out", "timeout")
_AUTH = ("401 unauthorized", "403 forbidden", "authorization failed")
_NOT_FOUND = ("404 not found", "454 session not found")


def classify_ffmpeg_error(stderr: str) -> UpstreamError:
    """Map (already redacted) ffmpeg stderr to a fail-closed upstream error."""
    text = stderr.lower()
    if any(s in text for s in _AUTH):
        return CameraAuthFailed()
    if any(s in text for s in _NOT_FOUND):
        return CameraStreamNotFound()
    if any(s in text for s in _UNREACHABLE):
        return CameraUnreachable()
    if any(s in text for s in _TIMEOUT):
        return CameraTimeout()
    return CameraProtocolError()


@dataclass(frozen=True)
class RtspTarget:
    url: str  # contains credentials; never log
    transport: RtspTransport
    secrets: tuple[str, ...] = ()

    def redacted(self, text: str) -> str:
        return redact(text, self.secrets)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Return (width, height) from a baseline/progressive JPEG, or raise CameraProtocolError."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise CameraProtocolError("Camera frame is not a JPEG image")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > len(data):
                break
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            if width and height:
                return width, height
            break
        i += 2 + length
    raise CameraProtocolError("Camera frame has no decodable dimensions")


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate in {"0/0", "0"}:
        return None
    try:
        num, _, den = rate.partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value > 0 else None


class RtspClient:
    def __init__(
        self,
        runner: ProcessRunner,
        *,
        ffmpeg: str,
        ffprobe: str,
        timeout_s: float,
        snapshot_max_bytes: int,
    ) -> None:
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout_s = timeout_s
        self._snapshot_max_bytes = snapshot_max_bytes

    def _input_args(self, target: RtspTarget) -> list[str]:
        return [
            "-rtsp_transport",
            target.transport.value,
            # Socket I/O timeout in microseconds (ffmpeg >= 5).
            "-timeout",
            str(int(self._timeout_s * 1_000_000)),
            "-i",
            target.url,
        ]

    async def probe(self, target: RtspTarget) -> MediaInfo:
        argv = [
            self._ffprobe,
            "-hide_banner",
            # "warning" is required: ffmpeg only reports socket timeouts at this level.
            "-v",
            "warning",
            "-analyzeduration",
            "3000000",
            "-probesize",
            "2000000",
            *self._input_args(target),
            "-select_streams",
            "v:0",
            "-show_streams",
            "-of",
            "json",
        ]
        result = await self._runner.run(argv, self._timeout_s + 2, 1024 * 1024)
        stderr = target.redacted(result.stderr)
        if result.returncode != 0:
            log.warning("rtsp probe failed", extra={"ffmpeg_stderr": stderr[-2000:]})
            raise classify_ffmpeg_error(stderr)
        try:
            doc = json.loads(result.stdout or b"{}")
            streams = doc.get("streams") or []
        except (ValueError, AttributeError):
            raise CameraProtocolError("ffprobe returned malformed metadata") from None
        video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
        if video is None:
            raise CameraProtocolError("RTSP stream contains no video track")
        return MediaInfo(
            codec=video.get("codec_name"),
            profile=video.get("profile"),
            width=video.get("width") or None,
            height=video.get("height") or None,
            fps=_parse_fps(video.get("avg_frame_rate")) or _parse_fps(video.get("r_frame_rate")),
            pixel_format=video.get("pix_fmt"),
        )

    async def snapshot(self, target: RtspTarget) -> tuple[bytes, int, int]:
        argv = [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            *self._input_args(target),
            "-an",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
        result = await self._runner.run(argv, self._timeout_s + 5, self._snapshot_max_bytes)
        stderr = target.redacted(result.stderr)
        if result.returncode != 0 or not result.stdout:
            log.warning("rtsp snapshot failed", extra={"ffmpeg_stderr": stderr[-2000:]})
            raise classify_ffmpeg_error(stderr)
        width, height = jpeg_dimensions(result.stdout)
        return result.stdout, width, height

    async def mjpeg(
        self, target: RtspTarget, *, fps: int, max_width: int, duration_s: int
    ) -> AsyncIterator[bytes]:
        """Yield a multipart MJPEG byte stream (boundary 'frame'). Caller bounds concurrency."""
        argv = [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            *self._input_args(target),
            "-an",
            "-t",
            str(duration_s),
            "-vf",
            f"fps={fps},scale=w='min({max_width},iw)':h=-2",
            "-c:v",
            "mjpeg",
            "-q:v",
            "7",
            "-f",
            "mpjpeg",
            "-boundary_tag",
            "frame",
            "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(duration_s + self._timeout_s + 5):
                while chunk := await proc.stdout.read(65536):
                    yield chunk
        except TimeoutError:
            return
        finally:
            await terminate(proc)
