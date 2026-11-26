# VQ-03c — xem reference trước khi bật Visual QA

Thời gian test cuối: 2026-11-26 12:38:11–12:39:31 UTC (19:38:11–19:39:31 ICT).

## Nguồn và thay đổi

- Draft PR #74 lúc bắt đầu: `dfe2be82cfc5b397e7cbe9efd4bb7cd4eef7838c`, tree `7a23f50102febb434be5c19950204b5dc181ae50`.
- Worktree task-owned ban đầu ở local commit `db941199ce25578ee0b8270f0e588c2ef92743f2`, cùng tree với PR head.
- Source commit đã test: `5bc2a4bc8505bb030c20684c9713dc9a380d35b1`, tree `bf0d59c23a6139a789f105cc439dd408d8f052ba`; worktree sạch trước/sau lượt test cuối.
- Thay đổi gồm `app/main.py`, `app/services/task_decision_service.py`, `app/services/visual_qa_evidence.py`, `app/templates/task_detail.html`, hai test file, hướng dẫn VN và `PROJECT_CONTEXT.md`.

## Hành vi

- Task panel đọc contract/reference cho Task opt-in lẫn opt-out. Ảnh chỉ hiện khi contract hợp lệ, source sạch ở SHA hiện tại, reference là PNG thường (không symlink), kích thước đúng viewport và đọc trong giới hạn byte.
- Route preview chỉ nhận Task/workspace/SHA/viewport. Nó lấy file từ contract đã commit trong workspace gắn với repository đã đăng ký, dùng `require_read_role("task", "tid")`, kiểm tra fingerprint và source SHA sạch trước/sau khi đọc. Trả `image/png`, `nosniff`, `private, no-store`; lỗi trả 404 tổng quát, không lộ đường dẫn.
- Panel cho xem route, threshold, viewport, SHA-256 reference và SHA source hiện tại. Không có record approval thì ghi “Chưa ghi nhận — không đồng nghĩa đã được duyệt”. Không cho nhập path/URL, không sửa `PROJECT.yaml`, không upload.
- Opt-in dùng biểu mẫu trong cùng Task page và endpoint lưu prompt hiện có. Checkbox bắt buộc; handler thêm marker canonical mà không thay prompt đã nhập, không xóa marker đang có trong implementation prompt, không sửa brief. Quyền MEMBER và CSRF từ guard hiện tại vẫn áp dụng. Lưu không khởi chạy Builder/TestRunner; Run Test chỉ hiện khi TaskDecisionService xác định đã opt-in.

## Kiểm chứng

Lệnh đã chạy trên source commit nêu trên:

```sh
DOCKER_HOST=unix:///tmp/pf-vq03b3b-no-daemon.sock \
  "$TASK_VENV/bin/python" -m pytest -q -ra \
  tests/test_visual_qa_task_panel.py \
  tests/test_b04_csrf.py::test_visual_qa_prompt_optin_uses_existing_csrf_guard \
  tests/test_visual_qa_evidence.py tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  --junitxml="$EVIDENCE_TMP/focused-tests.xml"
```

Trong lần chạy thực tế, `$TASK_VENV` là venv task-local hiện có (Python 3.14.4, pytest 9.2.0); `$EVIDENCE_TMP` là thư mục scratch task-owned. Test result: **96 passed, 0 failed, 0 errors, 0 skipped**, exit 0; JUnit runtime 57.746 giây. Lượt này gồm preview opt-out, stale SHA, dirty source, viewport không cấu hình, symlink, opt-in giữ nội dung, không xóa marker, không tự tạo test run, CSRF, unauthenticated/cross-tenant denial, route PNG cũ, evidence/contract/execution, TaskDecisionService và source-identity.

`git diff --check` và `py_compile` cho Python source/test đã exit 0. Trước khi sửa, test mới đã được chạy và thất bại đúng vì opt-out page không hiển thị route reference; stdout của lượt RED chỉ còn trong execution transcript, không được giữ lại như raw artifact. Lượt GREEN cuối có raw sanitized log và JUnit trong thư mục này.

Không chạy full suite, browser/Playwright, provider, Docker, build/preflight, CI hoặc production. Vì vậy đây là kiểm chứng route/template/API bằng FastAPI TestClient và fixture repo/DB tạm; không phải browser screenshot hay end-to-end visual pass.

## Replay và rollback

Trong task worktree có venv tương thích:

```sh
TASK_VENV=/path/to/task-venv
EVIDENCE_TMP=/path/to/task-owned-evidence
DOCKER_HOST=unix:///tmp/pf-vq03b3b-no-daemon.sock \
  "$TASK_VENV/bin/python" -m pytest -q -ra \
  tests/test_visual_qa_task_panel.py \
  tests/test_b04_csrf.py::test_visual_qa_prompt_optin_uses_existing_csrf_guard \
  tests/test_visual_qa_evidence.py tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  --junitxml="$EVIDENCE_TMP/focused-tests.xml"
```

Rollback source bằng cách revert riêng commit `5bc2a4bc8505bb030c20684c9713dc9a380d35b1` trên nhánh task/PR; không reset nhánh khác. Không có runtime/service nào được khởi chạy.

## Gate status

Chỉ đây là cải tiến Task UI/opt-in và kiểm thử cục bộ. A full suite vẫn FAIL/chưa đủ điều kiện, B/C Nova reference/pilot vẫn bị chặn, E exact-head CI chưa PASS, F chưa được tái chứng nhận do sự cố harness trước đó. D không được nâng trạng thái chỉ từ milestone này. PR và issue vẫn phải giữ draft/open.
