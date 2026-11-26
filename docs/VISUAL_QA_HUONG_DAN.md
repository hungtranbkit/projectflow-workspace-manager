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

### Chạy stage trong sandbox khi AUTH_MODE=required

Khi `WORKSPACE_MANAGER_AUTH_MODE=required`, ProjectFlow dùng Docker sandbox thật cho các lệnh của TestRunner. Container chạy bằng UID/GID của chủ sở hữu worktree để ghi được vào thư mục evidence riêng có quyền `0700`; giới hạn network, capability, memory/CPU/PID và mount worktree vẫn được giữ. Vì sandbox dùng `--network none`, `localhost` bên trong lệnh không trỏ về server trên host. Với test fixture, khởi động server loopback trong chính command/container rồi dừng bằng `trap`; không mở cổng ra host và không đổi cấu hình sandbox để kết nối server ngoài.

Kiểm tra đường chạy bắt buộc trên máy đã có Docker và runtime UIFix pinned:

```sh
VISUAL_QA_MANDATORY_EVIDENCE_DIR=.runtime/visual-qa-mandatory \
python -m pytest -p no:cacheprovider -q tests/test_visual_qa_mandatory_sandbox.py
```

Test tạo project/ảnh synthetic tạm, gọi app `AUTH_MODE=required`, `TestRunner` và `TaskDecisionService` thật, rồi chạy CLI trong container digest đã pin; đây là kiểm tra sandbox contract, không phải NovaRetail pilot hay phê duyệt thiết kế. Không dùng host loopback server hoặc đổi sang direct-host để làm test xanh.

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
CHROME_PATH=/path/to/authorized-chromium-wrapper \
VISUAL_QA_PLAYWRIGHT_FROM="$PWD" node tools/visual_qa/smoke.mjs
```

Smoke chạy page fixture local: baseline chính xác PASS (0%), cố ý đổi màu FAIL, chạy lại bản đã sửa PASS (0%) với cùng reference SHA; reference thiếu/hỏng và server không sẵn sàng phải trả lỗi. Nó không chứng minh reference tương đương với màn nghiệp vụ.

## 7. Dùng Image-to-code trong Claude/Codex

- Codex: gọi `$image-to-code`, yêu cầu đọc reference, chia lớp layout/components/typography, dùng token/component hiện tại, dựng đúng stack hiện hữu và kiểm tra lại bằng CLI.
- Claude Code: gọi `/image-to-code` hoặc yêu cầu đọc `.claude/skills/image-to-code/SKILL.md`.
- Không tạo app thứ hai hoặc đổi framework; không đưa secrets/reference chưa được phép ra ngoài.
- Không chạy yêu cầu cài đặt tùy chọn upstream; các dependency nâng cao không cần cho screenshot/diff.

### Phân biệt stage runner và runtime provider

Trong PR #74, `tests/test_visual_qa_execution.py` kiểm tra `TestRunner.start()` và `TaskDecisionService`. `tests/test_visual_qa_runner_subprocess.py` bổ sung kiểm tra tích hợp gọi command runner subprocess thật, SQLite tạm, git fixture và HTTP server loopback; bằng chứng có positive, negative, stale SHA và fail-closed.

```sh
.venv/bin/python -m pytest -p no:cacheprovider -q \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  tests/test_visual_qa_runner_subprocess.py
node --test tests/test_visual_qa_cli.mjs
node tools/visual_qa/smoke.mjs
```

Các kiểm tra này chứng minh marker UI thêm stage, stage fail làm gate fail, và task backend không có marker không bị thêm stage; chúng không chứng minh model/provider đã dựng hoặc sửa giao diện. Nếu Codex/Claude CLI chưa có phiên đăng nhập được ủy quyền, ghi runtime là **UNVERIFIED**, không tự đọc/in token, không gọi API bằng credential không rõ nguồn và không đưa reference riêng tư lên dịch vụ bên ngoài. Khi có runtime hợp lệ, dùng fixture local được phép, lưu prompt/lệnh đã khử bí mật, mã nguồn trước/sau và chạy lại cùng reference bằng UIFix. Không biến kết quả của fixture chưa được duyệt thành bằng chứng tương đương sản phẩm.

Benchmark issue #73 ở commit `b18c54dd12b781f22bfc89699b21a964e18106bb` xác nhận one-shot-ui là công cụ chẩn đoán: ở controlled payment displacement 24px, one-shot báo 5.476663% trong khi UIFix pinned báo 9.409958% và fail ngưỡng 8%. Vì vậy giữ UIFix làm gate; không dùng tỷ lệ của one-shot để quyết định pass.

## 8. Xóa/rollback

Tắt opt-in bằng cách bỏ marker khỏi Task prompt theo quy trình versioning Task, hoặc xóa riêng `visual_qa` block/command khỏi `PROJECT.yaml`. Xóa cache cục bộ bằng `rm -rf .runtime/uifix-v1.2.6` chỉ trong root ProjectFlow. Đảo code qua revert PR. Không xóa preview service/port mà task không sở hữu và không deploy production từ pilot.


### Docker sandbox tests trên host hết IPAM mặc định

Test fixture nginx khai báo bridge subnet riêng cho từng Compose sandbox. Mặc định test chọn pool `10.252.0.0/16`, kiểm tra không giao với route IPv4 và subnet Docker hiện có, rồi chia subnet `/28` riêng cho mỗi project qua file `.env` tạm. Điều này không đổi cấu hình daemon hay luồng Compose thực. Nếu pool mặc định xung đột với mạng của host, đặt `PROJECTFLOW_TEST_DOCKER_IPAM_POOL` thành một IPv4 private pool chưa dùng sau khi kiểm tra route/network inventory:

```sh
ip -j route show
docker network ls
docker network inspect $(docker network ls -q)
export PROJECTFLOW_TEST_DOCKER_IPAM_POOL=10.253.0.0/16
./scripts/test.sh
```

Fixture cũng kiểm tra overlap và dừng rõ ràng nếu pool cấu hình bị chiếm. Chọn pool chỉ cho test; không sửa daemon, restart Docker, prune network dùng chung, hoặc dùng một dải còn route/network overlap. Xem log và inventory của lần chạy thực tế trước khi chọn dải.

Exact-SHA rerun 24/11/2026 tại `cee006ca53eb36b7be29cf654fb3081844c4cce1`: `./scripts/test.sh` exit 0, 1,575 passed / 1 skipped trong 52:17; 14 node IDs từng lỗi do IPAM nay pass. Collect-only module totals là 1,575, còn runtime outcomes là 1,576; chênh một item chưa được giải thích. Log, preflight/build, targeted, smoke và inventory ở `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124-ipam-fix/`. Full-suite pass cục bộ không đồng nghĩa CI exact-SHA đã chạy; PR #74 workflow vẫn chờ maintainer cho phép bật lại.


## 9. Replay dispatch Builder → TestRunner thật

Bằng chứng local trong bundle ProjectFlow dispatch runtime cho thấy editor và visual gate là hai bước của cùng Task, qua các adapter hiện có:

1. Tạo Task UI trên worktree disposable; thêm đúng dòng `VISUAL_QA_REQUIRED: true`. Không đặt marker cho backend/firmware.
2. Chạy React UI preview trên loopback, thêm reference/route/viewport/threshold cố định vào `PROJECT.yaml`, kiểm tra SHA PNG và UIFix pin. Chỉ dùng fixture đã được phép; ghi rõ nếu proposal chưa được duyệt.
3. Dùng hành động Builder hiện có để khởi chạy provider. Đây là API session → `AgentSessionManager`; không dùng PlannerAgentInvoker để sửa code. Đảm bảo repo đã được xem xét/trust theo prompt Claude Code nếu CLI yêu cầu. Nếu slash skill không được nhận diện, yêu cầu agent mở trực tiếp file project-local `.claude/skills/image-to-code/SKILL.md` (hoặc `.agents/skills/image-to-code/SKILL.md`) và ghi nhận hash.
4. Sau khi agent sửa source trong đúng worktree, bấm **Run tests** của workspace. Endpoint `POST /api/workspaces/{id}/test` gọi TestRunner, chạy command từ `PROJECT.yaml`, ghi từng `test_runs` theo SHA. TestRunner chỉ đo/gate; không sửa source.
5. Xác minh cùng SHA trên stage row và `TaskDecisionService.builder_tests_status`: negative phải FAIL; sau edit phải PASS dưới 8%; commit/source đổi sau đó phải khiến evidence thành `NOT_RUN`. Task backend không có marker chỉ chạy stage thường.
6. Lưu reference/actual/diff/overlay/metrics, sanitized prompt/transcript, provider exit, stage logs và SHA. Không coi `TaskDecisionService.evaluate().test_readiness=NO` trong một Task còn ACTIVE là visual gate failure; visual-test verdict là trường builder-tests canonical, nhưng Task vẫn phải đạt các trạng thái nghiệp vụ còn lại.

Ví dụ replay bằng giao diện ProjectFlow: **Task UI → Builder → agent sửa source → Run tests → xem Test Runs và SHA hiện tại**. CLI chạy manual ngoài Builder chỉ kiểm tra tool/provider trực tiếp và không chứng minh ProjectFlow đã dispatch. Nếu chưa có login provider được ủy quyền, dừng ở **UNVERIFIED**; không tự lấy token, không giả kết quả và không retry quota-limited provider.

Lần chạy synthetic được ghi tại `docs/visual-qa/evidence/issue-73/projectflow-dispatch-runtime-20261124/`: regression UIFix 26.680849% FAIL tại SHA `8913bda32a647bfb399f13497e273cbab424307f`; một lần Claude Builder repair; stage cùng SHA sửa PASS 5.784358% tại `d308d481441eaea23e151e0126a92dfa4b596cd2`; stale SHA trả `NOT_RUN`; backend opt-out chỉ có stage `test`. Reference là POS proposal chưa duyệt — đây là tool/runtime acceptance, không phải product-design approval.

## 10. Xem Visual QA trên Task hiện có

Mở trang chi tiết của Task. Panel **Visual QA** đọc contract trong `PROJECT.yaml` của từng Builder workspace và run `visual_qa` đã ghi trong `test_runs`; panel không tạo dashboard, stage, status hay lệnh thực thi riêng.

- Nếu prompt Task không có `VISUAL_QA_REQUIRED: true`, panel ghi **Không bắt buộc**. Khi workspace có source sạch và contract hợp lệ, người dùng vẫn xem được reference đã commit trước khi chọn opt-in. Chỉ có block trong `PROJECT.yaml` không tự bật gate.
- Phần opt-in hiển thị prompt hiện tại và checkbox xác nhận. Lưu dùng chính endpoint lưu prompt hiện có; nội dung prompt được giữ nguyên, marker được thêm khi cần và version prompt chỉ tăng nếu nội dung thực sự đổi. Lưu không chạy Builder hay TestRunner; người dùng vẫn bấm hành động hiện có.
- Reference preview chỉ lấy đường dẫn đã xác định từ `PROJECT.yaml` trong workspace hiện tại, đòi hỏi HEAD sạch/khớp, ảnh PNG thường (không symlink) đúng kích thước viewport. Route read-only yêu cầu quyền xem Task, không nhận path/URL từ client và trả `no-store`. Thiếu/không hợp lệ/stale/dirty thì ảnh không được phục vụ. Approval không được suy ra từ việc ảnh hiển thị.
- Nếu đã opt-in, panel hiện route loopback, viewport/reference, SHA-256 reference, ngưỡng, SHA source đã đo và trạng thái run. Nguồn thiết kế không có trường approval thì UI ghi rõ **Chưa ghi nhận — không đồng nghĩa đã được duyệt**.
- Metrics chỉ xuất hiện sau khi verifier của ProjectFlow xác minh lại evidence theo đúng run, source SHA, reference, route, viewport và contract. Thiếu/sai evidence sẽ không hiện metric như một kết quả hợp lệ; run SHA cũ hiện **STALE**.
- Nếu CLI hoàn tất capture, chỉ lỗi pixel vượt threshold, và manifest/metrics/ảnh khớp đầy đủ (không JS error, request ngoài, overflow hay lỗi chạy), panel có thể hiển thị ảnh **Không đạt — dùng để sửa**. Stage vẫn là `FAIL`; ảnh này không được dùng để PASS gate. Timeout, browser/server lỗi, thiếu viewport/manifest, source bẩn/cũ hoặc evidence sai hash vẫn không có ảnh chẩn đoán.
- Ảnh được phục vụ qua route read-only có quyền xem `test_run`, giới hạn đúng task/workspace/run và bốn tên PNG cho phép. Metrics/report JSON/Markdown không được phục vụ trực tiếp. Sửa source sẽ làm run cũ thành **STALE**.
- Với run hiện tại PASS và source còn sạch, panel hiện thumbnail **Reference / Actual / Diff / Overlay** cho từng viewport. Ảnh tải từ route GET có authz theo test run; manifest và digest được kiểm tra lại trước khi trả byte. Run stale, source dirty, sai tenant, opt-out hoặc artifact bị sửa đều không được phục vụ.
- **Run Test** trong panel gọi đúng endpoint TestRunner hiện có cho workspace. **Xem log run này** dùng route log hiện có và vẫn qua read authorization. Chỉ bốn PNG cố định được phục vụ qua route GET có authz; report/Markdown không được mở, và panel không nhận upload/URL/command.

Opt-in từ panel Task: xem reference đã cấu hình, mở **Bật Visual QA bắt buộc sau khi đã xem reference**, xác nhận checkbox và lưu prompt hiện tại. ProjectFlow thêm marker nếu cần mà không xóa brief. Sau đó người dùng chủ động bấm **Run Test** trên Builder workspace. Cũng có thể mở **Task Prompt → Edit** và thêm đúng dòng sau:

```text
VISUAL_QA_REQUIRED: true
```

Kết quả nhìn thấy trên panel chỉ là phần hiển thị của decision/evidence hiện có; không thay thế review hành vi/accessibility, approval thiết kế, hoặc yêu cầu CI.
