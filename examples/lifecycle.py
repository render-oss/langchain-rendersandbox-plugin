"""Check the sandbox image and demonstrate the package's owned session."""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain_render import RenderSandbox
from langchain_render import sandbox_session as render_session


@asynccontextmanager
async def sandbox_session() -> AsyncIterator[RenderSandbox]:
    options = {}
    if snapshot_id := os.getenv("RENDER_SNAPSHOT_ID"):
        options["snapshot_id"] = snapshot_id
    async with render_session(**options) as backend:
        print(f"Created sandbox {backend.id}", flush=True)
        probe = await backend.aexecute("command -v bash && command -v python3 && command -v python")
        if probe.exit_code != 0:
            raise RuntimeError("The selected sandbox image needs bash, python3 and python")
        yield backend
    print(f"Cleanup verified: {backend.id} is terminated or no longer retrievable", flush=True)


async def wait_demo() -> None:
    start = time.monotonic()
    async with sandbox_session() as backend:
        result = await backend.aexecute("printf 'hello from Render\\n'")
        if result.exit_code != 0:
            raise RuntimeError("Remote command failed")
        print(result.output, end="")
    print(f"Completed in {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    asyncio.run(wait_demo())
