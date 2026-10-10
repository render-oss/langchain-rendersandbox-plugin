import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from render.client.errors import ClientError, RateLimitError
from render.experimental.sandbox.errors import SandboxFileNotFoundError, SandboxNotFoundError
from render.experimental.sandbox.types import SandboxExecExit, SandboxExecOutput

import langchain_render.sandbox as module
from langchain_render import RenderSandbox, RenderSandboxError


class Connection:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Transport:
    def __init__(self):
        self.events = [SandboxExecExit(0)]
        self.calls = []
        self.closed_streams = 0
        self.files = {}
        self.errors = {}
        self.wait = None
        self.started = None
        self.local_paths = []
        self.connections = []

    def sdk(self, **kwargs):
        connection = Connection()
        self.connections.append(connection)
        return SimpleNamespace(
            client=SimpleNamespace(internal=connection),
            experimental=SimpleNamespace(sandboxes=self),
        )

    async def exec(self, sandbox_id, command):
        self.calls.append((sandbox_id, command))
        try:
            if self.started is not None:
                self.started.set()
            if self.wait is not None:
                await self.wait.wait()
            events = [SandboxExecExit(1)] if command.startswith("test -d ") else self.events
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            self.closed_streams += 1

    async def copy_to(self, sandbox_id, local, remote):
        self.local_paths.append(Path(local))
        if remote in self.errors:
            raise self.errors[remote]
        self.files[remote] = await asyncio.to_thread(Path(local).read_bytes)

    async def copy_from(self, sandbox_id, remote, local):
        self.local_paths.append(Path(local))
        if remote in self.errors:
            raise self.errors[remote]
        if remote not in self.files:
            raise SandboxFileNotFoundError("file missing")
        await asyncio.to_thread(Path(local).write_bytes, self.files[remote])
        return str(local)


@pytest.fixture
def transport(monkeypatch):
    transport = Transport()
    monkeypatch.setattr(module, "RenderAsync", transport.sdk)
    return transport


@pytest.fixture
def backend(transport):
    return RenderSandbox(
        sandbox_id="sandbox-test", token="test-only-token", owner_id="workspace-test"
    )


def test_nonzero_exit_is_recoverable_and_stdout_stderr_keep_order(backend, transport):
    transport.events = [
        SandboxExecOutput("stdout", "out"),
        SandboxExecOutput("stderr", "err"),
        SandboxExecExit(7),
    ]
    assert backend.execute("a command") == ExecuteResponse("outerr", 7, False)
    transport.events = [SandboxExecExit(0)]
    assert backend.execute("retry").exit_code == 0
    assert all(c.closed for c in transport.connections)
    assert transport.closed_streams == 2


def test_output_cap_drains_stream_and_keeps_real_exit_code(transport):
    backend = RenderSandbox(sandbox_id="sandbox-test", max_output_chars=4)
    transport.events = [
        SandboxExecOutput("stdout", "abc"),
        SandboxExecOutput("stderr", "def"),
        SandboxExecOutput("stdout", "later"),
        SandboxExecExit(13),
    ]
    assert backend.execute("large") == ExecuteResponse("abcd", 13, True)
    assert transport.closed_streams == 1


@pytest.mark.parametrize(
    "events",
    [[], [object()], [SandboxNotFoundError("not found")], [ConnectionError("connection lost")]],
)
def test_incomplete_execution_aborts_and_prevents_reuse(backend, transport, events):
    transport.events = events
    with pytest.raises(RenderSandboxError, match="may still be running"):
        backend.execute("possibly started")
    calls = len(transport.calls)
    with pytest.raises(RenderSandboxError, match="unusable"):
        backend.execute("must not start")
    with pytest.raises(RenderSandboxError):
        backend.upload_files([("/tmp/x", b"x")])
    assert len(transport.calls) == calls
    assert all(c.closed for c in transport.connections)


async def test_deadline_closes_stream_and_preserves_timeout_as_cause(backend, transport):
    transport.wait = asyncio.Event()
    with pytest.raises(RenderSandboxError) as error:
        await backend.aexecute("sleep forever", timeout=1)
    assert isinstance(error.value.__cause__, TimeoutError)
    assert transport.closed_streams == 1
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("next")


