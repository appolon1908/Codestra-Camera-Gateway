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


async def _read_capped(stream: asyncio.StreamReader, cap: int, *, fail_on_overflow: bool) -> bytes:
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        remaining = cap - len(buf)
        if len(chunk) > remaining and fail_on_overflow:
            raise CameraProtocolError("Camera output exceeded the configured size limit")
        buf.extend(chunk[:remaining])
        # Stderr is drained after truncation so a verbose child cannot block on its pipe.


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
        readers = [
            asyncio.create_task(_read_capped(proc.stdout, max_stdout, fail_on_overflow=True)),
            asyncio.create_task(_read_capped(proc.stderr, _STDERR_CAP, fail_on_overflow=False)),
        ]
        try:
            async with asyncio.timeout(timeout_s):
                stdout, stderr = await asyncio.gather(*readers)
                returncode = await proc.wait()
        except TimeoutError:
            raise CameraTimeout() from None
        finally:
            await terminate(proc)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        return ProcessResult(returncode, stdout, stderr.decode("utf-8", "replace"))

    def available(self, binary: str) -> bool:
        return shutil.which(binary) is not None
