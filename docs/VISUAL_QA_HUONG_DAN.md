# Hướng dẫn Visual QA ảnh thiết kế → UI

Tài liệu này mô tả công cụ project-local cho tác vụ UI có ảnh tham chiếu. ProjectFlow chỉ bật gate khi prompt Task có dòng `VISUAL_QA_REQUIRED: true`; backend/firmware và Task không có marker tiếp tục dùng các gate hiện tại.

## 1. Cài tool và kiểm tra pin

Trong root của ProjectFlow:

```sh
python3 tools/visual_qa/bootstrap.py
python3 tools/visual_qa/verify_uifix.py .runtime/uifix-v1.2.6
python3 .agents/skills/image-to-code/scripts/install.py --agent codex --scope project --project . --verify
python3 .claude/skills/image-to-code/scripts/install.py --agent claude --scope project --project . --verify
```

`Image-to-code` được cài project-local cho Codex trong `.agents/skills/` và Claude Code trong `.claude/skills/`, từ commit `9b6c8940d2844aff8288ed594abfc2b9680ee558` (MIT). UIFix `1.2.6` được checkout tại commit `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53` (MIT). Bootstrap kiểm tra pin, license và build script; `npm ci --ignore-scripts` tắt tất cả dependency lifecycle scripts. Source/build/dependencies được giữ trong `.runtime/`, không đưa vào PR. Mặc định Playwright lấy từ lockfile UIFix. Để dùng Playwright đã có trong repo ứng dụng, đặt `VISUAL_QA_PLAYWRIGHT_FROM` thành thư mục package chứa `playwright`. Cài browser theo đúng phiên bản Playwright đã chọn; `CHROME_PATH` có thể override executable.

Không có npm package UIFix công khai ở bản pin; CLI gọi trực tiếp `diffPipeline` trong source UIFix pinned để nhận pixel diff/SSIM và vùng khác biệt. Không gắn nhãn fallback/pixelmatch tự viết.

Ưu tiên Chromium đúng phiên bản Playwright của repo UI. Nếu Chromium trên host lỗi GPU, dùng container Playwright tạm thời đúng phiên bản đã pin trong `toolchain-lock.json`, nối loopback preview qua host network trên máy phát triển được ủy quyền, mount source/artifact cần thiết và không publish port. Image chính thức chạy với quyền root tắt Chromium sandbox trong container; chỉ dùng cho trang fixture/local preview, không production hoặc tab cá nhân. Không thêm `--no-sandbox` vào lệnh host.

## 2. Chuẩn bị ảnh và trang

1. Xuất frame đã được duyệt thành PNG. Ghi nguồn, screen/route, viewport và người duyệt cạnh ảnh.
2. Cấu hình một reference PNG riêng cho từng viewport trong `visual_qa.references` theo khóa `WxH`. Ảnh phải có đúng số pixel với `--viewport`; công cụ từ chối resize hoặc crop ngầm. Với một viewport có thể dùng khóa `visual_qa.reference` dạng chuỗi.
3. Chạy app fixture/local trên loopback. Không dùng endpoint production; CLI chỉ mở `http://localhost`, `127.0.0.1` hoặc `[::1]`, và chặn mọi request origin khác.
4. Chọn route cùng trạng thái với reference. Nếu khác màn hoặc nội dung không tương đương, không báo fidelity PASS; đánh dấu `REFERENCE_NOT_EQUIVALENT` và lấy reference/fixture đúng.

## 3. Chạy một viewport

```sh
tools/visual_qa/visual-qa \
  --reference docs/design/orders-1366x768.png \
  --url http://127.0.0.1:4173/app/store-minh-anh/orders \
  --viewport 1366x768 \
  --out .artifacts/visual-qa/orders-1366 \
  --threshold 0.08
```

Lặp lại với mỗi viewport được yêu cầu, ví dụ `430x932`, `768x1024`, `1366x768`, `1920x1080`. CLI chờ `networkidle`, `document.fonts.ready`, chụp DPR 1, không che dữ liệu động, ghi page/console error và kiểm tra document overflow.

Mặc định ảnh là đúng viewport. Với reference được duyệt dạng full-page, truyền thêm `--capture full-page`; chiều rộng vẫn phải khớp viewport và chiều cao phải khớp ảnh chụp thực tế. Không resize/crop để làm khớp.

Mỗi lần chụp hoàn chỉnh tạo:

