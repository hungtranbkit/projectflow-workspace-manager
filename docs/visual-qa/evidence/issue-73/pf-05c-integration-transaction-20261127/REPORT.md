# PF-05c — xác minh trước khi promote integration candidate

Ngày 2026-11-27. Đây là milestone sửa lỗi an toàn release trong IntegrationService; không phải nghiệm thu toàn bộ issue #73.

## Source và phạm vi

- PR #74, branch `agent/codex/visual-qa-73`; source commit `b0e55c5a15fef8e7515c8effc0c7d3296f1215a4`, tree `20fc76b6bb2ade31fe61d094e91ee7982706b612`, parent `e96a3d0b5560a1d5e5376538a94670cc102f8de3`.
- Chỉ sửa `app/services/integration_service.py` và `tests/test_release_pipeline.py`.
- Integration nay giữ merge candidate trong probe riêng, xác minh đúng candidate SHA trước khi promotion, kiểm tra lại readiness/workspace/task SHA và target SHA, rồi dùng `git update-ref <ref> <candidate> <expected-old>` làm compare-and-swap. Verification thất bại, exception, task drift, hoặc target race không tiến ref. Rejection được lưu là `FAILED`, `merged_commit=NULL`, WorkProduct `REJECTED`, không phát `TASK_INTEGRATED`. Nhánh non-visual vẫn dùng verifier hiện có.
- Giới hạn còn lại: Git CAS và SQLite không phải một transaction nguyên tử. Nếu ghi DB lỗi sau CAS, không rollback ref mù vì writer khác có thể đã tiến ref; cần reconciliation có kiểm soát. Không khẳng định atomic Git+DB.

## Đo đỏ/xanh

- RED trên baseline parent `e96a3d0b5560a1d5e5376538a94670cc102f8de3`: 3 regression tests, 3 failed, 0 errors/skips, exit 1. Repro dùng Git repo và SQLite tạm, actual subprocess `true`, opted-in valid synthetic PNG/contract. Ca visual chứng minh required visual evidence fail dù non-visual stages thành công, nhưng target ref vẫn đổi; hai ca khác chứng minh task source drift và concurrent target writer cũng bị chấp nhận/ghi đè trước fix. JUnit tổng thời lượng 10.906s; thời điểm kết thúc được ghi là 11:27:19 UTC. Thời điểm bắt đầu của lần tổng hợp không được lưu, nên không suy diễn.
- GREEN exact source commit `b0e55c5...`: 5 transaction tests passed (0 fail/error/skip), exit 0, 11:43:33–11:43:51 UTC.
- GREEN exact source commit `b0e55c5...`: 5 module-focused regression set, 78 passed (0 fail/error/skip), exit 0, 11:45:01–11:47:13 UTC. Gồm `test_release_pipeline.py`, `test_integration_contract_probe.py`, `test_visual_qa_contract.py`, `test_visual_qa_execution.py`, `test_task_decision_required_stages.py`.
- Môi trường: Python 3.14.4, pytest 9.2.0, venv sẵn có; PATH giới hạn `/usr/bin:/bin`, HOME/TMPDIR riêng cho task, `DOCKER_HOST` và `DOCKER_CONTEXT` trỏ giá trị không tồn tại. Không provider, browser, Docker daemon, mạng ngoài, production, deploy, hay full suite.

### Lệnh tái chạy

```bash
<existing-venv>/bin/python -m pytest -q -ra \
  tests/test_release_pipeline.py \
  tests/test_integration_contract_probe.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  --junitxml=<task-owned-artifact-dir>/green.xml
```

Chạy từ root repo ở source commit `b0e55c5...`, với các biến môi trường cô lập nêu trên. JUnit và log trong thư mục này là bản đã thay các absolute host path bằng nhãn portable; hash manifest xác thực đúng các byte đã publish.

## Kết quả và trạng thái

- Lỗi promotion trước verify đã có regression đỏ và fix xanh, gồm CAS chống ghi đè target writer và kiểm tra task SHA/readiness trước promotion.
- Chỉ dùng synthetic evidence trong test contract; không có ảnh/browser thật, không chứng minh NovaRetail visual acceptance.
- Không chạy full suite, CI exact-SHA hiện không có check run được xác minh trong milestone này. Gate A/E không được nâng PASS; các gate A–F tổng thể giữ nguyên. PR #74 vẫn draft, không merge/deploy.
- Rollback: revert riêng source commit `b0e55c5...` nếu cần; evidence-only commit độc lập. Không reset hay sửa `main`/WIP.
