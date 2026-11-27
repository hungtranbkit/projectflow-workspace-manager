# Replay on an approved isolated runner

From a clean checkout of source SHA `70d467ce7532d6f4fdcb9a238157daba3737afa1`, with the repository-declared test dependencies and approved browser/provider environment already available:

```bash
export VIRTUAL_ENV=/path/to/task-owned-venv
export PYTEST_ADDOPTS='-rs --junitxml=/path/to/task-owned-artifacts/full-suite.xml'
./scripts/preflight.sh
./scripts/test.sh
./scripts/build.sh
./scripts/smoke.sh
```

Keep the test selection unchanged. Before running provider tests, ensure their `~/.claude.json` trust setup is isolated from any other process/session; the current helper cleanup sweeps `/tmp/pytest-of-dell/` entries. Do not use Dell/HP states recorded in `REPORT.md` until the ownership/resource blocker is resolved.
