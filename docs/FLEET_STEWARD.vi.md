# Quản gia ProjectFlow

Mở `/steward` trong ProjectFlow. Mỗi mục có node, nguồn và giờ quan sát. Bộ thu chạy theo chu kỳ 5 phút. Sau 15 phút dữ liệu được đánh dấu cũ. Chọn ngày theo giờ Việt Nam để xem ảnh chụp gần nhất lúc cuối ngày và những thay đổi đã quan sát. Ngày trước lần đo đầu tiên có thể trống. Hồ sơ merge và deploy đã có được đọc trực tiếp từ DB ProjectFlow. Câu hỏi về việc tiếp theo dùng bằng chứng còn thiếu và hành động đã ghi cho task. Phần trả lời hỗ trợ truy vấn theo dự án, node, ngày, phiên bản S7, kho ảnh, đường điều khiển và thay đổi.

## Máy chủ

Tại bản phát hành ProjectFlow đã kiểm thử, chạy dưới tài khoản dịch vụ.

```sh
umask 077
python3 -m scripts.fleet_agent init --db "$HOME/.local/state/projectflow-steward/memory.sqlite3"
```

Lưu `~/.config/projectflow-steward/nodes.json` (quyền 0600) dưới dạng JSON, khóa là ID từng node, giá trị là khóa **công khai** Ed25519 DER mã hóa base64 của đúng node đó, lấy từ kênh quản trị đã xác thực. Có thể thay vị trí bằng `PROJECTFLOW_STEWARD_KEYS` và `PROJECTFLOW_STEWARD_DB`. ProjectFlow có thể dùng `PROJECTFLOW_STEWARD_OPERATOR_IDS` khi đã bật xác thực tài khoản. Với chế độ local của ProjectFlow, quản trị viên phát một mã dùng một lần, hết hạn sau 15 phút. Nhập mã qua HTTPS ở trang đăng nhập quản gia. Phiên dùng cookie Secure/HttpOnly trong 21 ngày.

```sh
python3 -m scripts.fleet_agent invite --db "$HOME/.local/state/projectflow-steward/memory.sqlite3"
```

## Từng node

Cần Python 3.9 trở lên và OpenSSL hỗ trợ Ed25519 cùng pkeyutl -rawin. Bộ thu hoạt động bằng tài khoản thường. Git, Docker, systemd và ADB chỉ được đọc trong phạm vi tài khoản đó. ADB chỉ dùng cho pilot ở M910. Đường dẫn checkout do quản trị khai báo. Phạm vi Git gồm workspace đến ba cấp thư mục, dừng ở gốc từng checkout. Chỉ lấy tên dịch vụ đang chạy, trạng thái, thư mục, SHA hoặc image ID khi có. Tên dịch vụ ZGH/ProjectFlow theo quy ước là gợi ý liên kết; checkout và tên không chứng minh phiên bản ứng dụng đang phục vụ. Phép đo điện thoại chỉ xem model, package, phiên bản, trạng thái tiến trình và sự tồn tại của đường dẫn cố định. HTTP 200 từ M910 đến S7 chỉ chứng minh đường LAN đã thử.

```sh
umask 077
mkdir -p "$HOME/.config/projectflow-steward"
key="$HOME/.config/projectflow-steward/signing.pem"
test -s "$key" || (set -C; openssl genpkey -algorithm ED25519 > "$key")
openssl pkey -in "$key" -pubout -outform DER | base64 -w0
# Sau khi quản trị viên đăng ký khóa công khai, đặt node_id và ingest_url theo môi trường.
./scripts/install-fleet-steward.sh "$node_id" "$HOME/workspace" "$ingest_url"
systemctl --user status projectflow-steward-collect.timer --no-pager
```

Địa chỉ nhận có dạng `https://<ProjectFlow>/api/steward/captures`. Mỗi bản đo được ký và phải nhận đúng biên nhận SHA-256. Khi mất kết nối, file chưa có biên nhận nằm lại trong spool để thử lại. Bản đã nhận được dọn sau bảy ngày. Khi spool đạt 256 MiB node ưu tiên gửi tồn trước khi thu mới. Trên máy chủ, nội dung trùng được khử trùng và nén, thời điểm quan sát vẫn được giữ. Các thay đổi xảy ra giữa hai lần lấy mẫu có thể không được quan sát.

