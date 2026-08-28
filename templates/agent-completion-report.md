# Agent Completion Report

When you (Claude, Codex, or any coding agent working in an Agent
Workspace) consider your source changes for a task complete, report back
in this exact format. This is the report the user pastes into **+ Agent
Report** on the Agent Workspace or Task Detail page (`HOW_TO_VERIFY`
becomes numbered steps, one per line; the rest map to their matching
field).

```text
WORK_STATUS:
READY / FIX_REQUIRED

WHAT_CHANGED:
...

AUTOMATED_TESTS:
...

HOW_TO_VERIFY:
1. ...
2. ...

EXPECTED_RESULT:
...

TEST_DATA:
...

RUNTIME_REQUIREMENTS:
NONE / BACKEND / FULL / HARDWARE

RISKS:
...
```

## Field meaning

- `WORK_STATUS` -- `READY` if you consider the source change complete and
  ready for verification/integration, `FIX_REQUIRED` if you know it still
  needs work. This is your own assessment, independent of the workspace's
  `Mark Ready` button (a human still decides when to press that).
- `WHAT_CHANGED` -- one or two sentences a non-technical reader can use as
  the verification goal. Not a diff summary.
- `AUTOMATED_TESTS` -- what you actually ran (`preflight`, `test`, etc.)
  and the result. Never claim a test ran if it didn't.
- `HOW_TO_VERIFY` -- the exact manual steps a human should follow against
  the running sandbox to confirm the change works, one step per line.
  Write steps in terms of the actual behavior change (a login flow, a
  specific button, a specific field) -- never generic filler like "test
  the app". Workspace Manager never invents these steps for you; if you
  omit this field, the UI shows an honest empty state instead of guessing.
- `EXPECTED_RESULT` -- what a human should observe if the change is
  correct.
- `TEST_DATA` -- exact usernames/passwords/IDs/fixtures needed to follow
  `HOW_TO_VERIFY` (e.g. the specific test account and its credentials).
- `RUNTIME_REQUIREMENTS` -- the sandbox profile this change actually needs
  to be verified (`NONE` if it can be judged from the diff alone,
  `BACKEND`/`FULL`/`HARDWARE` otherwise). This is a recommendation for the
  human choosing a Sandbox profile, not something Workspace Manager
  enforces automatically.
- `RISKS` -- anything a reviewer should specifically watch for (a
  behavior that could regress, a migration, a security-sensitive change).

## Rules

- Never claim `WORK_STATUS: READY` if you know required tests are failing
  or you didn't actually verify the change compiles/runs.
- Never invent `HOW_TO_VERIFY` steps for a change you didn't make --
  report only what this workspace's diff actually changed.
- `READY` here (and the workspace's own `Mark Ready`) means only "source
  change complete, ready for verification/integration" -- never "sandbox
  tested", "manually verified", "integration passed", or "ready for
  main". Those are separate, later signals a human or the Integration
  Agent records independently.
