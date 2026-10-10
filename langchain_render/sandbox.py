"""A Deep Agents backend using the public Render Sandboxes SDK."""

from __future__ import annotations

import asyncio
import json
import posixpath
import random
import re
import shlex
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypeVar

from deepagents.backends.protocol import (
    DeleteResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
)
from deepagents.backends.sandbox import BaseSandbox
from render import RenderAsync
from render.client.errors import ClientError, RateLimitError
from render.experimental.sandbox.errors import SandboxFileNotFoundError
from render.experimental.sandbox.types import SandboxExecExit, SandboxExecOutput

_T = TypeVar("_T")
_FILE_ERRORS = {
    "file_not_found": "file_not_found",
    "permission_denied": "permission_denied",
    "path_not_readable": "permission_denied",
    "path_not_writable": "permission_denied",
    "is_directory": "is_directory",
    "is_a_directory": "is_directory",
    "not_a_directory": "invalid_path",
    "invalid_path": "invalid_path",
}
_RENDER_120 = version("render") == "1.2.0"
_RATE_LIMIT_RETRIES = 6


async def _rate_limit_pause(attempt: int) -> None:
    # The public SDK does not expose Retry-After on RateLimitError. Back off
    # across a one-minute quota window; the caller's deadline still bounds us.
    await asyncio.sleep(min(2**attempt, 30) * random.uniform(1.0, 1.25))


class RenderSandboxError(RuntimeError):
    """Infrastructure failure; stop this run and clean up its sandbox.

    A disconnected or timed-out exec stream does not cancel the remote process.
    The application that owns the sandbox must terminate it through the Render SDK.
    """


def _sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Bridge sync callers, including notebook cells, without reusing event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Notebook users may call sync methods from a running event loop. Async
    # agent paths use the native async methods and never enter this bridge.
    with ThreadPoolExecutor(max_workers=1) as pool:

        def run() -> _T:
            return asyncio.run(coro)

        return pool.submit(run).result()


