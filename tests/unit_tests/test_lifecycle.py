"""Exercise lifecycle cleanup without provisioning any remote resource."""

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from render.client.errors import RenderError

from langchain_render import RenderSandboxError


@pytest.fixture
def lifecycle(monkeypatch):
    return importlib.import_module("langchain_render.session")


class FakeSandboxes:
    def __init__(self):
        self.status = "running"
        self.terminated = False
        self.create_args = None
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = None
        self.cleanup_error = None

    async def create(self, **kwargs):
        self.create_args = kwargs
        return SimpleNamespace(id="sandbox-fixture", status=self.status)

    async def terminate(self, sandbox_id):
        assert sandbox_id == "sandbox-fixture"
        self.cleanup_started.set()
        if self.cleanup_release:
            await self.cleanup_release.wait()
        if self.cleanup_error:
            raise self.cleanup_error
        self.terminated = True

    async def from_id(self, sandbox_id):
        assert sandbox_id == "sandbox-fixture"
        return SimpleNamespace(
            id=sandbox_id, status="terminated" if self.terminated else self.status
        )


@pytest.fixture
def remote(lifecycle, monkeypatch):
    remote = FakeSandboxes()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(
        lifecycle,
        "RenderAsync",
        lambda **kwargs: SimpleNamespace(
            client=SimpleNamespace(internal=Connection()),
            experimental=SimpleNamespace(sandboxes=remote),
        ),
    )
    return remote


async def test_lifecycle_uses_finite_private_sandbox_and_observes_cleanup(lifecycle, remote):
    async with lifecycle.sandbox_session() as backend:
        assert not remote.terminated
        assert backend.id == "sandbox-fixture"
    assert remote.create_args["timeout_seconds"] == 900
    assert remote.create_args["network_policy"] == "deny-all"
    assert remote.terminated
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("must not run")


async def test_failed_readiness_still_terminates(lifecycle, remote):
    remote.status = "errored"
    with pytest.raises(RuntimeError, match="entered errored"):
        async with lifecycle.sandbox_session():
            pytest.fail("failed sandbox must not be yielded")
    assert remote.terminated


async def test_agent_error_still_terminates(lifecycle, remote):
    with pytest.raises(ValueError, match="agent failed"):
        async with lifecycle.sandbox_session():
            raise ValueError("agent failed")
    assert remote.terminated


async def test_repeated_cancellation_cannot_interrupt_bounded_cleanup(lifecycle, remote):
    remote.cleanup_release = asyncio.Event()
    entered = asyncio.Event()

    async def run():
        async with lifecycle.sandbox_session():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await remote.cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    remote.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert remote.terminated


async def test_cleanup_failure_retains_id_without_claiming_success(lifecycle, remote):
    remote.cleanup_error = RuntimeError("transport failed")
    with pytest.raises(RenderSandboxError, match="Retain sandbox ID sandbox-fixture") as error:
        async with lifecycle.sandbox_session():
            pass
    assert str(error.value.__cause__) == "transport failed"


async def test_creation_options_reach_sdk_and_auth_stays_on_host(lifecycle, remote):
    async with lifecycle.sandbox_session(
        token="test-token",
        owner_id="test-workspace",
        timeout_seconds=600,
        env={"TASK": "repair"},
        snapshot_id="snapshot-fixture",
        plan="test-plan",
        region="test-region",
    ) as backend:
        assert backend.id == "sandbox-fixture"
    assert remote.create_args == {
        "timeout_seconds": 600,
        "network_policy": "deny-all",
        "env": {"TASK": "repair"},
        "snapshot_id": "snapshot-fixture",
        "plan": "test-plan",
        "region": "test-region",
    }


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"snapshot_id": "id", "snapshot_name": "name"},
    ],
)
async def test_invalid_creation_options_fail_before_resource(lifecycle, remote, options):
    with pytest.raises(ValueError):
        async with lifecycle.sandbox_session(**options):
            pytest.fail("must not enter")
    assert remote.create_args is None


async def test_ambiguous_creation_is_not_replayed(lifecycle, remote, monkeypatch):
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("response lost")

    monkeypatch.setattr(remote, "create", create)
    with pytest.raises(TimeoutError) as error:
        async with lifecycle.sandbox_session():
            pytest.fail("must not enter")
    assert calls == 1 and not remote.terminated
    assert "Inspect the workspace before retrying" in error.value.__notes__[0]


@pytest.mark.parametrize("status", [401, 403])
async def test_sdk_auth_and_workspace_rejections_are_not_retried_or_hidden(monkeypatch, status):
    import httpx
    from render.client.errors import ClientError

    from langchain_render import sandbox_session

    requests = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"message": "access denied"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(ClientError):
        async with sandbox_session(
            token="synthetic-test-key", owner_id="tea-test", base_url="https://api.render.test"
        ):
            pytest.fail("Rejected creation must not yield a backend")
    assert len(requests) == 1
    assert requests[0].method == "POST"


@pytest.mark.parametrize(
    "missing,error_type", [("RENDER_API_KEY", ValueError), ("RENDER_WORKSPACE_ID", RenderError)]
)
async def test_missing_credentials_fail_before_http(monkeypatch, missing, error_type):
    import httpx

    from langchain_render import sandbox_session

    monkeypatch.setenv("RENDER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("RENDER_WORKSPACE_ID", "tea-test")
    monkeypatch.delenv(missing)
    original_client = httpx.AsyncClient

    def reject_network(request):
        pytest.fail("Missing configuration must not make an HTTP request")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(reject_network), **kwargs),
    )
    with pytest.raises(error_type, match="token|owner_id"):
        async with sandbox_session():
            pytest.fail("Missing configuration must not yield a backend")
