# Bootstrap quản trị có phạm vi — V2

Đây là công cụ **chủ máy chạy một lần trên Linux Debian/Ubuntu**, không phải quyền root cho agent. Agent chỉ chuẩn bị mã, audit và kiểm thử trong thư mục tạm. Khi NEC chưa có `sshd` và không có đường quản trị được duyệt, trạng thái vẫn là `OWNER_BOOTSTRAP_REQUIRED`; có gói SSH chưa chứng minh `SSH_VERIFIED`.

## Một lệnh kiểm tra, không sửa hệ thống

Từ worktree đã kiểm tra của PR #84:

```bash
python3 scripts/fleet-admin.py audit
```

`dry-run` in ra danh sách thao tác chính xác mà không ghi file hoặc cài gói. Công cụ chỉ dùng executable tuyệt đối, môi trường cố định, stdin đóng và timeout 300 giây. Nó không gọi `sudo`, không hỏi mật khẩu, không đổi namespace/kernel, không thêm nhóm Docker và không đổi SSH trust.

```bash
python3 scripts/fleet-admin.py dry-run
```

## Chủ máy NEC: hành động một lần

Nếu chưa có quyền sudo, người quản trị đăng nhập trực tiếp bằng đường đã được duyệt. Không thử mật khẩu và không cấp `NOPASSWD: ALL`. Lệnh tối thiểu đã được owner yêu cầu là:

```bash
sudo apt-get update && sudo apt-get install -y openssh-server && sudo systemctl enable --now ssh
```

Lựa chọn có checkpoint/backup/rollback dưới đây chạy đúng phạm vi đó, thêm `sshd -t`, giữ cấu hình cũ bằng `--force-confold`, và không restart dịch vụ đang chạy. Package manager có thể tự khởi động SSH khi cài đặt; đây là hành vi của gói hệ điều hành. Không sửa firewall, `authorized_keys`, root-login hoặc public ingress. Vì package manager có quyền root, chủ máy cần xem dry-run và dùng kho gói hệ điều hành đã được quản trị tin cậy.

Trên đúng máy đích, từ mã đã xác minh SHA của PR:

```bash
python3 scripts/fleet-admin.py artifact > "$HOME/projectflow-fleet-admin-v2.py"
sha256sum "$HOME/projectflow-fleet-admin-v2.py"
```

