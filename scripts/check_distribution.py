"""Check the installed wheel and the complete internal-review source archive.

Run with python -I after installing dist/*.whl to exclude checkout imports.
"""

import hashlib
import json
import sysconfig
import tarfile
import zipfile
from importlib.metadata import distribution
from pathlib import Path

from deepagents.backends.sandbox import BaseSandbox

import langchain_render

ROOT = Path(__file__).resolve().parents[1]
package = Path(langchain_render.__file__).resolve()
assert package.is_relative_to(Path(sysconfig.get_path("purelib")).resolve()), package
assert issubclass(langchain_render.RenderSandbox, BaseSandbox)
assert callable(langchain_render.sandbox_session)
wheel = next((ROOT / "dist").glob("*.whl"))
sdist = next((ROOT / "dist").glob("*.tar.gz"))
with zipfile.ZipFile(wheel) as archive:
    for path in (ROOT / "langchain_render").glob("*.py"):
        assert archive.read(f"langchain_render/{path.name}") == path.read_bytes()
        assert (package.parent / path.name).read_bytes() == path.read_bytes()
    assert any(name.endswith("/licenses/LICENSE") for name in archive.namelist())
with tarfile.open(sdist) as archive:
    members = {name.split("/", 1)[1]: name for name in archive.getnames() if "/" in name}
    for relative in (
        "tests/upstream/sandboxes.py",
        "tests/upstream/LICENSE",
        "tests/upstream/manifest.json",
        "tests/integration_tests/test_repository_standard.py",
        "tests/integration_tests/test_standard.py",
        ".github/workflows/ci.yml",
        "uv.lock",
        "README.md",
        "LICENSE",
    ):
        assert relative in members, f"Missing from source distribution: {relative}"
        assert archive.extractfile(members[relative]).read() == (ROOT / relative).read_bytes()
    assert not any(
        name.startswith((".loop/", ".venv/", ".artifacts/")) or name == ".env" for name in members
    )
metadata = distribution("langchain-render").metadata
assert metadata["License-Expression"] == "Apache-2.0"
print(
    json.dumps(
        {
            "version": metadata["Version"],
            "imported_from": str(package),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
            "source_and_installed_code_match": True,
            "review_harness_included": True,
        },
        indent=2,
    )
)
