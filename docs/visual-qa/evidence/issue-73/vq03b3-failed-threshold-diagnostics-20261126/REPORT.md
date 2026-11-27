# VQ-03b3 — ảnh chẩn đoán cho visual threshold FAIL

## Kết quả

- Bắt đầu từ PR #74 draft `56ed8b31a35dc7ba0c28a146b434871d5c09670f`, tree `21fd3f4c28dce7f1dfde20d6237f4c18c7b5ccdf`; branch task `agent/codex/vq03b3-failed-visual-evidence-20261126`.
- Source commit `2c62565cfb2e962ce6e18c2614e746df20fafdc5`, tree `dc7f5de4726914ba135671050fbfa5d14ae184ce`; diff chỉ gồm TaskRunner/evidence verifier/Task panel, guide và tests.
- Source commit được kiểm tra sạch trước final replay. `git diff --check` và `python3 -m compileall -q ...` đều exit 0.
- TDD red: fixture chạy qua `TestRunner` thật với manifest threshold mismatch tổng hợp; khi app source được tạm hoàn nguyên về trạng thái trước sửa, stage vẫn FAIL nhưng không có `PROJECTFLOW_VISUAL_DIAGNOSTIC_SHA256`/ảnh lưu. JUnit 1 test, 1 fail, 0 error/skip, exit 1, 2026-11-26 17:14:05 ICT (`pre-fix-red.xml`, `pre-fix-red.log`). File red được thu lại sau green để lưu baseline có thể replay; lần đầu quan sát test đỏ xảy ra trước khi viết implementation.
- Final replay trên source commit: 121 passed, 0 failed/error/skipped, exit 0, 95.245 giây, 2026-11-26 17:11:58 ICT (`final-focused.xml`, `final-focused.log`). Có 109 regression trước đây và 12 ca mới, gồm mismatch diagnostic, tamper/symlink, opt-out, dirty source, exact bytes, read authorization/tenant denial, cùng TestRunner/TaskDecisionService giữ FAIL.
- TestRunner chỉ lưu run khi tiến trình trả exit 1, source còn sạch/đúng SHA, opt-in và toàn bộ viewport có manifest/metrics/PNG đúng contract, và nguyên nhân duy nhất là `percentDiff > threshold`; page error, request bị chặn, overflow, thiếu artifact hoặc sai hash bị loại. Evidence dùng token riêng `PROJECTFLOW_VISUAL_DIAGNOSTIC_SHA256` trong stderr. `test_runs.status` vẫn `FAIL`; không ghi success digest, không đổi `TaskDecisionService`, không cho diagnostic mở khóa gate.
- Task panel hiển thị **“Không đạt — dùng để sửa”** và bốn PNG allowlist qua route hiện có `require_read_role('test_run', 'rid')`. Route kiểm tra opt-in, workspace/task, current clean SHA, latest run, exit 1, manifest digest và đọc PNG no-follow có kiểm hash trước/sau. Tenant/unauthenticated, opt-out, stale/dirty, tampered, symlink và path không allowlist bị từ chối.

## Replay

```bash
DOCKER_HOST=unix:///tmp/projectflow-vq03b3-no-daemon.sock \
  <TASK_VENV>/bin/python -m pytest -p no:cacheprovider -q -ra \
  --junitxml=<EVIDENCE_DIR>/final-focused.xml \
  tests/test_visual_qa_task_panel.py \
  tests/test_visual_qa_evidence.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  tests/test_b1_read_isolation.py \
  tests/test_b03_authz.py \
  tests/test_engineering_lifecycle_ui.py::test_existing_task_detail_page_still_works \
  tests/test_visual_qa_runner_subprocess.py::test_test_runner_retains_synthetic_threshold_diagnostic_without_passing_gate
```

The last test is explicitly **synthetic**: it exercises TestRunner persistence and TaskDecisionService with locally generated, hash-consistent failure artifacts. It is not a browser/UIFix run and is not evidence of a real screenshot comparison.

## Browser boundary

Pinned UIFix verification succeeded at v1.2.6 / commit `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`; Node package was Playwright 1.60.0. Chromium 153.0.8010.12 binary SHA-256 was `8c599d43aec53f2460a31ae2f4af6bd863f8258b34ff519564bc5d4726bfaa1e`. The bounded smoke attempt failed before producing a usable screenshot: Chromium logged GPU launch error 1002 and `GPU process isn't usable`; Playwright's emitted launch argv included its default `--no-sandbox`. No such flag was explicitly added by the task, and no further browser launch was attempted after observing it. Therefore this milestone does not claim real browser or CLI evidence.

## Prior evidence correction

Re-hashed VQ-03b2 `pre-route-red.xml`: actual bytes and its `SHA256SUMS` entry are `6b4144be193aa1d60ea528b7c927c5b41a623efa9d600e1301ae90a468165078`; `run-summary.json` had recorded `843dc45d33178491669d245297b84cc5d8d9a9fb7a65b4d14c6ce15f98326c84`. The prior summary field was stale. The summary was corrected to the observed file hash and its manifest entry recomputed; both are included in the evidence update.

## Limits

No full suite, provider, shared Docker daemon, CI, production, merge or deploy was used. This is a local UI repair aid only. A/E/F and approved NovaRetail B/C remain unchanged and unqualified. PR #74 remains draft.
