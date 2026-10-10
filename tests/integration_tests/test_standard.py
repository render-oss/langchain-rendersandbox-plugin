"""Unmodified LangChain standard tests against a real, disposable Render sandbox."""

import os
import time
from collections.abc import Iterator

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_tests.integration_tests import SandboxIntegrationTests
from render import Render
from render.experimental.sandbox.errors import SandboxNotFoundError

from langchain_render import RenderSandbox


@pytest.mark.live
class TestRenderSandboxStandard(SandboxIntegrationTests):
    @pytest.fixture(scope="class")
    def sandbox(self) -> Iterator[SandboxBackendProtocol]:
        if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
            pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 to create a billable Render sandbox")
        if not os.getenv("RENDER_API_KEY") or not os.getenv("RENDER_WORKSPACE_ID"):
            pytest.fail("RENDER_API_KEY and RENDER_WORKSPACE_ID are required")
        sdk = Render()
        with sdk.client.internal:
            sandboxes = sdk.experimental.sandboxes
            sandbox = sandboxes.create(
                timeout_seconds=1800,
                network_policy="deny-all",
                snapshot_id=os.getenv("RENDER_SNAPSHOT_ID") or None,
            )
            print(f"Created live test sandbox: {sandbox.id}", flush=True)
            try:
                deadline = time.monotonic() + 120
                while sandbox.status != "running":
                    if sandbox.status in {"terminated", "errored"}:
                        pytest.fail(f"Sandbox failed to start: {sandbox.id} ({sandbox.status})")
                    if time.monotonic() >= deadline:
                        pytest.fail(f"Sandbox readiness timed out: {sandbox.id}")
                    time.sleep(1)
                    sandbox = sandboxes.from_id(sandbox.id)
                backend = RenderSandbox(sandbox_id=sandbox.id)
                probe = backend.execute(
                    "command -v bash && command -v python3 && command -v python"
                )
                assert probe.exit_code == 0, "The sandbox image needs bash, python3 and python"
                yield backend
            finally:
                sandboxes.terminate(sandbox.id)
                try:
                    state = sandboxes.from_id(sandbox.id)
                except SandboxNotFoundError:
                    print(f"Termination verified: {sandbox.id} (no longer retrievable)", flush=True)
                else:
                    assert state.status == "terminated", (
                        f"Cleanup unverified: {sandbox.id} ({state.status})"
                    )
                    print(f"Termination verified: {sandbox.id} ({state.status})", flush=True)