async def test_cancellation_propagates_closes_connections_and_prevents_reuse(backend, transport):
    transport.wait = asyncio.Event()
    transport.started = asyncio.Event()
    task = asyncio.create_task(backend.aexecute("sleep forever"))
    await transport.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed_streams == 1
    assert all(c.closed for c in transport.connections)
    with pytest.raises(RenderSandboxError):
        await backend.adownload_files(["/tmp/x"])


async def test_native_async_does_not_use_sync_method(backend, monkeypatch):
    monkeypatch.setattr(backend, "execute", lambda *_a, **_k: pytest.fail("sync execution used"))
    assert (await backend.aexecute("ok")).exit_code == 0


async def test_sync_interface_works_inside_notebook_event_loop(backend):
    assert backend.execute("sync in notebook").exit_code == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_invalid_timeout_does_not_start_remote_work(backend, transport, value):
    with pytest.raises(ValueError):
        backend.execute("not run", timeout=value)
    assert transport.calls == []


def test_zero_timeout_is_supported(backend):
    assert backend.execute("ok", timeout=0).exit_code == 0


@pytest.mark.parametrize(
    "options",
    [
        {"sandbox_id": ""},
        {"sandbox_id": "x", "file_timeout": 0},
        {"sandbox_id": "x", "max_output_chars": 0},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises(ValueError):
        RenderSandbox(**options)


def test_binary_files_order_normalization_and_temporary_cleanup(backend, transport):
    data = bytes(range(256)) * 5000
    files = [("/tmp/a", b""), ("relative", b"bad"), ("/tmp//b", data)]
    assert backend.upload_files(files) == [
        FileUploadResponse("/tmp/a"),
        FileUploadResponse("relative", "invalid_path"),
        FileUploadResponse("/tmp//b"),
    ]
    assert backend.download_files(["/tmp//b", "relative", "/tmp/a", "/missing"]) == [
        FileDownloadResponse("/tmp//b", data),
        FileDownloadResponse("relative", None, "invalid_path"),
        FileDownloadResponse("/tmp/a", b""),
        FileDownloadResponse("/missing", None, "file_not_found"),
    ]
    assert transport.local_paths
    assert all(not p.exists() for p in transport.local_paths)


def test_file_permission_error_is_per_file_and_does_not_poison_backend(backend, transport):
    transport.errors["/denied"] = ClientError("denied", code="permission_denied")
    assert backend.upload_files([("/denied", b"a"), ("/ok", b"b")]) == [
        FileUploadResponse("/denied", "permission_denied"),
        FileUploadResponse("/ok"),
    ]
    assert backend.download_files(["/denied", "/ok"]) == [
        FileDownloadResponse("/denied", None, "permission_denied"),
        FileDownloadResponse("/ok", b"b"),
    ]
    assert backend.execute("still usable").exit_code == 0


@pytest.mark.parametrize("operation", ["upload", "download"])
def test_auth_failure_is_not_mislabeled_as_file_not_found(backend, transport, operation):
    transport.errors["/x"] = ClientError("unauthorized", code="unauthorized")
    with pytest.raises(RenderSandboxError):
        if operation == "upload":
            backend.upload_files([("/x", b"x")])
        else:
            backend.download_files(["/x"])
    with pytest.raises(RenderSandboxError):
        backend.execute("next")


def test_download_directory_does_not_transfer_archive(backend, transport, monkeypatch):
    async def probe(*args, **kwargs):
        return ExecuteResponse("", 0)

    monkeypatch.setattr(backend, "aexecute", probe)
    assert backend.download_files(["/directory"]) == [
        FileDownloadResponse("/directory", None, "is_directory")
    ]
    assert transport.local_paths == []


async def test_async_delete_uses_native_execution(backend, monkeypatch):
    commands = []

    async def execute(command, **kwargs):
        commands.append(command)
        return ExecuteResponse("", 0)

    monkeypatch.setattr(backend, "aexecute", execute)
    monkeypatch.setattr(backend, "execute", lambda *_a, **_k: pytest.fail("sync execution used"))
    assert (await backend.adelete("/tmp/a b")).path == "/tmp/a b"
    assert commands == ["test -e '/tmp/a b' || test -L '/tmp/a b'", "rm -rf '/tmp/a b'"]


@pytest.mark.parametrize(
    "helper,constant", [("agrep", "ASYNC_GREP_TIMEOUT"), ("aglob", "ASYNC_GLOB_TIMEOUT")]
)
async def test_inherited_helper_deadline_cannot_hide_canceled_remote_work(
    backend, transport, monkeypatch, helper, constant
):
    import deepagents.backends.sandbox as upstream

    monkeypatch.setattr(upstream, constant, 0.01)
    transport.wait = asyncio.Event()
    with pytest.raises(RenderSandboxError, match="unusable"):
        await getattr(backend, helper)("something", path="/tmp")
    assert transport.closed_streams == 1


@pytest.mark.parametrize(
    "action,code,expected",
    [
        ("upload", "path_not_writable", "permission_denied"),
        ("download", "path_not_readable", "permission_denied"),
        ("upload", "is_a_directory", "is_directory"),
        ("upload", "not_a_directory", "invalid_path"),
    ],
)
def test_actual_render_120_error_envelope_maps_only_known_file_codes(action, code, expected):
    import json

    from render.experimental.sandbox.api import _raise_sandbox_http_error

    with pytest.raises(ClientError) as caught:
        _raise_sandbox_http_error("sandbox-test", 403, json.dumps({"code": code}), action)
    assert caught.value.code is None  # the released SDK loses the structured code
    assert module._file_error(caught.value) == expected


@pytest.mark.parametrize(
    "message",
    [
        'download failed with status 401: {"code":"permission_denied"}',
        'download failed with status 403: {"code":"unauthorized"}',
        "download failed with status 403: permission_denied",
        'download failed with status 403: ["permission_denied"]',
        'download failed with status 403: {"code":[]}',
        'some text: download failed with status 403: {"code":"permission_denied"}',
    ],
)
def test_error_envelope_does_not_guess_auth_or_malformed_responses(message):
    assert module._file_error(ClientError(message)) is None


def test_error_envelope_shim_is_limited_to_render_120(monkeypatch):
    monkeypatch.setattr(module, "_RENDER_120", False)
    assert (
        module._file_error(
            ClientError('download failed with status 403: {"code":"path_not_readable"}')
        )
        is None
    )


async def test_canceled_upload_joins_disk_writer_before_temporary_cleanup(
    backend, transport, monkeypatch
):
    import threading

    started = threading.Event()
    release = threading.Event()
    seen = []
    original_write = Path.write_bytes

    def slow_write(path, data):
        seen.append(path)
        started.set()
        assert release.wait(3)
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", slow_write)
    task = asyncio.create_task(backend.aupload_files([("/tmp/a", b"bytes")]))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert seen[0].parent.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not seen[0].parent.exists()
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("next")


@pytest.mark.parametrize("content_type", ["application/octet-stream", "application/x-tar"])
async def test_real_sdk_download_replaces_file_but_never_starts_archive_extraction(
    monkeypatch, content_type
):
    """Exercise the released SDK transport, not a fabricated copy_from method."""
    import httpx
    from render import RenderAsync
    from render.experimental.sandbox import api as sdk_api
    from render.experimental.sandbox.errors import SandboxDownloadError

    original_client = httpx.AsyncClient
    local_workers = []
    original_worker = sdk_api.arun_blocking
    temporary_paths = []
    original_touch = Path.touch

    def capture_touch(path):
        temporary_paths.append(path)
        original_touch(path)

    async def capture_worker(function, *args):
        local_workers.append(function.__name__)
        return await original_worker(function, *args)

    def response(request):
        if request.url.host == "api.render.test":
            assert request.url.path.endswith("/files/download/token")
            return httpx.Response(
                200,
                json={
                    "token": "mock-file-token",
                    "uri": "https://files.render.test/download",
                    "method": "GET",
                },
            )
        assert request.url.host == "files.render.test"
        return httpx.Response(200, headers={"Content-Type": content_type}, content=b"file bytes")

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(sdk_api, "arun_blocking", capture_worker)
    monkeypatch.setattr(Path, "touch", capture_touch)
    sdk = RenderAsync(
        token="mock-token", owner_id="workspace-test", base_url="https://api.render.test"
    )
    backend = RenderSandbox(sandbox_id="sandbox-test")
    monkeypatch.setattr(backend, "_client", lambda: sdk)

    async def probe(*args, **kwargs):
        return ExecuteResponse("", 1)

    monkeypatch.setattr(backend, "aexecute", probe)
    if content_type == "application/octet-stream":
        assert await backend.adownload_files(["/tmp/file"]) == [
            FileDownloadResponse("/tmp/file", b"file bytes")
        ]
        assert local_workers == ["commit"]
    else:
        with pytest.raises(RenderSandboxError) as caught:
            await backend.adownload_files(["/tmp/path-became-a-directory"])
        assert isinstance(caught.value.__cause__, SandboxDownloadError)
        assert isinstance(caught.value.__cause__.__cause__, FileExistsError)
        assert local_workers == []
        with pytest.raises(RenderSandboxError, match="unusable"):
            backend._check_usable()
    assert len(temporary_paths) == 1
    assert not temporary_paths[0].parent.exists()


async def test_rate_limit_rejection_retries_without_replaying_received_output(
    backend, transport, monkeypatch
):
    transport.events = [RateLimitError("HTTP 429")]
    pauses = []

    async def pause(attempt):
        pauses.append(attempt)
        transport.events = [SandboxExecOutput("stdout", "once"), SandboxExecExit(0)]

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    assert (await backend.aexecute("command")).output == "once"
    assert pauses == [0]
    assert len(transport.calls) == 2
    assert transport.closed_streams == 2
    assert all(c.closed for c in transport.connections)


async def test_rate_limit_after_any_event_must_not_replay_command(backend, transport, monkeypatch):
    transport.events = [SandboxExecOutput("stdout", "started"), RateLimitError("late 429")]

    async def pause(attempt):
        pytest.fail("must not retry a command that already emitted an event")

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    with pytest.raises(RenderSandboxError) as caught:
        await backend.aexecute("possibly changed state")
    assert isinstance(caught.value.__cause__, RateLimitError)
    assert len(transport.calls) == 1


async def test_rate_limit_retries_are_bounded(backend, transport, monkeypatch):
    transport.events = [RateLimitError("HTTP 429")]
    pauses = []

    async def pause(attempt):
        pauses.append(attempt)

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    with pytest.raises(RenderSandboxError) as caught:
        await backend.aexecute("rejected")
    assert isinstance(caught.value.__cause__, RateLimitError)
    assert pauses == list(range(6))
    assert transport.closed_streams == 7
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("must not dispatch")


async def test_cancel_during_rate_limit_backoff_stops_retries(backend, transport, monkeypatch):
    transport.events = [RateLimitError("HTTP 429")]
    waiting = asyncio.Event()

    async def pause(attempt):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    task = asyncio.create_task(backend.aexecute("rejected"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(transport.calls) == 1
    with pytest.raises(RenderSandboxError, match="unusable"):
        await backend.aexecute("must not dispatch")


async def test_command_deadline_includes_rate_limit_backoff(backend, transport, monkeypatch):
    transport.events = [RateLimitError("HTTP 429")]

    async def pause(attempt):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    with pytest.raises(RenderSandboxError) as caught:
        await backend.aexecute("rejected", timeout=1)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("operation", ["upload", "download"])
async def test_file_rate_limit_retries_preserve_bytes_and_cleanup(
    backend, transport, monkeypatch, operation
):
    transport.errors["/tmp/file"] = RateLimitError("HTTP 429")
    transport.files["/tmp/file"] = b"payload"

    async def pause(attempt):
        assert attempt == 0
        transport.errors.clear()

    monkeypatch.setattr(module, "_rate_limit_pause", pause)
    if operation == "upload":
        assert await backend.aupload_files([("/tmp/file", b"payload")]) == [
            FileUploadResponse("/tmp/file")
        ]
    else:
        assert await backend.adownload_files(["/tmp/file"]) == [
            FileDownloadResponse("/tmp/file", b"payload")
        ]
    assert transport.files["/tmp/file"] == b"payload"
    assert len(transport.local_paths) == 2
    assert all(not p.exists() for p in transport.local_paths)
