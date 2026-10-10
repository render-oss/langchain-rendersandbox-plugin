"""Real Render transport and agent wiring, without requiring a model-provider key."""

import asyncio
import importlib.util
import json
import os
import shlex
from pathlib import Path

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field
from render import RenderAsync
from render.experimental.sandbox.errors import SandboxNotFoundError

from langchain_render import RenderSandbox, RenderSandboxError, sandbox_session

pytestmark = pytest.mark.live
_LIFECYCLE_PATH = Path(__file__).resolve().parents[2] / "examples" / "lifecycle.py"


@pytest.fixture
async def owned_backend():
    if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 to create a billable Render sandbox")
    if not os.getenv("RENDER_API_KEY") or not os.getenv("RENDER_WORKSPACE_ID"):
        pytest.fail("RENDER_API_KEY and RENDER_WORKSPACE_ID are required")
    # Use the exact lifecycle helper readers run, including its cleanup checks.
    spec = importlib.util.spec_from_file_location("render_runtime_lifecycle", _LIFECYCLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with module.sandbox_session() as backend:
        yield backend


class ScriptedToolModel(FakeMessagesListChatModel):
    """Deterministic tool requests test real agent dispatch, not model reasoning."""

    bound_names: set[str] = Field(default_factory=set)

    def bind_tools(self, tools, **kwargs):
        self.bound_names.update(getattr(tool, "name", "") for tool in tools)
        return self


async def test_deep_agent_dispatches_file_and_shell_tools_to_render(owned_backend):
    path = "/tmp/render-agent-flow/message.txt"
    body = "hello render\n"
    calls = [
        ("write_file", {"file_path": path, "content": body}, "write-one"),
        ("read_file", {"file_path": path}, "read-one"),
        ("execute", {"command": f"cat {path}"}, "exec-one"),
    ]
    model = ScriptedToolModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
            for name, args, call_id in calls
        ]
        + [AIMessage(content="The scripted tool sequence is complete.")]
    )
    agent = create_deep_agent(model=model, backend=owned_backend)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Run the transport test."}]}
    )
    assert {"write_file", "read_file", "execute"} <= model.bound_names
    messages = {
        message.tool_call_id: message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    }
    assert {"write-one", "read-one", "exec-one"} <= messages.keys()
    assert all(messages[call_id].status == "success" for _name, _args, call_id in calls)
    assert "hello render" in str(messages["read-one"].content)
    assert "hello render" in str(messages["exec-one"].content)
    downloaded = await owned_backend.adownload_files([path])
    assert downloaded[0].error is None and downloaded[0].content == body.encode()


async def test_current_deepagents_write_and_glob_contract(owned_backend):
    root = "/tmp/render-current-contract"
    assert (await owned_backend.aexecute(f"mkdir -p {root}/subdir")).exit_code == 0
    path = f"{root}/file.txt"
    assert (await owned_backend.awrite(path, "first")).error is None
    assert (await owned_backend.awrite(path, "second")).error is None
    downloaded = await owned_backend.adownload_files([path])
    assert downloaded[0].content == b"second"
    matches = await owned_backend.aglob("*", path=root)
    assert matches.error is None
    # Current Deep Agents reports absolute file paths and excludes directories.
    assert matches.matches == [{"path": path, "is_dir": False}]


