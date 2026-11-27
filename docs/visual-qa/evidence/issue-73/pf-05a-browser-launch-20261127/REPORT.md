# PF-05a — chính sách launch Chromium có sandbox

Ngày 27-11-2026. Đây là thay đổi hợp đồng launch và regression deterministic; **không phải** chứng minh browser sandbox hoạt động trên kernel/runner.

## Nguồn gốc và thay đổi

PR #74 được kiểm tra ở head `572ef11df59bc2f4a7b1db25bb2478ea794add57`, tree `730c64c9958cc00fe8ff8fa5775e0929545aca5c`; remote head không đổi trước khi sửa. Trong Playwright 1.60.0 đã pin, kiểu API khai báo `chromiumSandbox?: boolean`; mã Chromium thêm `--no-sandbox` nếu `chromiumSandbox !== true`. Vì vậy caller bỏ option không đồng nghĩa với sandbox bật.

Tree ứng viên gồm 8 file triển khai/test/guide đã được chạy kiểm thử là `e2e6a368d72db4f0ae958a6da31cdd06b1c0b77f`. Evidence/context được thêm sau để giữ riêng identity của thay đổi đã test.

Tạo `tools/visual_qa/browser-launch.mjs` làm chính sách chung: chọn Playwright app nếu được chỉ định, nếu không dùng package pin UIFix; runtime thiếu hoặc thấp hơn 1.60.0 fail closed; launch truyền `chromiumSandbox: true`; lỗi launch không được retry. `visual-qa.mjs` và `smoke.mjs` dùng helper này. `CHROME_PATH` vẫn là executable override do operator chọn, không phải bằng chứng sandbox. Hai test fixture TestRunner sao chép helper cùng CLI để giữ cấu trúc project-local chính xác.

`toolchain-lock.json` bỏ fallback container chạy root/host-network; hướng dẫn tiếng Việt yêu cầu runner được duyệt, không `--no-sandbox`, không host-network, không Docker dùng chung. Không sửa `SandboxRuntimeService` hay AUTH_MODE=required.

## Bằng chứng kiểm thử

- Red trước implementation: `node --test tests/test_visual_qa_browser_launch.mjs`, 1 test, **fail/exit 1** vì CLI cũ không đi qua shared secure launch policy. Log trích xuất đã sanitize ở `red-node-sanitized.log`; thời điểm 2026-11-27 16:51:28 ICT.
- Green: `node --test tests/test_visual_qa_browser_launch.mjs tests/test_visual_qa_cli.mjs` — **10 passed, 0 failed, 0 skipped, exit 0**, 2026-11-27 16:59:25 ICT. Kiểm tra version, không fallback từ runtime app sai phiên bản, option launch bật sandbox, executable override vẫn giữ option, chỉ gọi launch một lần khi lỗi.
- Python: `python -m pytest -p no:cacheprovider -q -ra tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_task_decision_required_stages.py tests/test_visual_qa_runner_subprocess.py::test_test_runner_retains_synthetic_threshold_diagnostic_without_passing_gate --basetemp=<task-temp> --junitxml=<junit>` — **36 passed, 0 failed/errors/skipped, exit 0**, 2026-11-27 16:59:05–16:59:17 ICT (command window from preserved `python-run.txt`; previous report end time was incorrect). JUnit timestamp 16:59:07 ICT, reported test duration 9.535 s. Đây là regression contract/decision và test synthetic; không có browser hoặc Docker test.
- `node --check` cho ba module launch/CLI/smoke và `git diff --check` — exit 0.

Environment Python 3.14.4, pytest 9.2.0, PATH giới hạn `/usr/bin:/bin`, HOME/TMPDIR riêng, DOCKER_HOST trỏ tới socket task-local không tồn tại. Không provider/network call.

## Giới hạn còn lại

Browser canary và `smoke.mjs` **NOT RUN**: chưa xác minh runner cô lập được phê duyệt có thể thực thi Chromium sandbox. Chưa chạy `tests/test_visual_qa_mandatory_sandbox.py` vì test này dò/đụng Docker daemon; không dùng daemon dùng chung. `chromiumSandbox: true` chứng minh option được yêu cầu và không bị retry yếu hơn, không chứng minh kernel/runtime thực thi sandbox. Không tạo screenshot hay báo visual PASS trong milestone này.

## Replay

```sh
node --test tests/test_visual_qa_browser_launch.mjs tests/test_visual_qa_cli.mjs
PATH=/usr/bin:/bin <task-venv>/bin/python -m pytest -p no:cacheprovider -q -ra \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  tests/test_visual_qa_runner_subprocess.py::test_test_runner_retains_synthetic_threshold_diagnostic_without_passing_gate \
  --basetemp=<task-private-temp> --junitxml=<task-private-junit>
```

Không có production/runtime/CI mutation, không có browser launch, không đổi A–F. PR giữ draft. Bước cần owner/runner: cung cấp runner cô lập có hỗ trợ Chromium sandbox; sau đó chạy canary/smoke và test mandatory TestRunner trên đúng runner.
