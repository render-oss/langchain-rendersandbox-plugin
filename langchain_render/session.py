"""An optional owned session around the Render SDK's sandbox lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TypedDict, Unpack

from render import RenderAsync
from render.experimental.sandbox.errors import SandboxNotFoundError

from langchain_render.sandbox import RenderSandbox, RenderSandboxError, _seconds


class SandboxOptions(TypedDict, total=False):
    """Options forwarded to Render's create API; availability depends on Render."""

    timeout_seconds: int
    network_policy: str
    env: dict[str, str]
    plan: str
    region: str
    snapshot_id: str
    snapshot_name: str


@asynccontextmanager
async def sandbox_session(
    *,
    token: str | None = None,
    owner_id: str | None = None,
    base_url: str = "https://api.render.com",
    **options: Unpack[SandboxOptions],
) -> AsyncGenerator[RenderSandbox, None]:
    """Create a ready backend and terminate its sandbox when the block exits.

    Defaults to a 900-second lifetime and deny-all outbound networking. Other
    creation options pass directly to the SDK. Credentials authenticate your
    application; only explicitly supplied ``env`` values enter the sandbox.

    Readiness is bounded to 120 seconds, creation and cleanup to 30 seconds each.
    Cleanup is verified even after an exception or repeated cancellation. Download
    wanted files before leaving the block. For application-managed lifetimes or
    advanced operations, use the Render SDK and ``RenderSandbox(sandbox_id=...)``.
    """
    values: SandboxOptions = {"timeout_seconds": 900, "network_policy": "deny-all", **options}
    if not _seconds(values["timeout_seconds"], "timeout_seconds"):
        raise ValueError("timeout_seconds must be positive")
    if values.get("snapshot_id") is not None and values.get("snapshot_name") is not None:
        raise ValueError("Choose snapshot_id or snapshot_name, not both")
    sdk = RenderAsync(token=token, owner_id=owner_id, base_url=base_url)
    async with sdk.client.internal:
        sandboxes = sdk.experimental.sandboxes
        try:
            async with asyncio.timeout(30):
                sandbox = await sandboxes.create(**values)
        except BaseException as exc:
            # A mutation with an unknown outcome must not be retried automatically.
            exc.add_note(
                "Creation did not return a sandbox ID. Inspect the workspace before retrying; "
                "the request may have created a resource with the configured finite lifetime."
            )
            raise

        async def cleanup() -> None:
            async with asyncio.timeout(30):
                try:
                    await sandboxes.terminate(sandbox.id)
                except SandboxNotFoundError:
                    return
                while True:
                    try:
                        state = await sandboxes.from_id(sandbox.id)
                    except SandboxNotFoundError:
                        return
                    if state.status == "terminated":
                        return
                    await asyncio.sleep(1)

        backend = None
        try:
            async with asyncio.timeout(120):
                while sandbox.status != "running":
                    if sandbox.status in {"errored", "terminated"}:
                        raise RuntimeError(f"Sandbox {sandbox.id} entered {sandbox.status}")
                    await asyncio.sleep(1)
                    sandbox = await sandboxes.from_id(sandbox.id)
            backend = RenderSandbox(
                sandbox_id=sandbox.id, token=token, owner_id=owner_id, base_url=base_url
            )
            yield backend
        finally:
            if backend is not None:
                backend._failed = True
            cleanup_task = asyncio.create_task(cleanup())
            cancelled = False
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                cleanup_task.result()
            except Exception as exc:
                raise RenderSandboxError(
                    f"Cleanup not verified. Retain sandbox ID {sandbox.id} and retry termination."
                ) from exc
            if cancelled:
                raise asyncio.CancelledError
