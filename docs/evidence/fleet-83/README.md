# Owner-authorized issue 83 pilot — 2026-11-25

Implementation started from verified remote main
`c1c9718913483ab71e4bd3ddd94339ba018cf4bc` in a separate clean worktree,
branch `agent/codex/fleet-permissions-83`. Initial implementation checkpoint:
`b9bedaa`. Existing main and Terminal MCP WIP were preserved.

| Node | Before | User-level action and measured after | Remaining blocker |
|---|---|---|---|
| HP | GH auth/API identity/helper/push permission pass; sudo fails; native Codex namespace denied | Real commit/push/SHA readback/remote delete pass; non-root Docker Codex file write and independent host readback pass; profile repeat apply no change and rollback/repeat rollback pass | Native sandbox `ACL_DENIED`; Claude `AUTH_REQUIRED`; Docker canary is file-only |
| Dell | `gh auth status` exits 0, but `gh api user` returns HTTP 401; anonymous repo response has null permissions | Versioned audit bundle staged in account home; local write/read/delete pass; existing helper/auth preserved | `AUTH_REQUIRED`, not established `GIT_WRITE_DENIED`; no remote push attempted |
| M910 | gh missing; sudo fails; Docker socket access denied | Official GH 2.105.0 verified per-user install, repeat install no change; local write/read/delete pass | GH and Claude `AUTH_REQUIRED`; Docker `ACL_DENIED`; no remote push attempted |
| NEC | gh missing; sudo fails; Docker available | Official GH 2.105.0 verified per-user install, repeat install no change; local write/read/delete pass | GH and Claude `AUTH_REQUIRED`; no remote push attempted |
| dell-5530 / local / mac17-macbook-pro / macbook | OFFLINE/no client | No mutation | OFFLINE |
| Windows | Terminal route degraded/unreachable; desktop route offline | No mutation, PowerShell runtime not tested | OFFLINE for execution |

All four sudo failures remain failures. HP/Dell/NEC reported no-new-privileges;
M910 required interactive authentication. No sudo/root repair was attempted.
Local read/write canaries on Dell/M910/NEC created a unique temporary directory,
wrote `PF83_LOCAL_OK\n`, read it back exactly, then removed only that file and
directory. These are filesystem canaries, not authenticated Git push canaries.

## HP proof

`hp-git-canary.json` records the actual normal remote push of
`agent/codex/pf83-canary-6cfebe4cf7fe43adaba75661ad979a53`, SHA
`35aeb7fd5aa5ea1afffb5de18c79e7a341913251`, equal readback, exact remote-ref
deletion and verified absence. Local branch retained; temporary worktree removed.

`hp-codex-canary.json` preserves the failed native run: process exit 0 but no
requested artifact, with namespace ACL failure. `hp-container-canary.json`
preserves the first failed Docker attempt: CLI companion executable missing,
again process exit 0 without the artifact. Neither is a successful task.
`hp-container-canary-final.json` records the corrected binary-directory mount:
12,965 ms, file `PF83_CLI_WRITE_OK\n` independently read from the host,
SHA-256 `aba40a8b57668b9412189df66c77056f2bf8c2326e21c3dd618076b71664fccb`.
The existing local image was pinned by ID; no image pull or service restart.
The canary worktree/branch remains a durable checkpoint. Its trust profile was
rolled back; the private backup remains in the account state directory.

## Remote installation and report provenance

Code-only pilot bundle SHA-256:
`721fd0b46a6d9e3e31af367d36b486d5abc22e61356b39ad207c6b891ce89d5d`.
Each node staged it under `~/.local/share/projectflow/fleet-pilot-83`.
No credentials were included. M910 used the pre-existing approved user SSH
configuration; Dell/NEC used dedicated Terminal MCP generic shell sessions.
The system SSH include had invalid ownership/mode; it was not changed.

Official GH 2.105.0 archive SHA-256:
`79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f`.
Only M910/NEC needed installation. Existing login files were not copied or
modified. Private install receipts support exact uninstall with `rollback-gh`.

