"""Bounded subprocess execution for ffmpeg/ffprobe: hard timeout, output cap, always reaped."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from typing import Protocol

from camera_gateway.errors import CameraProtocolError, CameraTimeout

_STDERR_CAP = 64 * 1024


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: str


class ProcessRunner(Protocol):
    async def run(self, argv: list[str], timeout_s: float, max_stdout: int) -> ProcessResult: ...

    def available(self, binary: str) -> bool: ...


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf), False
        if len(buf) + len(chunk) > cap:
            return bytes(buf), True
        buf.extend(chunk)


async def terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)


class AsyncProcessRunner:
    async def run(self, argv: list[str], timeout_s: float, max_stdout: int) -> ProcessResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        try:
            async with asyncio.timeout(timeout_s):
                (stdout, overflow), (stderr, _) = await asyncio.gather(
                    _read_capped(proc.stdout, max_stdout), _read_capped(proc.stderr, _STDERR_CAP)
                )
                if overflow:
                    raise CameraProtocolError("Camera output exceeded the configured size limit")
                returncode = await proc.wait()
        except TimeoutError:
            raise CameraTimeout() from None
        finally:
            await terminate(proc)
        return ProcessResult(returncode, stdout, stderr.decode("utf-8", "replace"))

    def available(self, binary: str) -> bool:
        return shutil.which(binary) is not None
