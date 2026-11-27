# VQ-03e1 — Task upload ảnh reference thành bản nháp riêng tư

Milestone này cho phép người dùng đưa PNG thiết kế vào Task hiện có để owner xem và tải xuống. Đây là WorkProduct `UI_UX_DESIGN` trạng thái `DRAFT`, không phải reference đã duyệt hoặc input cho TestRunner.

## Source và PR

- PR #74 trước thay đổi: `28a148018d1c1c92dcc5312817cd231600ad921c`, tree `fa5aa4609f5d6bc930c76dd9fb09d87a18ade83a`.
- Source commit đã kiểm thử: `8e70ca21cfec8ea8fbbc02ee4c1bf5c54321169e`, tree `17a4e21b503eff0395b961ece07d4cfda7e46a32`.
- Hai source commits `4c671c1412d256e1b1b8a16ab6edeec6ce97f87a` và `8e70ca21cfec8ea8fbbc02ee4c1bf5c54321169e` được đẩy fast-forward vào branch draft PR `agent/codex/visual-qa-73`; head sau push là SHA trên. PR vẫn draft.
- Source files: `app/services/visual_qa_drafts.py`, `app/main.py`, `app/templates/task_detail.html`, `tests/test_visual_qa_drafts.py`, `docs/VISUAL_QA_HUONG_DAN.md`.
- Không sửa `PROJECT.yaml`, repo/worktree của sản phẩm, schema/DB migration, TaskDecisionService hoặc TestRunner. Không tự bật opt-in, phê duyệt ảnh, tạo stage/status, chạy Builder/TestRunner hoặc thay reference đang cấu hình.

## Red / green

RED chạy trên archive sạch của source trước sửa (`28a148...`, tree `fa5aa...`) cùng test regression mới. Lệnh trả exit `1`: endpoint upload chưa tồn tại, HTTP `404` trong khi test cần `303`. JUnit: 1 test, 1 failure, 0 error/skip; thời điểm `2026-11-26 22:17:13 +07:00` (15:17:13 UTC). Log/JUnit đã được lưu thành bản sanitized: `red.log`, `red.xml`.

Replay RED: từ root repo trước khi áp source commit, đặt `RED_DIR` thành thư mục scratch rỗng:

```sh
git archive 28a148018d1c1c92dcc5312817cd231600ad921c | tar -x -C "$RED_DIR"
cp tests/test_visual_qa_drafts.py "$RED_DIR/tests/"
cd "$RED_DIR"
DOCKER_HOST=unix:///tmp/pf-vq03e1-no-docker.sock \
  "$TASK_VENV/bin/python" -m pytest -q \
  tests/test_visual_qa_drafts.py::test_task_upload_creates_private_unapproved_draft_and_preview_download \
  --junitxml=red.xml
```

GREEN chạy trên source commit `8e70ca...`, tree `17a4e2...`, worktree sạch trước/sau. Python 3.14.4, pytest 9.2.0. JUnit bắt đầu `2026-11-26 22:31:23 +07:00`, log hoàn tất `22:33:28 +07:00` (15:31:23–15:33:28 UTC). Kết quả JUnit: **138 passed, 0 failed, 0 errors, 0 skipped**, suite time 124.814 giây. Có một test browser subprocess bị loại bằng `-k not test_visual_gate_runs_through_real_test_runner_subprocess_and_pins_current_head` vì prerequisite Chromium pin chưa có; không có browser result nào được claim. JUnit ghi 138 test; một test node bị deselect.

Lệnh replay trong worktree ProjectFlow đã cài dependencies:

```sh
DOCKER_HOST=unix:///tmp/pf-vq03e1-no-docker.sock \
  "$TASK_VENV/bin/python" -m pytest -p no:cacheprovider -q -ra \
  tests/test_visual_qa_drafts.py \
  tests/test_visual_qa_task_panel.py \
  tests/test_visual_qa_evidence.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  tests/test_visual_qa_runner_subprocess.py \
  tests/test_b1_read_isolation.py \
  -k 'not test_visual_gate_runs_through_real_test_runner_subprocess_and_pins_current_head' \
  --junitxml=focused.xml
```