def _seconds(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _path(path: str) -> str | None:
    if not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
        return None
    return posixpath.normpath(path)


async def _disk_call(function: Callable[..., _T], *args: Any) -> _T:
    """Join local file work before cancellation can remove its temporary directory."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve any error so a canceled file operation cannot leave an
        # unobserved task exception. The caller's cancellation takes precedence.
        if not task.cancelled():
            task.exception()
        raise


def _file_error(exc: Exception) -> str | None:
    if isinstance(exc, SandboxFileNotFoundError):
        return "file_not_found"
    if not isinstance(exc, ClientError):
        return None
    if exc.code in _FILE_ERRORS:
        return _FILE_ERRORS[exc.code]
    # Render 1.2.0 drops structured codes for transfer 400/403 responses.
    # Decode only its exact SDK envelope and an allow-listed JSON error code.
    # Auth, throttling, malformed bodies and other releases stay fatal.
    if _RENDER_120 and type(exc) is ClientError and exc.code is None:
        match = re.fullmatch(
            r"(?:upload|download) failed with status (?:400|403): (.+)", str(exc), re.DOTALL
        )
        if match:
            try:
                body = json.loads(match[1])
            except (ValueError, TypeError):
                return None
            if isinstance(body, dict) and isinstance(body.get("code"), str):
                return _FILE_ERRORS.get(body["code"])
    return None


class RenderSandbox(BaseSandbox):
    """Wrap an existing Render sandbox for Deep Agents.

    Implements Deep Agents' BaseSandbox contract. The application creates,
    waits for, and terminates the sandbox with the Render SDK. This backend does
    not automatically terminate a borrowed resource, even on failure.

    Args:
        sandbox_id: ID of an existing, running Render sandbox.
        token: Render API key, or use RENDER_API_KEY.
        owner_id: Workspace owning the sandbox, or use RENDER_WORKSPACE_ID.
        base_url: Render API origin, normally https://api.render.com.
        timeout: Default wall-clock command deadline in seconds. Zero disables
            the client deadline; the sandbox's own lifetime still applies.
        file_timeout: Network-operation deadline for each file transfer, in seconds.
            Must be positive. Local filesystem cleanup can outlast this deadline.
        max_output_chars: Maximum retained characters of combined stdout/stderr.
            Excess output is drained while awaiting the real exit event.

    A timeout, cancellation, or execution transport failure makes this backend
    unusable. Render may still be running the remote command. Stop the agent and
    terminate the owned sandbox in a finally block. Ordinary nonzero process exits
    are returned as ExecuteResponse so the agent can repair its code.
    """

    def __init__(
        self,
        *,
        sandbox_id: str,
        token: str | None = None,
        owner_id: str | None = None,
        base_url: str = "https://api.render.com",
        timeout: int = 300,
        file_timeout: int = 300,
        max_output_chars: int = 2 * 1024 * 1024,
    ) -> None:
        if not sandbox_id or not sandbox_id.strip():
            raise ValueError("sandbox_id must not be empty")
        self._timeout = _seconds(timeout, "timeout")
        self._file_timeout = _seconds(file_timeout, "file_timeout")
        if self._file_timeout == 0:
            raise ValueError("file_timeout must be positive")
        if (
            isinstance(max_output_chars, bool)
            or not isinstance(max_output_chars, int)
            or max_output_chars < 1
        ):
            raise ValueError("max_output_chars must be a positive integer")
        self._id = sandbox_id
        self._token = token
        self._owner_id = owner_id
        self._base_url = base_url
        self._max_output_chars = max_output_chars
        self._failed = False

    @property
    def id(self) -> str:
        """Return the Render sandbox ID."""
        return self._id

    def _client(self) -> RenderAsync:
        # Each operation owns a client and its connections. This also keeps
        # sync calls, notebook calls and different async loops independent.
        return RenderAsync(token=self._token, owner_id=self._owner_id, base_url=self._base_url)

    def _check_usable(self) -> None:
        if self._failed:
            raise RenderSandboxError(
                f"Sandbox backend {self.id} is unusable after an earlier infrastructure "
                "failure or cancellation. Terminate the owned sandbox and start a new run."
            )

    def _failure(self, operation: str) -> RenderSandboxError:
        self._failed = True
        return RenderSandboxError(
            f"Render sandbox {self.id}: {operation} did not complete reliably. "
            "Remote work may still be running. Stop the agent and terminate the owned sandbox."
        )

    async def _retry_rate_limit(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        safe_to_retry: Callable[[], bool] = lambda: True,
    ) -> _T:
        """Retry explicit HTTP 429 rejections, never ambiguous transport failures."""
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            self._check_usable()
            try:
                return await operation()
            except RateLimitError:
                if attempt == _RATE_LIMIT_RETRIES or not safe_to_retry():
                    raise
                await _rate_limit_pause(attempt)
        raise AssertionError("Rate-limit retry loop must return or raise")

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run Bash remotely, returning combined output and the actual exit code."""
        return _sync(self.aexecute(command, timeout=timeout))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run Bash with native asynchronous streaming and a wall-clock deadline."""
        self._check_usable()
        effective_timeout = self._timeout if timeout is None else _seconds(timeout, "timeout")
        chunks: list[str] = []
        retained = 0
        truncated = False
        exit_code: int | None = None
        received_event = False

        async def attempt() -> None:
            nonlocal retained, truncated, exit_code, received_event
            sdk = self._client()
            async with sdk.client.internal:
                stream = sdk.experimental.sandboxes.exec(self.id, command)
                try:
                    async for event in stream:
                        received_event = True
                        if isinstance(event, SandboxExecOutput):
                            remaining = self._max_output_chars - retained
                            chunk = event.data[:remaining]
                            if chunk:
                                chunks.append(chunk)
                            retained += len(chunk)
                            truncated = truncated or len(chunk) < len(event.data)
                        elif isinstance(event, SandboxExecExit):
                            exit_code = event.exit_code
                        else:
                            raise RuntimeError("Unexpected Render execution event")
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await close()

        try:
            async with asyncio.timeout(effective_timeout or None):
                # In the public SDK, RateLimitError is an HTTP rejection before
                # the exec stream starts. A stream error or any received event
                # makes replay unsafe: never run an uncertain command twice.
                await self._retry_rate_limit(attempt, safe_to_retry=lambda: not received_event)
            if exit_code is None:
                raise RuntimeError("Render stream ended without an exit event")
        except asyncio.CancelledError:
            self._failed = True
            raise
        except Exception as exc:
            raise self._failure("command execution") from exc
        return ExecuteResponse(output="".join(chunks), exit_code=exit_code, truncated=truncated)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload bytes through the SDK, preserving input order and per-file errors."""
        return _sync(self.aupload_files(files))

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload bytes asynchronously without placing API credentials in the sandbox."""
        self._check_usable()
        results: list[FileUploadResponse] = []
        for path, content in files:
            self._check_usable()
            remote = _path(path)
            if remote is None:
                results.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            try:
                async with asyncio.timeout(self._file_timeout):
                    with TemporaryDirectory(prefix="langchain-render-") as local_dir:
                        local = Path(local_dir) / "upload"
                        await _disk_call(local.write_bytes, content)
                        sdk = self._client()
                        async with sdk.client.internal:
                            await self._retry_rate_limit(
                                partial(sdk.experimental.sandboxes.copy_to, self.id, local, remote)
                            )
            except asyncio.CancelledError:
                self._failed = True
                raise
            except Exception as exc:
                error = _file_error(exc)
                if error is None:
                    raise self._failure("file upload") from exc
                results.append(FileUploadResponse(path=path, error=error))
            else:
                results.append(FileUploadResponse(path=path, error=None))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files as bytes; directory downloads are not part of this contract."""
        return _sync(self.adownload_files(paths))

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files asynchronously through the SDK's validated file transport."""
        self._check_usable()
        results: list[FileDownloadResponse] = []
        for path in paths:
            self._check_usable()
            remote = _path(path)
            if remote is None:
                results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                continue
            # Avoid downloading and expanding a directory archive just to report
            # that this API returns file bytes. The SDK remains the transfer owner.
            probe = await self.aexecute(f"test -d {shlex.quote(remote)}")
            if probe.exit_code == 0:
                results.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
                continue
            try:
                async with asyncio.timeout(self._file_timeout):
                    with TemporaryDirectory(prefix="langchain-render-") as local_dir:
                        local = Path(local_dir) / "download"
                        # A regular-file destination prevents the SDK from
                        # extracting an unexpected directory archive, even if
                        # the remote path changed after our directory probe.
                        await _disk_call(local.touch)
                        sdk = self._client()
                        async with sdk.client.internal:
                            written = await self._retry_rate_limit(
                                partial(
                                    sdk.experimental.sandboxes.copy_from, self.id, remote, local
                                )
                            )
                        written_path = Path(written)
                        if await _disk_call(written_path.is_dir):
                            results.append(
                                FileDownloadResponse(path=path, content=None, error="is_directory")
                            )
                            continue
                        content = await _disk_call(written_path.read_bytes)
            except asyncio.CancelledError:
                self._failed = True
                raise
            except Exception as exc:
                error = _file_error(exc)
                if error is None:
                    raise self._failure("file download") from exc
                results.append(FileDownloadResponse(path=path, content=None, error=error))
            else:
                results.append(FileDownloadResponse(path=path, content=content, error=None))
        return results

    async def adelete(self, file_path: str) -> DeleteResult:
        """Delete a remote path with the same semantics as BaseSandbox.delete."""
        quoted = shlex.quote(file_path)
        exists = await self.aexecute(f"test -e {quoted} || test -L {quoted}")
        if exists.exit_code is not None and exists.exit_code != 0:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        result = await self.aexecute(f"rm -rf {quoted}")
        if result.exit_code == 0:
            return DeleteResult(path=file_path)
        return DeleteResult(
            error=f"Error deleting file '{file_path}': {result.output.strip() or 'unknown error'}"
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """Keep a canceled remote grep fatal even if BaseSandbox catches its timeout."""
        result = await super().agrep(pattern, path, glob, max_count=max_count)
        self._check_usable()
        return result

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Keep a canceled remote glob fatal even if BaseSandbox catches its timeout."""
        result = await super().aglob(pattern, path)
        self._check_usable()
        return result