@pytest.mark.parametrize("mode", ["deadline", "cancel"])
async def test_uncertain_execution_aborts_backend_and_owned_session_cleans_up(owned_backend, mode):
    observer = RenderSandbox(sandbox_id=owned_backend.id)
    # Let any pre-existing quota window clear before starting the timed command.
    assert (await observer.aexecute("true")).exit_code == 0
    pid_path = "/tmp/render-cancellation.pid"
    task = asyncio.create_task(
        owned_backend.aexecute(
            f"echo $$ > {pid_path}; sleep 30",
            timeout=5 if mode == "deadline" else 60,
        )
    )
    try:
        async with asyncio.timeout(10):
            while True:
                if (await observer.aexecute(f"test -s {pid_path}")).exit_code == 0:
                    break
                await asyncio.sleep(0.1)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RenderSandboxError) as caught:
                await task
            assert isinstance(caught.value.__cause__, TimeoutError)
        with pytest.raises(RenderSandboxError, match="unusable"):
            await owned_backend.aexecute("must not run")
        # Closing the client stream does not stop the remote process. The
        # fixture's finally block must terminate and observe the owned sandbox.
        assert (await observer.aexecute(f"kill -0 $(cat {pid_path})")).exit_code == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_full_repair_walkthrough_with_scripted_model(monkeypatch):
    if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 to create a billable Render sandbox")
    if not os.getenv("RENDER_API_KEY") or not os.getenv("RENDER_WORKSPACE_ID"):
        pytest.fail("RENDER_API_KEY and RENDER_WORKSPACE_ID are required")
    # Import the example as a reader would run it, without changing any upstream
    # model code. Only the model's choices are scripted; all tool work is remote.
    monkeypatch.syspath_prepend(str(_LIFECYCLE_PATH.parent))
    spec = importlib.util.spec_from_file_location(
        "render_repair_walkthrough", _LIFECYCLE_PATH.parent / "repair_project.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = [
        ("read_file", {"file_path": "/tmp/render-repair/total.py"}, "inspect"),
        (
            "execute",
            {"command": "cd /tmp/render-repair && python3 -m unittest -v test_total"},
            "observe-failure",
        ),
        (
            "edit_file",
            {
                "file_path": "/tmp/render-repair/total.py",
                "old_string": "rows[:-1]",
                "new_string": "rows",
            },
            "repair",
        ),
        (
            "execute",
            {
                "command": "cd /tmp/render-repair && python3 -m unittest -v test_total && python3 total.py"
            },
            "verify-visible-test",
        ),
    ]
    model = ScriptedToolModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
            for name, args, call_id in calls
        ]
        + [AIMessage(content="The scripted repair is complete.")]
    )
    output = await module.repair_project(model)
    report = json.loads(await asyncio.to_thread((output / "report.json").read_text))
    source = await asyncio.to_thread((output / "total.py").read_text)
    assert report == {"total": 42}
    assert "for row in rows)" in source and "rows[:-1]" not in source
    assert {"read_file", "edit_file", "execute"} <= model.bound_names
    # repair_project returns only after its finally block confirms cleanup.


async def test_package_session_configuration_sdk_access_and_cleanup():
    if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 to create a billable Render sandbox")
    async with sandbox_session(timeout_seconds=600, env={"TASK": "repair"}) as backend:
        print(f"Created sandbox {backend.id}", flush=True)
        result = await backend.aexecute("printf '%s' \"$TASK\"")
        assert result.exit_code == 0 and result.output == "repair"
        sdk = RenderAsync()
        async with sdk.client.internal:
            state = await sdk.experimental.sandboxes.from_id(backend.id)
            assert state.id == backend.id and state.status == "running"
            assert state.timeout_seconds == 600 and state.network_policy == "deny-all"
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("must not run")
    sdk = RenderAsync()
    async with sdk.client.internal:
        state = await sdk.experimental.sandboxes.from_id(backend.id)
        assert state.status == "terminated"
    print(f"Cleanup independently verified: {backend.id} is terminated", flush=True)


async def test_concurrent_sessions_isolate_files_and_cleanup_after_application_error():
    if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 to create billable Render sandboxes")
    ids = []
    ready = asyncio.Event()

    class ExpectedApplicationError(Exception):
        pass

    async def run(name, fail):
        try:
            async with sandbox_session(timeout_seconds=600, env={"RUN_NAME": name}) as backend:
                ids.append(backend.id)
                print(f"Created sandbox {backend.id}", flush=True)
                uploaded = await backend.aupload_files([("/tmp/isolation.txt", name.encode())])
                assert uploaded[0].error is None
                if len(ids) == 2:
                    ready.set()
                async with asyncio.timeout(30):
                    await ready.wait()
                result = await backend.aexecute(
                    "printf '%s:' \"$RUN_NAME\"; cat /tmp/isolation.txt"
                )
                assert result.output == f"{name}:{name}" and result.exit_code == 0
                if fail:
                    raise ExpectedApplicationError("application rejected the result")
        except ExpectedApplicationError:
            return "expected application error"
        return "success"

    # gather retrieves both outcomes before asserting, allowing both cleanup paths to finish.
    results = await asyncio.gather(run("one", True), run("two", False), return_exceptions=True)
    assert len(set(ids)) == 2
    sdk = RenderAsync()
    async with sdk.client.internal:
        for sandbox_id in ids:
            state = await sdk.experimental.sandboxes.from_id(sandbox_id)
            assert state.status == "terminated"
            print(f"Cleanup independently verified: {sandbox_id} is terminated", flush=True)
    assert results == ["expected application error", "success"]


async def test_invalid_render_key_fails_without_starting_remote_work():
    if os.getenv("RUN_RENDER_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_RENDER_LIVE_TESTS=1 for a live authentication check")
    from render.client.errors import ClientError

    backend = RenderSandbox(sandbox_id="sbx-invalid-test-id", token="invalid-test-key")
    with pytest.raises(RenderSandboxError) as caught:
        await backend.aexecute("true", timeout=30)
    assert isinstance(caught.value.__cause__, ClientError)
    assert not isinstance(caught.value.__cause__, SandboxNotFoundError)