`DOCKER_HOST` là socket task-local không tồn tại; các tests không gọi daemon Docker. Đây là FastAPI/TestClient integration với DB và Git fixtures tạm, không phải browser E2E hay provider test. JUnit: `green.xml`; console: `green.log`; bản red tương ứng: `red.xml`, `red.log`.

## Hành vi và ranh giới

- Task page có form PNG/viewport/ghi chú cho từng Builder workspace, dù Task hiện opt-out. Form hiển thị rõ ảnh là nháp, chưa duyệt; UI không bật gate hoặc tự chạy test.
- Server yêu cầu MEMBER và CSRF theo auth mode hiện hữu; xác minh Task chứa workspace, workspace trỏ đúng registered repo/fingerprint và source đang clean ở SHA hiện tại. Source được kiểm tra lại sau khi ghi file để từ chối drift.
- Chỉ nhận PNG được parser hiện có giải mã/CRC kiểm tra, đúng một trong năm viewport cho phép và đúng pixel dimensions, tối đa 8 MiB và tối đa 10 draft/workspace. Middleware giới hạn toàn multipart request; ghi chú tối đa 500 ký tự. Filename/content-type từ client không được dùng để tạo đường dẫn.
- File có UUID do server sinh, ghi `O_EXCL`/`O_NOFOLLOW`, mode `0600` dưới state directory mode `0700`; đường dẫn thư mục được mở từng component không theo symlink. WorkProduct chứa task/workspace/repository, SHA nguồn lúc nhập, viewport, dimensions, SHA-256 và ghi chú nguồn; `project_id`/`change_id` rỗng, status `DRAFT`, approval `UNREVIEWED`.
- Preview/download chỉ truy xuất theo Task + workspace + WorkProduct đúng scope, xác nhận DRAFT/metadata/hash, đọc file giới hạn bằng file descriptor không theo symlink và kiểm tra SHA/dimensions lại. Trả `image/png`, `nosniff`, `private, no-store`; tên tải xuống cố định, không dùng tên client.
- PNG nháp chỉ tải về để owner kiểm tra, rồi owner tự đưa ảnh vào đúng branch/repo, cập nhật `visual_qa.references` và commit qua workflow bình thường. Bản nháp không được gate dùng.

## Các regression chính

`tests/test_visual_qa_drafts.py` có 16 cases: upload/preview/download và hash; form khi opt-out; CRC/truncated/SVG/dimension mismatch; viewport và body/file size limits; giới hạn 10 draft/workspace; filename/path traversal; wrong Task; byte tamper và symlink; duplicate; cleanup sau lỗi DB/source drift; symlink storage root; Task membership/tenant denial/unauthenticated read/CSRF/role; ghi chú HTML được escape. Bộ 122 test còn chạy Task panel, image evidence verifier, committed contract, TaskDecisionService required stages, TestRunner source identity, route read isolation và các execution contracts liên quan.

`python -m py_compile app/main.py app/services/visual_qa_drafts.py tests/test_visual_qa_drafts.py`, `git diff --check` và `git show --check` đều exit 0. Không chạy browser, full suite, provider, CI, Docker daemon, production, merge hoặc deploy.

## Rollback và tiếp tục

Rollback code bằng cách revert source commits theo thứ tự ngược: `8e70ca2`, rồi `4c671c1` trên branch PR. Không xóa ảnh nháp người dùng đã lưu như một phần rollback; nếu cần dọn test artifact, chỉ xóa state/temp do test sở hữu. PR #74 vẫn draft; gate A/B/C/E/F không thay đổi. Chromium runner, provider authorization, approved Nova design/equivalence, exact-SHA CI và incident disposition vẫn là blockers riêng.
