# Review resolution for runtime checkpoint 47ed370

`review-original.json` is the complete review receipt for the original `b9bedaa`
snapshot, not a claim that its findings remained open after repair. It used
correctness, security, reliability, testing, project-standards, maintainability,
agent-native and adversarial lenses, then synthesis and independent validation.
The requested Claude cross-model execution failed expired authentication;
the review recorded that failure and used the skill's local adversarial fallback.

| Original finding | Resolution and evidence |
|---|---|
| Docker execution outside runtime service | Commands/run/cleanup moved to `sandbox_runtime.py`; `wm-projectflow-fleet<uid>-<uuid>` namespace and `verify_owned` plus exact label check. Final live HP canary passed. |
| Docker omitted host managed policy | Preflight refuses when managed policy files are present; regression fails before fix and passes after. Audit no longer claims verified effective policy. |
| Windows core bypassed wrapper guards | Core identity invokes bounded noninteractive owner/elevation/private-state ACL check too; negative fixture passes. Live Windows remains unverified. |
| Interrupted GH installation unrecoverable | Private intent receipt precedes final binary creation; matching partial install resumes or rolls back; interrupted-link fixture passes. |
| SSH incorrectly required HTTPS helper | Exact approved SSH origin reported separately; actual write canary still required. No SSH authorization inferred from public read. |
| Canary recovery paths untested | Real bare-remote rejection preserves local checkpoint; concurrent advancement preserves newer remote branch. |
| Canary deletion race | Private task-owned pre-push hook checks server-advertised SHA; ordinary Git deletion carries that old OID for server comparison. No force flag. |
| New terminal entry lacked gate test | Integration fixture proves blocked preflight never opens terminal and success invokes the absolute versioned runner. |

Follow-up scoped review found the fetch/push URL distinction. Both audit and
canary now require exactly one effective push destination equal the verified
fetch destination, before any worktree/ref mutation. Its regression uses two
disposable local bare repositories. The reviewer inspected the subsequent
fixes and confirmed that the reported runtime mechanisms were addressed;
that scoped inspection is separate from the original full-review receipt.

Final focused command on runtime checkpoint 47ed370:

```sh
env -u PYTHONPATH PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest \
  tests/test_fleet_permissions.py tests/test_terminal_launcher.py tests/test_git_integration.py
```

Result: **42 passed**, 12 dependency deprecation warnings, 1.45 seconds.
`./scripts/preflight.sh` and `./scripts/build.sh`: PASS.
No review or test result here proves fleet-wide issue acceptance.