@pytest.mark.parametrize("interface", ["sync", "async"])
async def test_all_public_backend_operations(owned_backend, interface):
    """Verify every public method through both interfaces on an attached sandbox."""
    backend = RenderSandbox(sandbox_id=owned_backend.id)
    assert backend.id == owned_backend.id
    exercised = set()

    async def call(operation, *args, **kwargs):
        name = f"a{operation}" if interface == "async" else operation
        exercised.add(name)
        method = getattr(backend, name)
        if interface == "async":
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    root = f"/tmp/render-public-operations-{interface}"
    path = f"{root}/message ' with spaces.txt"
    original = "first line\nold value\n"
    repaired = "first line\nnew value\n"
    assert (await call("execute", f"mkdir -p {root}")).exit_code == 0
    assert (await call("write", path, original)).error is None
    edited = await call("edit", path, "old value", "new value")
    assert edited.error is None and edited.occurrences == 1
    read = await call("read", path)
    assert read.error is None and read.file_data is not None
    assert "new value" in read.file_data["content"]
    listing = await call("ls", root)
    assert listing.error is None and {entry["path"] for entry in listing.entries} == {path}
    found = await call("grep", "new value", path=root, glob="*.txt", max_count=1)
    assert found.error is None and len(found.matches) == 1
    assert found.matches[0]["path"] == path and found.matches[0]["line"] == 2
    matched = await call("glob", "*.txt", path=root)
    assert matched.error is None and matched.matches == [{"path": path, "is_dir": False}]

    binary_path = f"{root}/payload.bin"
    binary = bytes(range(256))
    uploaded = await call("upload_files", [(binary_path, binary)])
    assert uploaded[0].error is None
    files = await call("download_files", [path, binary_path])
    assert [item.path for item in files] == [path, binary_path]
    assert all(item.error is None for item in files)
    assert [item.content for item in files] == [repaired.encode(), binary]

    deleted = await call("delete", path)
    assert deleted.error is None and deleted.path == path
    assert (await call("download_files", [path]))[0].error == "file_not_found"
    missing = await call("delete", path)
    assert missing.error is not None and "not found" in missing.error

    # Recursive deletion removes the directory and its links, not the link targets.
    nested = f"{root}/nested"
    assert (await call("write", f"{nested}/child.txt", "remove this")).error is None
    assert (await call("execute", f"ln -s {binary_path} {nested}/keep-target")).exit_code == 0
    assert (await call("delete", nested)).error is None
    assert (await call("execute", f"test ! -e {nested}")).exit_code == 0
    assert (await call("download_files", [binary_path]))[0].content == binary
    broken_link = f"{root}/broken link"
    assert (await call("execute", f"ln -s {root}/absent {shlex.quote(broken_link)}")).exit_code == 0
    assert (await call("delete", broken_link)).error is None
    assert (await call("execute", f"test ! -L {shlex.quote(broken_link)}")).exit_code == 0

    # Capture-at-source is disabled by default. Its public methods return the
    # ordinary command result so Deep Agents can use its generic output eviction.
    capture = f"{root}/capture.txt"
    assert backend.enable_capture_offload is False
    offload = await call(
        "execute_with_offload", "printf 'offload fallback'; exit 7", capture, max_inline_bytes=4
    )
    assert offload.offloaded is False
    assert offload.response.output == "offload fallback" and offload.response.exit_code == 7
    assert (await call("execute", f"test ! -e {capture}")).exit_code == 0
    assert (await call("execute", "printf 'still usable'")).output == "still usable"

    operations = {
        "execute",
        "execute_with_offload",
        "write",
        "read",
        "edit",
        "ls",
        "grep",
        "glob",
        "delete",
        "upload_files",
        "download_files",
    }
    expected = {f"a{name}" for name in operations} if interface == "async" else operations
    assert exercised == expected
    public = {
        name
        for name in dir(RenderSandbox)
        if not name.startswith("_") and callable(getattr(RenderSandbox, name))
    }
    assert public == operations | {f"a{name}" for name in operations}
    # File deletion and an ordinary command failure do not terminate a borrowed
    # sandbox. The fixture still owns its lifecycle and verifies final cleanup.
    sdk = RenderAsync()
    async with sdk.client.internal:
        assert (await sdk.experimental.sandboxes.from_id(backend.id)).status == "running"
