# Quy tắc điều phối hướng tới nghiệm thu thật (GLOBAL RULE V1)
Ngày chuẩn hóa: 2026-11-15

## 1. Mục đích
Áp dụng cho các workflow/project được chủ sở hữu giao điều phối: MESFlow, Offline POS/NovaRetail, BrandAgent, Zalo Hub, Screen Remote, CDTM, EverEnglish, FB Radar, ProjectFlow và dự án phát sinh. **Mục tiêu của từng dự án vẫn là nguồn chân lý**; tài liệu này chỉ chuẩn hóa cách tiến đến mục tiêu, không thay thế các phê duyệt riêng. Không tự bật automation đã tạm dừng.

Công thức: **Mục tiêu nghiệm thu → gate chưa đạt đầu tiên → một hành động kỹ thuật hợp lệ → kiểm chứng độc lập → checkpoint → gate kế tiếp**.

## 2. Gate (thích nghi theo mục tiêu)
| Gate | Điều kiện cần đạt | Bằng chứng tối thiểu |
|---|---|---|
| G0 Baseline | Đúng repo/branch, task, chủ sở hữu, node, phiên, trạng thái hiện tại, giới hạn an toàn | Commit/PR/issue hiện tại, log thực tế, phiên đang chạy |
| G1 Core | Chức năng và dữ liệu cốt lõi tồn tại, xử lý đúng các nhánh lỗi và retry/UNKNOWN | Diff, commit, unit tests phù hợp |
| G2 Contract | Tích hợp liên dịch vụ/thiết bị hợp đồng đúng, kiểm thử cả sai quyền, replay, STOP | Integration/E2E fake & negative tests với kết quả exit cụ thể |
| G3 Artifact | Build/deploy/staging theo quy trình hiện có và không trùng, rollback rõ | Hash, chữ ký, version/BuildConfig, smoke test, health |
| G4 Runtime | Đúng môi trường/thiết bị, quan sát được tín hiệu thật, không phá dữ liệu đang dùng | Log runtime, trạng thái phiên bản và dịch vụ/thiết bị thực |
| G5 Business acceptance | **Luồng nghiệp vụ thật** đúng mục tiêu và độc lập readback thành công | ID/URL/receipt/đối soát hoặc trạng thái nghiệp vụ kiểm chứng được |

Chỉ đánh dấu PASS khi chứng cứ phù hợp. Nếu không có chứng cứ: UNKNOWN, không phải PASS. CI không chạy do billing không tính CI PASS; test giả lập không tính LIVE PASS.

## 3. Vòng lặp mỗi kỳ
1. **Đọc checkpoint trước** (mục tiêu, gate, blocker, last evidence) và cập nhật tình trạng mới từ nguồn gốc (repo, máy, logs, ứng dụng).
2. **Chọn gate thấp nhất chưa PASS** trên critical path và một nhiệm vụ nhỏ nhất có thể hoàn tất ngay trong quyền được cấp. Viết tiêu chí PASS trước khi giao agent.
3. **Thực hiện → test → sửa căn nguyên → test lại**; nếu PASS tiếp tục gate kế. Chặn ở một gate thì chạy phần độc lập khác có quyền, không báo blocker rồi đứng lại.
4. **Không lặp quá 2 kỳ** cùng blocker mà không đổi chiến lược hợp lệ: đọc lỗi thực, giảm phạm vi, tái sử dụng session, chuyển node đủ khả năng, thử bộ kiểm thử độc lập, hoặc điều phối đúng người chịu trách nhiệm.
5. **Đóng vòng** bằng checkpoint lưu trong issue/PR/PROJECT_CONTEXT: gate state, delta thực, commit, lệnh/kết quả test, SHA/hash, link bằng chứng, worktree/owner/task, blocker và một next action cụ thể.
6. Chỉ báo **ĐÃ LÀM / ĐANG LÀM / SẼ LÀM** khi xuất hiện bằng chứng thay đổi hoặc rủi ro quan trọng. Không phóng đại tiến độ và không lặp báo cáo vô ích.

