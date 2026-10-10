"""Pinned, unmodified LangChain repository tests with the Render fixture.

The source and MIT license are distributed under tests/upstream. This module
checks their digests before loading any upstream code; no download is needed.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "tests" / "upstream"
manifest = json.loads((UPSTREAM / "manifest.json").read_text())
for name, key in (("sandboxes.py", "sha256"), ("LICENSE", "license_sha256")):
    if hashlib.sha256((UPSTREAM / name).read_bytes()).hexdigest() != manifest[key]:
        raise RuntimeError(f"Pinned upstream {name} digest mismatch; restore the unmodified source")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


upstream = load_module("langchain_tests.pinned_sandboxes", UPSTREAM / "sandboxes.py")
fixture_module = load_module(
    "render_standard_fixture", Path(__file__).with_name("test_standard.py")
)


@pytest.mark.live
class TestRenderSandboxRepository(upstream.SandboxIntegrationTests):
    # The only provider-specific input. All test methods remain inherited.
    sandbox = fixture_module.TestRenderSandboxStandard.__dict__["sandbox"]
