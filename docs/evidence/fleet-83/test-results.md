# Local verification record

Runtime code checkpoint: `47ed370`. These results are not fleet acceptance.

| Run | Observed result |
|---|---|
| Initial focused implementation tests | 26 passed |
| Final focused fleet/terminal/Git tests | 42 passed, 12 dependency warnings, 1.45 s |
| Final `./scripts/preflight.sh` | PASS |
| Final `./scripts/build.sh` | PASS |
| Initial full `./scripts/test.sh` with inherited Terminal MCP PYTHONPATH | Collection failed (43 errors); wrong inherited tests package |
| Full `env -u PYTHONPATH ./scripts/test.sh` | 656 passed, 8 failed, 1 skipped; interrupted at project 900 s budget (934.54 s including interrupt/teardown) |
| Corrected PATH rerun of release/baseline failure group | 9 passed |
| `env -u PYTHONPATH PATH="$PWD/.venv/bin:$PATH" timeout --signal=INT 900 .venv/bin/python -m pytest -k 'not real'` | 954 passed, 3 failed, 1 skipped, 66 deselected; timeout exit 124, pytest reported 889.14 s excluding startup |

Broad suites began while review repairs were in progress. They are diagnostic
coverage, not exact-final-commit qualification; the focused final run and live
HP canaries were repeated against the final runtime source.

The first full run's real Claude architecture/builder tests failed. The builder
transcript reported expired login; no arbitrary security dialog was answered.
Other release/baseline tests invoked system Python without pytest and passed in
the corrected task-local venv PATH rerun. Their earlier failures remain failures.

Three remaining broad-run failures:

* `test_cross_repo_integration.py::test_backend_and_client_test_together_without_either_merging_to_main`:
  the disposable test DB recorded Docker `all predefined address pools have
  been fully subnetted` during compose network creation.
* `test_golden_e2e.py::test_golden_end_to_end_inventory_app_reaches_complete` and
  `::test_golden_bug_closed_loop_returns_to_healthy`: the fixture explicitly sets
  `PATH=/usr/bin:/bin`, then `/usr/bin/python3` reports `No module named pytest`.

The host/system Python and global Docker networks were not modified to hide
these failures. A read-only cleanup audit collected project names from only the
two task-owned disposable test runs (`pytest-1155`, `pytest-1166`) and compared
Docker compose ownership labels: **22 recorded projects, zero remaining
containers and zero remaining networks**. No global prune or other-session
cleanup was performed.

Failure fixtures cover missing auth, prompt refusal, sudo failure, permissions,
offline status, unsupported CLI, disk failure, dirty/untrusted worktrees,
Windows owner/elevation refusal, managed-policy preservation, error redaction,
subprocess timeout, interrupted install, blocked-before-dispatch, real local
push rejection, concurrent remote advancement, and mismatched push destination.
Focused success does not replace the incomplete broad gate.