## 4. Nguyên tắc điều phối agent
- Không tạo session/job/build trùng khi công việc cũ còn chạy; ưu tiên tái sử dụng phiên và giữ ranh giới worktree.
- `SUBMIT_CONFIRMED` chỉ là giao việc; `RUNNING` không chứng minh có kết quả; `dispatched=false`, `IDLE_GUARD_UNSUPPORTED`, timeout hoặc prompt chờ input thì **không nói đã giao thành công**.
- Một luồng sửa critical path mỗi repo/module, có thể một luồng thử nghiệm độc lập; không để quá nhiều agent sửa chung nguồn.
- Khi kiểm thử hoặc commit không đổi qua hai kỳ, xử lý ngay nguyên nhân hoặc đổi phương án được phép thay vì mở phiên mới vô hạn.
- Đối với tác vụ chỉ đọc hoặc monitoring, tiến độ có thể là xác minh thực tế/khôi phục quan sát và cảnh báo; **không tự vượt giới hạn để tạo mutation**.

## 5. Giới hạn bắt buộc
- Không đổi ưu tiên, phạm vi, quyền hoặc lịch automation ngoài yêu cầu chủ sở hữu. Không tự bật lại nhiệm vụ đã tạm dừng.
- Không vượt xác thực, không dùng đường vòng khi platform policy chặn, không tự thực hiện thao tác public, Meta, gửi tin, cài firmware/APK, thanh toán/chi ngân sách hoặc xử lý dữ liệu nhạy cảm khi thiếu phê duyệt cần thiết.
- Khi thiếu quyền: yêu cầu **đúng một hành động cụ thể** của chủ tài khoản, tiếp tục mọi phần độc lập hợp lệ.
- Tác vụ liên quan phần cứng/kênh production phải xác minh đúng serial, backup, khả năng rollback và giữ nguyên thiết bị ngoài phạm vi.

## 6. Nghiệm thu đặc thù (minh họa; kiểm lại tiêu chí dự án trước khi triển khai)
- **BrandAgent:** MCP → S7 BrandAgent → Facebook native → Page/post thật, trusted ID/URL và vai trò quản trị readback, STOP, không trùng; chỉ khi được phép.
- **Zalo Hub:** Nguồn → đích chuyển đủ text/image/video/file theo bài test được định nghĩa, có biên nhận từ tài khoản đích độc lập, chống mất/trùng, giữ metadata.
- **Screen Remote:** Trình duyệt/YouTube trên một tab, điều khiển từ di động, QR pairing/share session, fullscreen thực và kết nối bền sau homescreen theo bài nghiệm thu cụ thể.
- **CDTM:** Nguồn hàng thật có quyền, mới/đa dạng, còn bán, link affiliate hợp lệ, đối soát hàng trước khi public Facebook, không đăng link chết.
- **Offline POS:** Tạo đơn → lưu → giao → ký → ghi sổ/công nợ → reload; search/paging/backup có test dữ liệu lớn, và EXE cài chạy thật.
- **MESFlow:** Task → operator → QC → report trên runtime thực, kiosk/OTA đúng thiết bị và rollback an toàn nếu có yêu cầu.
- **EverEnglish/ProjectFlow/những dự án khác:** Suy ra G5 từ user story gốc, không lấy số test/build thay bằng chứng nghiệp vụ.

## 7. Quy tắc dừng
STOP hoặc đề xuất đóng theo dõi **chỉ khi G0–G5 có chứng cứ PASS phù hợp và không còn lỗi P0 trong mục tiêu nghiệm thu**. Nếu chưa đạt, giữ P0 mở, chỉ rõ gate thấp nhất chưa PASS và bước có quyền tiếp theo. Đóng bớt agent/session dư thừa sau khi xác nhận công việc và dữ liệu đã được lưu an toàn.
