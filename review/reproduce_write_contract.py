"""Reproduce the upstream write mismatch without Render credentials.

This executes only trusted BaseSandbox preflight and standard-test commands in
an isolated local fixture. No model is used and no generated code is executed.
"""

import asyncio
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from langchain_tests.integration_tests import SandboxIntegrationTests


class TrustedLocalFixture(BaseSandbox):
    """Local transport only for the fixed upstream reproduction below."""

    def __init__(self, root: Path):
        self.root = root

    @property
    def id(self) -> str:
        return "upstream-write-reproduction"

    def checked_path(self, path: str) -> Path:
        local = Path(path).resolve()
        if not local.is_relative_to(self.root):
            raise ValueError("Reproduction path escaped its fixture")
        return local

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout or 10,
            check=False,
        )
        return ExecuteResponse(result.stdout, result.returncode)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        for path, content in files:
            self.checked_path(path).write_bytes(content)
        return [FileUploadResponse(path) for path, _content in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path, self.checked_path(path).read_bytes()) for path in paths]


class StandardTestReproduction(SandboxIntegrationTests):
    def sandbox(self):
        raise NotImplementedError("The reproduction passes its fixture directly")


def main() -> None:
    scratch = Path(__file__).resolve().parents[1] / ".loop" / "reproductions"
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=scratch, prefix="write-contract-") as directory:
        root = Path(directory).resolve()
        backend = TrustedLocalFixture(root)
        case = StandardTestReproduction()
        try:
            case.test_write_existing_file_fails(backend, str(root))
        except AssertionError:
            assert (root / "existing.txt").read_text() == "Second content"
            print(
                "CONFIRMED: the unmodified standard test fails after BaseSandbox overwrites the file"
            )
        else:
            raise AssertionError("Mismatch no longer reproduces; recheck the upstream versions")

        sync_path = str(root / "sync.txt")
        assert backend.write(sync_path, "first").error is None
        sync_result = backend.write(sync_path, "second")
        assert sync_result.error is None and sync_result.path == sync_path
        assert Path(sync_path).read_text() == "second"
        print("CONFIRMED: synchronous write matches the intentional overwrite contract")

        async def verify_async() -> None:
            async_path = str(root / "async.txt")
            assert (await backend.awrite(async_path, "first")).error is None
            async_result = await backend.awrite(async_path, "second")
            assert async_result.error is None and async_result.path == async_path
            assert await asyncio.to_thread(Path(async_path).read_text) == "second"

        asyncio.run(verify_async())
        print("CONFIRMED: asynchronous write matches the intentional overwrite contract")


if __name__ == "__main__":
    main()
