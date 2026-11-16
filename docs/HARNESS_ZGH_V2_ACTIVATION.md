# Zalo Hub V2/V229 — Harness activation and durable-sync migration

## Immutable main goal

Chuyển Zalo Hub sang kiến trúc V2 (thiết kế V229 durable sync) để xử lý lỗi
bỏ sót tin nhắn text/ảnh/video/file, không gửi trùng; bảo toàn WAL/PIN/grants
và nghiệm thu source→Test trên S7 bằng tài khoản nhận độc lập.

This refers to the **selected V229 architecture** for the existing Android
Zalo Group Hub V2 delivery/verification engine, not a second competing rewrite.

## Safety boundary

- One Samsung S7 is the production device; one Zalo WebView and the existing
  row-bound Native Share remain authoritative for actual sending.
- Keep V228 send path and historical SharedPreferences WAL as source of truth.
  Never auto-replay SUBMITTED_UNCERTAIN or delete/reset WAL/PIN/grants.
- Do not send synthetic messages to customer/family groups. Use the already
  authorized Test2 → Test pair only after explicit device E2E permission.
- No production APK install, force rollout, route mutation, auth bypass,
  public release or merge of ZGH runtime PRs before exact-sha independent QA.
- Disabled shadow journal/observer are developed and audited separately;
  shadow capture is not an authorization to transmit.
- One parent monitor owns this goal; at most two concurrent atomic children
  (one implementation, one independent QA), each <=24 hours from creation.
  No automatic rescope that silently resets TTL; cleanup must be verified.

## Existing work; do not duplicate

- Design specification: Zalo Hub [PR #79](https://github.com/hungtranbkit/zalo-group-hub/pull/79).
- A1a SQLite durable journal/schema and transactional source coverage:
  [PR #81](https://github.com/hungtranbkit/zalo-group-hub/pull/81),
  currently draft. Independent review identified **P1 evidence-range hash
  conflict**: nonmatching evidence could inherit earlier COVERED status.
- A1b default-off shadow reader, active crawl-lease checks and Keystore HMAC:
  [PR #82](https://github.com/hungtranbkit/zalo-group-hub/pull/82).
  It is draft, based on A1a and must be reviewed after its own exact-sha.
- Reuse existing Codex/Claude sessions, branches and PRs only after verifying
  they are idle and not running another task. Do not open a duplicate worker
  just because Terminal MCP's queue-off start returned SUBMIT_CONFIRMED.

## Execution gates (one child = one verifiable output)

1. **A1a P1 FIX / review:** On PR #81, repair conflicting evidence-range hashes
   before any message write. Add negative tests for old coverage reused with
   a new range and an unchanged-source repeat. Run focused Node, JDK21 Java
   source integration and Android compile-only checks. Claude re-audits exact
   SHA. Report audit FAIL instead of advancing on test PASS alone.
2. **A1b shadow OBSERVATION:** Rebase/fix PR #82 onto audited A1a. Validate
   grant and actual active native crawl lease in the same transaction,
   document-epoch consistency, account/source scoped Keystore HMAC, idless
   HOLD, virtualization/older-history UNKNOWN and gap accounting. No dispatch.
3. **Text loss R1/R2:** Avoid NOT_FRESH drops while an attempt is in flight;
   an UNCERTAIN for one source must not freeze unrelated text. Persist each
   observed candidate before cursor advance. Never automatically repost an
   uncertain candidate.
4. **Single UI lease + recovery:** Fair serialization across reader, text,
   media, roster, native Share, and receipt checks. Bounded crash/restart,
   focus-loss, >6h, 128-item-capacity and per-source catch-up accounting,
   without claiming unread history is delivered.
5. **Production acceptance:** Under authorized device constraints, prove
   fresh Test2→Test text/image/video/file, source+destination parity, no
   silent miss/duplicate after restart and independent receiver-side proof.
   Preserve PIN/grants/WAL before/after and provide signed APK rollback.
   NO-GO until these evidence levels actually exist.

## Activating the existing monitor without disturbing other automations

Only `Automation_eec08a5752d08191b3de371065c27320` is approved for
reactivation here. The registry changes that ID from paused to parent, adds
the immutable main goal, max_children=2 and the original repo restriction.
The other 21 paused automations and the three currently active ones remain
unchanged.

After review, merge the Harness activation patch to `main`, update the clean
Dell deployment checkout and run targeted tests. Existing SQLite bindings
were created against the old paused registry, so invoke the **one-time,
idempotent** migration before running bootstrap or a timer checkpoint:

~~~bash
cd /home/dell/workspace/projectflow-harness-main-20261115
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
$PF scripts/harness_monitor_bridge.py activate-zgh-v2 \
  --automation-id Automation_eec08a5752d08191b3de371065c27320
$PF scripts/harness_monitor_bridge.py bootstrap
$PF scripts/harness_monitor_bridge.py status \
  --automation-id Automation_eec08a5752d08191b3de371065c27320
systemctl --user start projectflow-harness-reconcile-staging.service
~~~

Only after these checks succeed, update the existing ChatGPT Zalo Hub V229
automation prompt and enable it without altering its hourly schedule. The
activation migration appends an audit event and never activates another
automation, starts a coding agent or alters the S7. The monitor must inspect
the A1a/A1b work before its first dispatch and record an evidence-backed
checkpoint to PR #79.

## Runtime acceptance and limits

`HARNESS_MONITOR` status must show one ZGH parent with locked main goal,
`max_children=2` and zero new duplicate tasks. The timer remains active,
service last result success/exit 0. A real new task should have a Harness
SQLite ID and a verifiable backend, never an unproven central-controller
submission receipt.

Currently Harness's managed coding executor requires Python/pytest. Zalo
Hub uses Android Java/Gradle and Node tests. Do **not** pretend these are
supported by the Python code executor: use existing Codex/Claude review/PR
lanes with actual Node+JDK21 tests and separate technical checkpoints.
A future dedicated Android/Node runner needs independent acceptance before
being granted code-changing authority.

Rollback of monitoring, if necessary, means pausing the ChatGPT automation,
not deleting the V2 source journal or silently rewriting its stored main goal.
The SQLite activation audit and historical checkpoints stay preserved.