Raw JSON reports here retain the first implementation's classifications so
history is not rewritten. In those reports, GH exit 4 was `PROBE_FAILED`; it is
authentication-required. Dell's null push permission was initially classified
`GIT_WRITE_DENIED`; HTTP 401 on the identity probe means authentication is the
first gate and write authorization is unknown. Remote home directories were
audited for node inventory, not approved as worktrees. Thus their workspace
check is intentionally not PASS. The HP initial external-config-symlink result
was corrected to allow only symlinks resolving inside the owned workspace.

## Verification and unresolved acceptance

The initial focused suite passed 26 tests. Full-suite results and final review
receipt are recorded separately with the final implementation checkpoint.
Fixtures cover actual subprocess timeouts, prompt suppression, dirty/untrusted
worktrees, private/idempotent profile rollback, a real local bare-remote push
cycle, container isolation arguments and verified install/uninstall.

No production changes, Actions dispatch/enable, force push, old-work recovery,
or interruption of existing Visual QA/Zalo/other sessions occurred. Harmless
Terminal MCP node/session reads and the dedicated shell command receipts were
observed. A submission receipt alone was never counted as completed work.

Issue 83 is not accepted as complete. Required remaining work includes node-local
owner authentication, authenticated remote branch canaries, offline macOS/Windows
runtime proof, verified connector tool grants, and native sandbox resolution
through an approved administrative path if native execution is required.

## Final implementation and operational observations

Runtime code checkpoint `47ed370` adds exact effective push-destination checks,
an advertised-SHA deletion guard, managed-policy refusal in Docker mode, core
Windows identity checks, recoverable interrupted GH setup and centralized Docker
ownership checks. Fresh final HP proofs are `hp-release-git-canary.json` and
`hp-release-cli.json`: remote SHA `6c9f751e036ce8b05b4fb2fd4135b8e343929b06`
matched and was deleted; file canary completed in 15,243 ms and host readback SHA
was `02697bdb39656c49d636a62450a8d1046258525bb63c73307300869d5edff555`.
The profile was again rolled back. Earlier proof is checkpointed locally in
branch `agent/codex/pf83-final-cli`, commit `7f39960`.

Final release bundle SHA-256:
`9dadc17c9787e93c478c43dc8624fb81a1e9c5e6edcf0ff5e49b95da4b845d5b`.
Dell and M910 verified and staged it in their pilot directory. NEC initially verified
the preceding `9f7002d` bundle, SHA
`643e912491fb44763e99470f2a42f52cb07fa3a26739dd230f1ba2ba251ccfc1`,
then returned `NODE_UNREACHABLE` before final update dispatch. A later node recheck showed it online with the old audit session missing. A new
dedicated shell staged and verified the final release successfully. No old task
was redispatched. The `macbook` node also returned online reporting Linux; it is
ONLINE_NOT_AUDITED, not evidence of macOS runtime support.
Both prior audit bundles remain available as code-only backups on the nodes.
All three online-node CLI help probes confirmed the selected Codex/Claude flags.
M910's noninteractive SSH PATH needed the task-local `~/.local/bin` prefix;
no global PATH or login configuration was changed.

One regression fixture initially used a GitHub placeholder push URL instead of
a local bare remote. The pre-fix code attempted that push; it was rejected, no
remote-tracking ref was created and a subsequent API probe returned HTTP 404.
The fixture was corrected to disposable local bare remotes. This test mistake
is recorded rather than represented as an authorized remote canary. The final
guard rejects differing or multiple effective push destinations before making
a branch/worktree, and its local regression passes.

Dedicated audit-shell cleanup returned `CONFIRMATION_REQUIRED`. The exposed
Terminal MCP tool schema has no `confirm`/`args` parameter even though the server
response requests one. No alternate deletion mechanism or other session was
touched; `pf83-audit-dell` and the replacement `pf83-audit-nec-final` were left idle;
the original NEC shell disappeared during the transient node outage.
This cleanup API limitation remains open.
