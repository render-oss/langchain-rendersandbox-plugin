# Required validation

Before handing off any change to this integration, run LangChain's full, unmodified sandbox standard tests against a real Render sandbox, in addition to the relevant local tests. Unit tests and scripted runtime checks alone are not sufficient. This is Ojus's standing requirement.

- Use the command in [CONTRIBUTING.md](CONTRIBUTING.md#live-tests). Run `tests/integration_tests/test_repository_standard.py`, which includes the pinned repository suite from `tests/upstream/`. Record its upstream revision and source hash. Never edit or format the vendored source.
- Do not skip, override, patch, or mark standard tests as expected failures to produce a passing result. Report all failures and distinguish known upstream contract disagreements from new regressions.
- Run the complete `tests/integration_tests/test_runtime.py` suite as well. It must exercise every public backend operation through both sync and async interfaces, plus owned lifecycle and failure cleanup. Keep SDK-only capabilities and untested configuration combinations explicitly separate from verified plugin coverage.
- Use finite-lived test sandboxes and verify termination. If a live run is blocked, state the exact blocker and do not imply the change passed LangChain's suite.
- Update the existing [evidence files](validation/) with the result for the final changed code and report the result in the pull request or handoff. Do not substitute results from an earlier version.
- Keep each validation JSON file focused on its latest run. Preserve earlier results in Git history instead of appending repeated run histories or tool transcripts. Retain current failures, source hashes, and cleanup evidence. Keep the separately approved real-model result until a new model run is authorized.
