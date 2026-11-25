# Replay — PF-03e2

Exact PR source: `10455bb4b0b52beb2c06fc3fbd9b339183a49fc0` (tree `4c9011ec3e0694b87e889077819b2a195e7ce6be`). Changed test blob: `dd5db08eedda8f524a81753a35479f5575325e0f`.

Run inside the task-owned Noble Playwright image after transferring the clean source archive and pinned project-local UIFix runtime into the container. Browser revision 1223 is available at `/ms-playwright`; do not install Playwright 1.60 on Ubuntu 26.04 or substitute another revision.

```sh
export PROJECT_ROOT="$(pwd)"
export PYTHONPATH="$PROJECT_ROOT/.runtime/python-site-packages"
export VISUAL_QA_PLAYWRIGHT_FROM="$PROJECT_ROOT/.runtime/uifix-v1.2.6/node_modules"
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
export VISUAL_QA_RUNNER_EVIDENCE_DIR=.runtime/visual-qa-runner-evidence-pf03e2
export PF03E2_SOURCE_COMMIT=10455bb4b0b52beb2c06fc3fbd9b339183a49fc0
export PF03E2_SOURCE_TREE=4c9011ec3e0694b87e889077819b2a195e7ce6be
export PF03E2_PR_HEAD=10455bb4b0b52beb2c06fc3fbd9b339183a49fc0
python3 -m pytest -q -ra "$PROJECT_ROOT/tests/test_visual_qa_runner_subprocess.py" \
  --junitxml=.runtime/visual-qa-runner-evidence-pf03e2.xml
```

The evidence path must not already exist; the test uses `mkdir(exist_ok=False)`. The verified container was `--network none`, read-only, bounded, with no mounts. `SandboxedCommandRunner` ran direct-host mode inside it. This does not verify the mandatory Docker sandbox executor path.