Chủ máy xem toàn bộ artifact và đối chiếu checksum với cùng `app/services/fleet_admin.py` trong commit đã duyệt. Không tải và chạy trực tiếp một URL thay đổi. Sau đó chủ máy dùng quyền quản trị hợp lệ để tạo bản root-owned:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 "$HOME/projectflow-fleet-admin-v2.py" /usr/local/libexec/projectflow-fleet-admin-v2
sudo /usr/bin/sha256sum /usr/local/libexec/projectflow-fleet-admin-v2
```

Chỉ tiếp tục khi checksum của bản root-owned đúng với mã đã xem. Tất cả thành phần đường dẫn phải root-owned, không group/world-writable và không symlink; artifact sẽ từ chối đường dẫn khác. Không chạy privileged artifact trực tiếp từ worktree hoặc home có thể sửa bởi user.

```bash
sudo /usr/bin/python3 -I /usr/local/libexec/projectflow-fleet-admin-v2 bootstrap --confirm-scope openssh-server
```

Checkpoint `/var/lib/projectflow/fleet-admin-v2/ssh.json` và backup cục bộ `ssh-before/` nằm dưới thư mục mode `0700`; file backup mode `0600`. Không đồng bộ hoặc gửi backup: nó có thể chứa host private keys. Trước mỗi lệnh thay đổi có checkpoint `pending_operation`; sau lệnh thành công có `completed`. Lặp lại thành công trả `UNCHANGED`. Nếu có lần chạy dở, công cụ chặn replay bằng `CHECKPOINT_EXISTS_REVIEW_REQUIRED`; người quản trị xem checkpoint trước khi xử lý, không đoán thành công.

**Sau bootstrap:** chuyển lại lane Terminal MCP #128 để chạy managed SSH status/setup/verify qua đúng registered route, pin host key, kiểm tra nonce/user/hostname/exit 0 và repeat setup unchanged. Công cụ này không thay thế host-key verification, không đổi Tailscale SSH và không retarget IP LAN đang collision.

## Phạm vi quản trị lặp lại: tùy chọn riêng

Ưu tiên `systemctl --user` đối với dịch vụ do user sở hữu. Công cụ **không cấp quyền restart**, cài gói, sửa file hoặc chạy shell cho agent. Nếu chủ máy duyệt chính xác hai thao tác chỉ đọc dưới đây, họ mới chạy install-scope. Không cần cài scope này để bootstrap SSH.

- `ssh-status` → `/usr/bin/systemctl is-active ssh.service`.
- `ssh-config-test` → `/usr/sbin/sshd -t`.

Từ shell user thực tế cần được cấp quyền (không phải root shell):

```bash
sudo /usr/bin/python3 -I /usr/local/libexec/projectflow-fleet-admin-v2 install-scope --confirm-scope read-only-ssh --account "$(id -un)" --operation ssh-status --operation ssh-config-test
```

Artifact kiểm tra account/UID, scope chính xác và mọi đường dẫn; tạo policy root-owned `/etc/projectflow/owned-admin.json`, wrapper root-owned `/usr/local/sbin/projectflow-owned-admin`, và sudoers `/etc/sudoers.d/projectflow-owned-admin`. `visudo -cf` kiểm tra candidate trước khi cài grant. Sudoers liệt kê từng lệnh với đối số cố định, không wildcard; wrapper kiểm tra UID thực từ sudo, version/scope policy, ownership/mode của policy/executable/parent, rồi chạy argv cố định với môi trường sạch. Thao tác lạ hoặc thêm đối số đều bị từ chối. Wrapper ghi operation/UID/exit vào syslog AUTHPRIV, không ghi output hoặc secrets.

```bash
sudo -n /usr/local/sbin/projectflow-owned-admin ssh-status
sudo -n /usr/local/sbin/projectflow-owned-admin ssh-config-test
```

Không coi file scope tồn tại là admin PASS: cần chạy thực tế đúng account sau khi owner cài. `ssh-status` trả exit 3 nếu dịch vụ inactive; không biến exit đó thành PASS.

## Rollback chỉ thay đổi do công cụ tạo

**Chạy từ console hoặc đường phục hồi độc lập đã xác minh.** Rollback SSH có thể dừng SSH vừa được bootstrap, làm mất đường SSH đang dùng. Công cụ kiểm tra hash cấu hình/keys và trạng thái dịch vụ hiện tại khớp checkpoint; nếu bị sửa sau đó thì từ chối rollback. Nó chỉ stop/disable khi trước bootstrap chưa active/enabled. Không gỡ gói hoặc xóa host keys vì có thể đã được công việc khác sử dụng; đây là rollback trạng thái dịch vụ, không phải hoàn nguyên package manager.

```bash
sudo /usr/bin/python3 -I /usr/local/libexec/projectflow-fleet-admin-v2 rollback --confirm-scope openssh-server
sudo /usr/bin/python3 -I /usr/local/libexec/projectflow-fleet-admin-v2 rollback-scope --confirm-scope read-only-ssh
```

Rollback scope xác minh hash cả ba file, thu hồi sudoers trước rồi mới xóa wrapper/policy do nó tạo. File có sẵn được giữ nguyên; file thay đổi sau cài đặt không bị xóa. Lặp lại rollback trả `UNCHANGED`. Backup và receipt được giữ lại. Nếu bootstrap hoặc rollback bị ngắt giữa chừng, owner phải kiểm tra checkpoint; không có auto-replay hoặc tự xóa checkpoint để vượt chặn.

## Phạm vi kiểm chứng

Tests dùng root filesystem tạm và runner tiêm vào, không dùng sudo hoặc chạy root. Bao phủ: dry-run không ghi, install/repeat/rollback, giữ cấu hình/dịch vụ có sẵn, drift, đường dẫn symlink/user-writable, UID sai, operation/argument injection, visudo lỗi, timeout/môi trường sạch, checkpoint khi cài gói thất bại. Linux thực tế cần owner bootstrap và lane #128 verify. macOS/Windows không dùng artifact Debian này; không có claim runtime PASS cho chúng.
