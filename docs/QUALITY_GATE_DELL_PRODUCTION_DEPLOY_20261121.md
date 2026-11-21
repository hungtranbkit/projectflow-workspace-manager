# Quality Gate — Dell production code deployment and risk assessment (21 Nov 2026)

## Deployment truth

**The new ProjectFlow Workspace Manager code is DEPLOYED and RUNNING on the real Dell Linux production host. The mandatory Quality Gate policy is NOT YET ENFORCING merges for production tasks.**

- Host: Dell Latitude 5511, accessed via authenticated SSH from M910 after a Tailscale ping succeeded. Prior Terminal MCP node status was offline/overloaded.
- Systemd user service: `workspace-manager.service`, status **active**.
- Endpoint: `http://127.0.0.1:8765/health` -> HTTP **200** and `{"status":"ok","auth_mode":"none"}`.
- Running process working directory (verified via `/proc/<MainPID>/cwd`):
  `/home/dell/workspace/projectflow-qualitygate-release-20261121`.
- Exact running source commit:
  `0858c0f4b5ce473063daba2ec37ac0e564b03a5f`, from
  `integrate/quality-gate-main-20261121`.
- Original working repository stays at
  `/home/dell/workspace/projectflow-workspace-manager` and was **not reset, merged, or overwritten** (it already had local modified/untracked files).
- Original production SQLite database remains at
  `/home/dell/workspace/projectflow-workspace-manager/data/workspace-manager.db`, preserved by an explicit systemd drop-in:
  `~/.config/systemd/user/workspace-manager.service.d/90-qualitygate-release.conf`.
- Effective systemd `WorkingDirectory` and `ExecStart` point at the new release code; `WORKSPACE_MANAGER_ROOT` and worktree root still point at the existing workspace root.
- The feature is deliberately **opt-in and OFF** in production:
  `PROJECTFLOW_QUALITY_PILOT_TASK_IDS=`
  and `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=0`.
  None of the seven current tasks matches the only reviewed monitor-report test policy; there is no independent production signing receipt. It would be false to claim protected production merges merely because the module is present.
- GitHub `main` was not updated by this deployment, no PR created and no GitHub Actions run initiated.

## Measured before / after

| Dimension | Before | After | Assessment |
| --- | --- | --- | --- |
| Running Workspace Manager code | Original local `8cf138c` | Release `0858c0f` | **Deployed** |
| Service | active | active | No persistent outage observed after planned restart |
| `/health` | HTTP 200 | HTTP 200 | PASS |
| `/api/tasks` | HTTP 200 | HTTP 200 | PASS |
| `/api/repositories` | HTTP 200 | HTTP 200 | PASS |
| Production task state | 3 ACTIVE, 4 CANCELLED | 3 ACTIVE, 4 CANCELLED | Preserved |
| Production repositories | 5 | 5 | Preserved |
| SQLite integrity | `ok` | `ok` | PASS |
| `/health` median latency, 15 sequential localhost requests | **1.05 ms** | **1.53 ms** | 0.48 ms slower, ~46% on a very small, unpaired sample; no significant performance inference |
| Exact-SHA signed QA / merge blocking feature available in app | not in original source | included in release | **New capability**, only enforced on explicitly selected tasks with valid signer config |
| Actual selected production task IDs | none | none | Safe default: no false blocks, but **not production enforcement** |
| Docker-isolated pinned QA | unavailable in old source | new code includes tested Docker runner | Engineering improvement, require independent QA signer/receipts to enforce |

Independent QA evidence:
- NEC accepted 90/90 regression and runtime-isolation tests including real no-network read-only Docker pytest (code commit `2d23d8e`).
- Dell preflight used a **copy** of the real production DB in `/dev/shm`. New application's Health, Tasks and Repositories API all returned 200, exposing 7 tasks and 5 repos. Dell focused signature/security tests: **23/23 PASS**.
- A green HTTP service was started at `127.0.0.1:18771` against the copied DB and passed health/tasks/repos verification. It was later stopped after production cutover.
- Dell root disk reached 0B available before upgrade; operating system still reserves a small pool for root. The server could not safely upgrade without reclaiming space. Storage maintenance described below remains important to operational continuity.

## Backup, rollback and runtime proof

- Local consistent SQLite backup plus pre-upgrade user unit:
  `/home/dell/workspace/projectflow-qualitygate-release-20261121/rollback-20261121/`
  (SQLite `PRAGMA integrity_check=ok`).
- Off-host consistent production DB backup at
  `/home/mesflow/workspace/dell-disk-relief-20261121/projectflow-predeploy.db`
  on M910 (SQLite integrity OK, 7 tasks).
- To rollback **code without overwriting newer production records**:
  1. Remove the single quality gate drop-in
     `~/.config/systemd/user/workspace-manager.service.d/90-qualitygate-release.conf`.
  2. `systemctl --user daemon-reload`.
  3. `systemctl --user restart workspace-manager.service`.
  4. Verify `/health` on localhost:8765; only consider restoring the old DB backup if an operator has separately confirmed data damage and reconciled post-upgrade writes.
- No untrusted deployment key or QA signing private key was placed in the production service.

## Disk capacity restoration and caveats

- Dell root ext4 was 100% full with 0 bytes available, likely contributing to Terminal MCP `OperationalError`/OFFLINE.
- Authenticated SSH via M910 `~/.ssh/terminal_mcp_controller` worked when other SSH credentials failed.
- Only regenerable user caches (inactive Playwright/Puppeteer browser binaries, desktop tracker cache and old Android CLI archive) were pruned. No production database, Docker volume, live container, or model data deleted.
- **7,326 Terminal MCP raw log files older than 30 days (about 3.15 GiB)** were transferred in a compressed 201,124,009-byte archive to M910, verified using zstd integrity, SHA-256
  `663ca4cc70e48e619332aacd9d7276ed60ba2e815d607ada1d9c98b068e65a43`, and exact file list, then deleted on Dell. Current logs and `session_knowledge.db` preserved.
- The available disk space temporarily recovered to ~3.1 GiB but then dropped again below 1 GiB as other workloads wrote data. A large, recently written Facebook Sales Radar SQLite WAL was observed; **it was not modified or truncated**. This is a **separate ongoing infrastructure risk**, and the new ProjectFlow code deployment does not fix the disk pressure.
- Five October Facebook Sales Radar staging/backup datasets were checked as not open and a compressed off-host archive was initiated, but **no original FSR files have been deleted**; do not treat a partial archive as completed or verified.

## Further hardening needed for real merge enforcement

1. Configure a distinct, restricted signing principal and protect the Ed25519 private key outside all coding-agent worktrees and their runtime.
2. Run the reviewed, pinned policy tests in the no-network read-only Docker runner; sign exact task/repo/commit only after actual PASS and deliver signed receipt/public key via an authenticated channel. Complete end-to-end evidence for `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1`.
3. Select a real production task whose changed code and test suite match a reviewed policy. The current monitor-only pilot cannot truthfully certify arbitrary tasks.
4. Configure GitHub merge rulesets to prevent bypass via direct GitHub PR merges. Current `.github/workflows/ci.yml` runs on Pull Requests and pushes to main; do not casually open a PR or merge while avoiding Actions.
5. Continue disk capacity/FSR WAL analysis and keep free space above a safe operational threshold.

**Conclusion: Dell production application code deployed, data preserved and smoke tests passed; mandatory Quality Gate enforcement remains intentionally disabled pending independent signer and task-specific policy.**