- `reference.png`: bản sao chính xác của đầu vào;
- `actual.png`: ảnh chụp trang ở viewport yêu cầu;
- `diff.png`: heatmap UIFix;
- `overlay.png`: reference và actual chồng 50%;
- `metrics.json`: tool/source SHA, SHA ảnh, viewport/DPR, threshold, mismatched pixels, SSIM, vùng lệch, JS errors, request bị chặn, overflow và verdict;
- `report.md`: kết quả và lệnh replay.

Sai kích thước, ảnh thiếu/hỏng, server không lên, browser không chạy, JS/page error, external request hoặc overflow đều trả exit code khác 0. Diff vượt threshold cũng trả code 1. SSIM và tỷ lệ pixel chỉ là chỉ báo, không phải nghiệm thu một mình.

## 4. Bật gate theo Task trong ProjectFlow

Gộp cấu hình mẫu từ [`docs/visual-qa/PROJECT-yaml-example.yaml`](visual-qa/PROJECT-yaml-example.yaml) vào `PROJECT.yaml` của repo UI. Lệnh stage phải chạy CLI một lần cho từng viewport, reference tương ứng và thư mục artifact riêng. Không thêm `visual_qa` vào `ci.required`: marker Task sẽ thêm stage vào CI cho đúng Task đó. Task backend và firmware không bị áp dụng.

Trong prompt Task UI, thêm nguyên dòng:

```text
VISUAL_QA_REQUIRED: true
```

ProjectFlow sẽ chèn reference/route/viewports/lệnh vào prompt; `TestRunner` chạy `visual_qa` và ghi kết quả vào `test_runs` ở SHA hiện tại. `TaskDecisionService` chỉ coi automated tests PASS khi có dòng `visual_qa` PASS tại current HEAD. Integration cũng yêu cầu stage đó khi Task được đánh dấu. Không có bảng visual status riêng. Reference config thiếu/sai sẽ fail closed.

## 5. Repair loop có giới hạn

1. Đọc `report.md`, `metrics.json`, `diff.png`, `overlay.png` và ảnh actual.
2. Chọn tối đa **3 lượt sửa** cho một viewport/run. Chỉ sửa component/CSS liên quan đến mismatch đã quan sát.
3. Không đổi ảnh reference, threshold, viewport, route hoặc thêm mask để ép PASS. Không đổi business semantics để khớp ảnh.
4. Sau sửa, chạy lại cùng reference/route/viewport/threshold. Giữ cả report trước và sau cùng SHA code tương ứng; nếu code thay đổi, chạy test và tạo evidence mới ở SHA mới.
5. Sau pixel gate, kiểm tra thủ công typography/nội dung, keyboard/accessibility, responsive behavior, states và flow nghiệp vụ.

CLI không tự sửa source; CLI tạo bằng chứng và hướng dẫn, agent đọc báo cáo rồi quyết định patch. Đây là repair loop có human/code-agent edit, giới hạn 3 lượt trong prompt, không phải auto-fix có thể ghi source tùy ý.

## 6. Smoke và negative controls

```sh
node --test tests/test_visual_qa_cli.mjs
node tools/visual_qa/smoke.mjs
```

Smoke chạy page fixture local: baseline chính xác PASS (0%), cố ý đổi màu FAIL, chạy lại bản đã sửa PASS (0%) với cùng reference SHA; reference thiếu/hỏng và server không sẵn sàng phải trả lỗi. Nó không chứng minh reference tương đương với màn nghiệp vụ.

## 7. Dùng Image-to-code trong Claude/Codex

- Codex: gọi `$image-to-code`, yêu cầu đọc reference, chia lớp layout/components/typography, dùng token/component hiện tại, dựng đúng stack hiện hữu và kiểm tra lại bằng CLI.
- Claude Code: gọi `/image-to-code` hoặc yêu cầu đọc `.claude/skills/image-to-code/SKILL.md`.
- Không tạo app thứ hai hoặc đổi framework; không đưa secrets/reference chưa được phép ra ngoài.
- Không chạy yêu cầu cài đặt tùy chọn upstream; các dependency nâng cao không cần cho screenshot/diff.

## 8. Xóa/rollback

Tắt opt-in bằng cách bỏ marker khỏi Task prompt theo quy trình versioning Task, hoặc xóa riêng `visual_qa` block/command khỏi `PROJECT.yaml`. Xóa cache cục bộ bằng `rm -rf .runtime/uifix-v1.2.6` chỉ trong root ProjectFlow. Đảo code qua revert PR. Không xóa preview service/port mà task không sở hữu và không deploy production từ pilot.