## Vận hành và hoàn tác

Dùng SQLite online backup cho DB quản gia và bảo quản riêng cấu hình khóa công khai. Khóa ký ở lại node. Có thể thu hồi một node bằng cách bỏ khóa công khai tương ứng. Để ngừng lấy mẫu trên một node, dùng `systemctl --user disable --now projectflow-steward-collect.timer` và dừng đúng oneshot nếu đang chạy. Khi hoàn tác web, trỏ dịch vụ về bản ProjectFlow trước và giữ lại DB cùng spool. Phần quản gia không chỉnh cấu hình, triển khai hay điều khiển Zalo Hub.

## Kiến trúc dự án (Archify · Floorplan)

Mở `/steward/architecture` sau khi đăng nhập quản gia. Mỗi dự án (ID `owner/repo`) có Tổng quan, Archify, Floorplan, Lịch sử và Bằng chứng. Một snapshot ghim đúng một commit nguồn cho cả hai công cụ, lưu HTML gốc của công cụ cùng manifest: phiên bản và commit công cụ, image runtime, SHA-256, kích thước, phủ quét, kết quả kiểm tra, giới hạn và nguồn gốc. Snapshot không bao giờ bị sửa; nhập lại cùng nội dung không tạo bản mới. Dữ liệu nằm trong DB quản gia (bảng `arch_*`), giữ 40 snapshot gần nhất mỗi dự án.

Độ mới so commit snapshot với nhánh theo dõi trong cấu hình. Cột "nguồn so với đang chạy" chỉ báo khớp khi một dịch vụ quan sát được báo đúng SHA; tên phiên bản APK không được suy ra commit.

HTML do công cụ sinh chỉ được phục vụ cho iframe cùng origin với CSP `sandbox allow-scripts` (origin mờ, không cookie/lưu trữ/API), `connect-src 'none'`, không tài nguyên ngoài; trang xem đặt `frame-src 'self'` để khung không điều hướng ra ngoài. Mở trực tiếp URL artifact trả 403, sai hash 409, sai dự án 404.

Cấu hình riêng `~/.config/projectflow-steward/architecture.json` (0600, có thể đổi bằng `PROJECTFLOW_ARCHITECTURE_CONFIG`): mỗi dự án khai báo kho cục bộ, ref theo dõi, tiền tố/hậu tố tệp được phép, mẫu loại trừ, IR Archify và bản đồ module Floorplan; công cụ ghim bằng commit đầy đủ và runtime ghim bằng image digest. Không có cấu hình thì không dự án nào chạy lại được.

```sh
python -m scripts.architecture_rerun import --project OWNER/REPO --commit SHA ...   # nhập đầu ra có sẵn của cả hai công cụ
python -m scripts.architecture_rerun enqueue --project OWNER/REPO [--commit SHA] [--archify-ir FILE]
python -m scripts.architecture_rerun status --project OWNER/REPO
```

Nút "Chạy lại" và `enqueue` đi qua `HarnessStore` (`architecture-runs.sqlite3` cạnh DB quản gia): một mục tiêu cho mỗi dự án + commit + bộ công cụ + đầu vào; lượt đã xong hoặc đang chạy không chạy lặp; lượt lỗi được thử lại tối đa 3 lần, mỗi lần là mục tiêu mới có ghi nhận; tối đa một lượt kiến trúc đồng thời; TTL 45 phút; cần còn ít nhất 5 GiB đĩa. Lượt chạy là unit user tạm thời có giới hạn thời gian/bộ nhớ. Nguồn là các blob Git của đúng commit, chỉ tệp thường trong danh sách cho phép, đặt chỉ đọc. Công cụ chạy trong container `--network none --read-only --cap-drop ALL` với giới hạn CPU/bộ nhớ/PID. IR Archify mang từ commit khác bị từ chối nếu dòng được trích dẫn đã đổi; khi đó tác tử phải soạn lại IR và gửi bằng `enqueue --archify-ir`. Browser-check nội bộ của Archify không chạy khi Chrome không có user namespace; hệ thống không tắt sandbox Chrome mà ghi `PARTIAL`.

Hoàn tác: trỏ dịch vụ về bản ProjectFlow trước; bảng `arch_*` và `architecture-runs.sqlite3` được bản cũ bỏ qua, không cần xoá.
