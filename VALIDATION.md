# Validation status

Development preview `langchain-render==0.0.1a1`. The source repository is [render-oss/langchain-rendersandbox-plugin](https://github.com/render-oss/langchain-rendersandbox-plugin); the Python package is not released on PyPI. The integration is not fully compliant with the pinned LangChain standard suite.

Local validation on October 9, 2026 (270.88 seconds for the combined 97-case live run) passed **61 unit tests and all ten real-Render runtime checks**. The full unmodified LangChain suite returned **80 passed and 7 failed**, with no errors or skips. The seven failures are the documented upstream write/glob contract differences below. Test results are not suppressed. All eleven test sandboxes were independently confirmed terminated. Exact evidence is in [live-results.json](validation/live-results.json).

The complete repair example also passed with Claude Sonnet 4.6: the application restored trusted tests, verified all three cases, downloaded a report totaling 42, and confirmed sandbox cleanup. See [real-model evidence](validation/real-model-repair.json). This was a separate earlier model run, not part of each validation rerun.

[Live run evidence](validation/live-results.json) and [upstream suite evidence](validation/repository-suite-results.json) retain the latest source hashes, per-case results, failures, and verified cleanup. Local filesystem paths and workspace details are redacted. [Historical evidence](https://github.com/render-oss/langchain-rendersandbox-plugin/tree/c8de5eeb0282b07125cbec6b6ebbf6db174e6e90/validation) remains available in Git history, including earlier stream connection failures whose cause was not established by later successful runs.

The [integration guide](docs/guide.mdx) targets LangChain documentation. Its MDX, Mermaid syntax, and links were checked locally. Native LangChain site preview and documentation submission remain pending.

## Operation coverage

Every public backend method now has a real-Render check through both the synchronous and asynchronous interface. The new [operation checks](tests/integration_tests/test_runtime.py) exercise all **22 callable methods** and the `id` property, and fail if a new public method is added without corresponding coverage. This establishes operation coverage for the supported default configuration, not exhaustive coverage of every input, option combination or failure mode. The repository's [validation requirement](AGENTS.md) now includes these checks after every change.

| Operation | Live evidence |
| --- | --- |
| Create, wait for readiness, configure, attach, terminate | Owned-session tests verify environment variables, finite lifetime, deny-all policy metadata, status lookup and cleanup; the operation checks attach a separate backend to the existing sandbox ID. |
| `execute` / `aexecute` | Output and exit codes, recoverable nonzero exits, continued usability, large output, timeout and cancellation. |
| `write` / `awrite`, `read` / `aread`, `edit` / `aedit` | Create a file, replace content, read back the changed content, and compare downloaded bytes. |
| `ls` / `als`, `grep` / `agrep`, `glob` / `aglob` | Verify returned paths, matching line numbers and current absolute file-only glob semantics. Seven upstream write/glob assertions still disagree with the current base-class contract. |
| `upload_files` / `aupload_files`, `download_files` / `adownload_files` | Verify binary and text byte round-trips and response ordering; upstream cases cover large files and missing/invalid/permission errors. |
| `delete` / `adelete` | Delete a file with spaces and quotes in its name, verify absence, report missing paths, delete a directory recursively without following its symlinks, remove a broken symlink, and confirm the sandbox remains running. |
| `execute_with_offload` / `aexecute_with_offload` | Verify the supported default fallback preserves command output and exit code when capture-at-source is disabled. The upstream agent test separately verifies generic large-output offload. |
| Failure cleanup | Live timeout, cancellation and application-error checks confirm owned sandbox termination. Unit tests also inject readiness, authentication, rate-limit and cleanup failures. |

**Still outside live coverage:** filesystem/runtime snapshot creation and restoration (including session startup by snapshot ID/name), direct SDK directory transfers and raw streaming as user-facing features, alternate compute/region options, and non-default network policies. Unit tests verify forwarding of snapshot ID, plan and region options, which does not prove successful provisioning or restoration. Capture-at-source opt-in is not enabled or qualified by these checks. Hosted cross-platform CI and exhaustive configuration/failure combinations also remain unverified. Do not describe this as every Render SDK feature being tested.

## Current LangChain repository suite

Fetched and ran the unmodified [sandbox suite at LangChain commit b060a746](https://github.com/langchain-ai/langchain/blob/b060a7466482ed5dac5fa1ef12e3ad21cccce385/libs/standard-tests/langchain_tests/integration_tests/sandboxes.py) on October 9, 2026. The repository still declares package version 1.1.9, but its source contains `test_execute_capture_at_source_offload`, which is absent from the published wheel. An AST comparison confirmed the other 85 test methods are identical. Including the inherited no-overrides check, the repository suite collects 87 cases.

**Latest result: 80 passed, 7 failed, no errors or skips**, after the repository cleanup (270.88 seconds for the combined runtime and upstream run). All ten runtime checks also passed. Exact source hashes are recorded with the current results; earlier runs remain in Git history.

The seven failures are the write/glob expectations listed below. Each also fails against the unchanged `BaseSandbox` with a trusted local command/file transport and no Render dependency. `RenderSandbox.write` and `RenderSandbox.glob` are the inherited methods by identity. This isolates the disagreement to the upstream test/current base-class contract, rather than this provider's transport. Full suite provenance, source hashes, individual outcomes and reproduction results are in [repository-suite-results.json](validation/repository-suite-results.json).

## Standard-suite contract differences

Pinned versions: `deepagents==0.7.23`, `render==1.2.0`, and `langchain-tests==1.1.9`.

| Existing test expectation | Current inherited behavior | Affected cases |
| --- | --- | --- |
| Writing an existing file fails and preserves its original bytes | `BaseSandbox.write` and `awrite` overwrite it | `test_write_existing_file_fails` |
| Glob results contain relative basenames | `BaseSandbox.glob` returns absolute paths; its permission checks depend on those paths | `test_glob`, `test_glob_basic_pattern`, `test_glob_hidden_files_explicitly`, `test_glob_with_character_class`, `test_glob_with_question_mark` |
| `glob("*")` includes directories | The current remote helper enumerates matching files | `test_glob_with_directories` |

The overwrite change is intentional, confirmed by merged [Deep Agents PR #4109](https://github.com/langchain-ai/deepagents/pull/4109), merge commit `77a463131c8708ee90aaad676f1e4b2da252d1ed`. The [standard suite at LangChain commit b060a746](https://github.com/langchain-ai/langchain/blob/b060a7466482ed5dac5fa1ef12e3ad21cccce385/libs/standard-tests/langchain_tests/integration_tests/sandboxes.py#L697) retains the old write assertion. In the installed Deep Agents source, `BaseSandbox.glob`, `_absolutize_glob_path` and `_GLOB_COMMAND_TEMPLATE` establish the current glob behavior. The live current-contract test verifies these semantics without overriding them.

No patch is applied to the integration's installed dependencies or the standard suite. Updating upstream tests is a separate review task, not a runtime dependency for this package. Do not claim full compliance with this version of `langchain-tests`.

## Implementation fixes and limits

- **Rate limiting:** Only explicit `RateLimitError` HTTP rejections are retried, up to six times with exponential backoff and jitter inside the existing deadline. The public SDK raises this before the exec stream starts. Any received event prevents replay. Network loss, stream errors and HTTP 500 remain fatal because execution may be uncertain. File transfers use the same bounded rejection handling.
- **File errors:** Render 1.2.0 drops some structured 400/403 codes. A version-scoped parser recognizes only the exact SDK envelope and known file error codes. Authentication, malformed responses and ambiguous errors remain fatal. Tests use the actual SDK error helper; runtime code does not import that private helper.
- **Async helper timeouts:** The adapter checks failed state after inherited grep/glob helpers so they cannot hide cancellation as a recoverable tool result.
- **Local cleanup:** Adapter-owned disk workers finish before their temporary directory is removed. A precreated regular-file destination prevents the SDK from extracting an unexpected directory archive. Actual SDK transport regressions verify both normal files and unexpected tar responses.
- **Deadline scope:** Local disk operations can outlast cooperative timeouts. The SDK exposes no hard filesystem cancellation/join guarantee. A disconnected exec stream does not stop a remote process. The application owns sandbox termination, with a finite lifetime as a fallback.

## Reproduce the internal review build

Clone [the repository](https://github.com/render-oss/langchain-rendersandbox-plugin) and open its root directory, or extract a source archive. The archive includes the lockfile, complete upstream suite, examples, workflow, license and validation record.

Run these commands in that directory with Python 3.11+ and [uv 0.12.1](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv sync --frozen --group dev
uv build
uv run --no-sync python scripts/install_wheel.py
uv run --no-sync python -I scripts/check_distribution.py
uv run --no-sync python -I -m pytest --import-mode=importlib tests/unit_tests -q
uv run --no-sync python -I -m pytest --import-mode=importlib tests/integration_tests/test_repository_standard.py --collect-only -q
```

The distribution check prints the installed package location and SHA-256 checksums, verifies that the wheel matches the source, and checks that the source archive contains the complete test harness and license files. `-I` and `--import-mode=importlib` keep the tests on the installed wheel. Collection should report 87 cases without Render credentials.

Configure `.env` using the [README setup](README.md#install-the-review-build), then run the complete live review against that same installed wheel:

```bash
RUN_RENDER_LIVE_TESTS=1 uv run --no-sync --env-file .env python -I -m pytest --import-mode=importlib tests/integration_tests/test_runtime.py tests/integration_tests/test_repository_standard.py -v -s --tb=short --timeout=180 --junitxml=.artifacts/live.xml
```

This includes invalid authentication, two concurrent isolated sessions, cleanup after an application error, timeout/cancellation, agent dispatch and the repair workflow. It creates finite-lived sandboxes and checks their termination. The ten runtime cases should pass. The upstream suite currently reports 80 passes and seven documented assertion failures, so the combined command exits with status 1. Keep that failure visible; it is not an approved compatibility exception. Test results are saved in `.artifacts/live.xml`. No external model key is needed for this review run.

On PowerShell, set `$env:RUN_RENDER_LIVE_TESTS="1"` before running the `uv` command without its shell prefix.

The default command uses all 87 cases from the unmodified LangChain repository suite at commit `b060a7466482ed5dac5fa1ef12e3ad21cccce385`. The source, MIT license and checksums are included under [tests/upstream](tests/upstream/manifest.json); the loader verifies both digests before collection. No upstream download or private harness is needed. `langchain-tests==1.1.9` remains the development dependency, and `tests/integration_tests/test_standard.py` separately runs its released 86-case suite. The repository version includes a large-output offload case absent from that release. See the [recorded contract differences](#standard-suite-contract-differences) and [per-case evidence](validation/repository-suite-results.json).

## CI and release ownership

The first hosted run passed the package checks on Linux (Python 3.11 through 3.14) and macOS. Windows passed all 61 unit tests but failed upstream-suite collection because Git converted the pinned source and license to CRLF. The Windows-built archive confirmed that converting those bytes back to LF reproduced both recorded upstream hashes. `.gitattributes` now preserves all `tests/upstream/*` bytes during checkout; neither upstream source nor expected hashes were changed. See [GitHub Actions](https://github.com/render-oss/langchain-rendersandbox-plugin/actions) for subsequent hosted results.

The [workflow](.github/workflows/ci.yml) checks lint, types, archives and installed-wheel unit tests on Linux with Python 3.11 through 3.14, plus macOS and Windows on Python 3.12. Actions are pinned to commit SHAs. Pull-request checks receive no Render credentials. Main-branch pushes and manual runs also run the live suite, serialized per repository, with no failure suppression.

Before enabling credentialed live checks, create a `sandbox-integration` environment with `RENDER_API_KEY` and `RENDER_WORKSPACE_ID` secrets for a dedicated Sandboxes-enabled test workspace. Restrict that environment to reviewed branches and configure required reviewers. For a fork contribution, a maintainer must review the code before starting a live run from a trusted branch. The job fails when credentials are missing; local unit results do not stand in for it. Require the package checks and a reviewed live result before merging.

The workflow is included in the repository. Hosted execution is separate from the local evidence above. The live job requires the Render test secrets and fails if they are missing. No package publishing job is enabled. The initial maintainer and release owner is [Ojus (@ojusave)](https://github.com/ojusave), recorded in [CODEOWNERS](.github/CODEOWNERS). Require his review for releases and changes to the workflow or package. The source checkout and archives contain the complete review harness.

## Configuration and existing sandboxes

`sandbox_session(...)` accepts `timeout_seconds`, `network_policy`, `env`, `plan`, `region`, `snapshot_id`, and `snapshot_name`, forwarding creation options to Render's SDK. Available values depend on Render. Both the helper and `RenderSandbox` accept explicit `token`, `owner_id`, and `base_url`; otherwise they read `RENDER_API_KEY` and `RENDER_WORKSPACE_ID` from the application environment.

The examples require Bash, `python3`, and `python` in the sandbox. Run `uv run --env-file .env python examples/lifecycle.py` to check access and image tools without making a model call. It prints `hello from Render` and confirms cleanup. If needed, set the example's `RENDER_SNAPSHOT_ID` to an available, unexpired filesystem snapshot containing those tools. See [Render snapshots](https://render.com/docs/sandboxes#snapshots).

To attach a sandbox your application already manages, wait for it to be `running`, then use its ID:

```python
from langchain_render import RenderSandbox

backend = RenderSandbox(sandbox_id="YOUR_RUNNING_SANDBOX_ID")
result = backend.execute("printf 'hello from Render\\n'", timeout=30)
print(result.output)
```

Pass this backend to `create_deep_agent(backend=backend, model=...)`. Your application owns termination when attaching an existing ID. Use the [Render Python SDK](https://render.com/docs/sandboxes-sdk-python) with `backend.id` for snapshots, streaming output, directory transfers, and other platform operations. Snapshot restoration does not restore LangGraph conversation state automatically and remains outside the current live tests.

## Execution and file behavior

Ordinary nonzero command exits are returned in `ExecuteResponse.exit_code`. Output combines stdout and stderr in stream arrival order. At most 2,097,152 characters are retained by default; excess output is drained to preserve the exit event, and `truncated=True` reports the loss. Change `max_output_chars` if your application needs another limit.

Explicit HTTP 429 rejections are retried with exponential backoff and jitter, up to six retries within the operation deadline. A command is never retried after an execution event, and ambiguous transport/server failures stop the run.

Commands default to a 300-second client deadline. `timeout=0` disables that deadline. A timeout, cancellation, missing exit event, or execution transport error makes this backend unusable. **Closing a Render stream does not cancel the remote command.** Stop the agent and leave the owned `sandbox_session` block so cleanup runs. For an application-managed sandbox, terminate it with the Render SDK. Start the next run with a new backend. The adapter does not automatically destroy a sandbox it was given.

The [repair example](examples/repair_project.py) downloads wanted files before the [session helper](langchain_render/session.py) requests termination and verifies that the sandbox reports `terminated` or is no longer retrievable. This is an observable API state, not a guarantee of instantaneous process death. If cleanup fails, the helper raises an error containing the sandbox ID needed to retry. A create request that times out before returning an ID can still have created a sandbox; inspect the workspace before retrying. The finite lifetime limits that resource's duration.

File transfers require absolute paths and return results in input order. Missing files and recognized file permission errors are reported per file. Authentication, connectivity, or ambiguous SDK failures raise `RenderSandboxError` instead of being presented as missing files. Downloads return file bytes, not directory archives. A precreated local file prevents the SDK from extracting an unexpected archive if the remote path changes after the directory check. Such an unexpected response aborts the run.

`file_timeout` defaults to 300 seconds per transfer, after the directory probe, which uses the command timeout. This is a cooperative timeout, not a hard deadline for local disk I/O. Cancellation waits for adapter-owned local file workers before removing their temporary directory; a stalled filesystem can extend that wait. The SDK also uses blocking file operations without a public join handle. Hard end-to-end cancellation requires stronger isolation or SDK support.

This integration does not implement Workflows, persistent Python interpreter state, automatic pause/resume, or checkpoint-based sandbox restoration.

Async callers use `await backend.aexecute(...)`, `aupload_files`, and `adownload_files`. Inherited `read`, `write`, `edit`, `ls`, `grep`, and `glob` methods have asynchronous equivalents. `delete(path)` and `adelete(path)` delete a remote file or directory; they do not terminate the sandbox.
