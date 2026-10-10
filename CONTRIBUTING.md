# Contributing

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Run these commands from the repository root to test the installed wheel:

```bash
uv sync --frozen --group dev
uv run --no-sync ruff check langchain_render tests examples scripts
uv run --no-sync ruff format --check langchain_render tests examples scripts
uv run --no-sync ty check langchain_render
uv build
uv run --no-sync python scripts/install_wheel.py
uv run --no-sync python -I scripts/check_distribution.py
uv run --no-sync python -I -m pytest --import-mode=importlib tests/unit_tests -q
```

## Live tests

Before handing off a change, run both the runtime tests and the full, unmodified LangChain sandbox suite. Configure `RENDER_API_KEY` and `RENDER_WORKSPACE_ID` in `.env` using the [README setup](README.md#install-the-review-build). No model-provider key is needed. These tests create finite-lived Render sandboxes and consume workspace quota.

```bash
RUN_RENDER_LIVE_TESTS=1 uv run --no-sync --env-file .env python -I -m pytest --import-mode=importlib tests/integration_tests/test_runtime.py tests/integration_tests/test_repository_standard.py -v -s --tb=short --timeout=180 --junitxml=.artifacts/live.xml
```

The upstream suite is pinned to LangChain commit `b060a7466482ed5dac5fa1ef12e3ad21cccce385`. Its [manifest](tests/upstream/manifest.json) records the source and license hashes; the loader verifies them before collecting all 87 cases. Do not modify assertions, skip cases, or mark failures as expected. Confirm every created sandbox is terminated and report any new failures.

The ten runtime tests cover public sync and async backend operations, owned lifecycle, and failure cleanup. Snapshot restoration, SDK-only operations, alternate compute/region settings, and non-default network policies remain outside live coverage.

## Known contract differences

With `deepagents==0.7.23`, the pinned LangChain suite reports **80 passed and 7 failed**. The combined live command therefore exits with status 1. These failures remain visible and are not an approved compatibility exception:

| Test expectation | Inherited `BaseSandbox` behavior | Cases |
| --- | --- | --- |
| Writing an existing file fails | Overwrites the file | 1 |
| Glob results use relative basenames | Returns absolute paths | 5 |
| Glob includes directories | Returns matching files | 1 |

The same failures reproduce without Render using the unchanged base class. See the latest [per-case results](validation/repository-suite-results.json) and [runtime and cleanup evidence](validation/live-results.json). Keep run logs and earlier results in [GitHub Actions](https://github.com/render-oss/langchain-rendersandbox-plugin/actions), pull requests, and Git history.

## CI

[CI](.github/workflows/ci.yml) checks the installed package on Linux, macOS, and Windows. Live checks run from trusted branches using the `sandbox-integration` environment's `RENDER_API_KEY` and `RENDER_WORKSPACE_ID` secrets. Configure required reviewers and restrict that environment to reviewed branches; fork pull requests receive no Render credentials. Missing credentials fail the live job. Package publishing is not enabled.
