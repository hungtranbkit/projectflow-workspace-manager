# CI blocker analysis — PR 37

Run `42484106100` at `55345f1` reported 192 failed, 985 passed, seven
skipped, and 20 errors. The same workflow on clean `main` at `8cf138c`
reported 191 failed, 983 passed, seven skipped, and 20 errors. Comparing
test node IDs showed one platform-message failure disappeared and two
unrelated failures appeared; the Herdr association tests themselves passed.

## Failure groups

| group | CI result | root cause |
|---|---:|---|
| architecture lifecycle | 36 failed | test doubles replaced `runner` but the invoker rejected missing `claude` before reaching it |
| planner | 28 failed | same test-double contract defect |
| spec lifecycle | 34 failed | same root failure plus downstream missing work products |
| test design | 35 failed | same root failure plus downstream missing test cases |
| review/fix | 17 failed | same root failure; two cases also exposed a missing `fix_task_id` on launch failure |
| release/waves/golden/incidents/acceptance | 21 failed | downstream review/planner cascade |
| lifecycle UI | 1 failed, 20 setup errors | fixture setup parsed the failed lifecycle response as JSON |
| productization audit | 3 failed | downstream invoker behavior |
| baseline reproduction race | 1 failed | independent pre-existing concurrency failure |
| repository rename | 1 failed | platform-specific git error text assertion; not introduced by runtime association code |

The deterministic fix injects both halves of the provider boundary: a fake
runner and a fake executable resolver. It never returns a fabricated model
result; each test still supplies and validates its structured envelope through
the production parser and service stack. Tests that intentionally contact a
real agent are marked `live_agent`, excluded explicitly from deterministic CI,
and available through a separate manual workflow on an isolated, credentialed
self-hosted runner. That workflow checks the operator-supplied exact Claude CLI
version before running and does not download or authenticate a CLI itself.

No secrets or external agent credentials are required by the normal GitHub
Actions job. Missing production CLIs still fail closed in production code.
