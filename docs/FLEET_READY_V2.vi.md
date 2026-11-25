# Fleet-ready V2 — thao tác theo task, không tự đổi mặc định

Phạm vi owner: [#83](https://github.com/hungtranbkit/projectflow-workspace-manager/issues/83#issuecomment-7151985597). Mã tiếp nối PR #84; không khôi phục task cũ chỉ vì Git đã hoạt động. Các mức độc lập: LOCAL_READY (CLI và local build/test), DELIVERY_READY (identity + push/readback hoặc HP receipt), ADMIN_READY (thao tác admin thực sự chạy), SSH_VERIFIED (host pin + nonce/user/hostname/exit). Không suy ra mức này từ mức khác.

## Một lệnh audit theo nền tảng

Chạy từ checkout đúng commit đã duyệt, bằng user thường. Workspace phải là worktree task-owned cụ thể. Audit không cấp trust hoặc sửa credentials.

Linux/macOS:
```sh
python3 scripts/fleet-permissions.py audit --workspace /exact/owned/task-worktree
```
Windows PowerShell:
```powershell
.\scripts\fleet-permissions.ps1 -Action audit -Workspace C:\exact\owned\task-worktree
```

Windows/macOS offline: đây là gói và hướng dẫn chuẩn bị; runtime vẫn UNKNOWN/OFFLINE. Không dùng bootstrap Debian trên các nền tảng đó. Installer Terminal MCP hiện có nhận `--fleet-readiness-config readiness.json --fleet-workspace /exact/worktree`; Windows nhận `-FleetReadinessConfig readiness.json -FleetWorkspace C:\exact\worktree`. Các mode audit/apply/check/rollback thoát trước pairing, token hoặc service update. Xem tài liệu `docs/fleet-readiness-v1.md` trong [PR Terminal MCP #132](https://github.com/hungtranbkit/terminal-mcp/pull/132) để pin source SHA và task_id.

## Opt-in một task qua ProjectFlow đang chạy

API owner `POST /api/workspaces/{wid}/fleet` nhận duy nhất các trường dưới. Cwd/agent/task được lấy từ workspace đã có, không nhận command từ client.

```json
{"trust_owned_workspace":true,"repo":"hungtranbkit/projectflow-workspace-manager","scope":"local","runtime":"native","request_key":"83-owner-task-v1"}
```

Sau đó nút Start Builder / API sessions / autopilot hiện có đều đi qua cùng preflight và runner. API launch-agent cũ cũng chuyển vào tracked session khi workspace đã enroll. Workspace chưa enroll giữ hành vi cũ. GET cùng endpoint xem enrollment; DELETE rollback trust sau khi session đã dừng. Task mới cần owner rollback rồi enroll request_key mới. HEAD thay đổi, Resume hoặc prompt thay đổi không tự cấp phép chạy lại; prompt khác với cùng key bị chặn.

HP native Codex hiện ACL_DENIED do namespace; không sửa kernel. Owner đã cho phép runtime Docker cô lập: chọn `runtime:"docker"`, `image:"sha256:..."` là image ID đã có và kiểm chứng, scope local. Mount Codex ở `/opt/codex-bin`, giữ nguyên toolchain image. Không cài image/CLI tự động. Docker local đã chứng minh file/build/test, chưa có Git trong container; scope delivery+Docker và hp-delivery chưa có handoff được xác minh bị chặn trước launch. Delivery code riêng qua HP bên dưới.

Codex dùng exec/workspace-write/approval never, network workspace theo cấu hình được hỗ trợ; update-only check tắt qua config. Claude dùng print/dontAsk với tools đã khai báo hạn chế; **chưa chứng minh shell/build/test/Git/MCP connector của Claude**. Auth expired hoặc tool cần thiết chưa được cấp vẫn là blocker, không auto-trust dialog và không cấp connector rộng.

`SUBMITTED` là gửi tới runner. Wrapper output không biến session thành RUNNING. Durable receipt mới xác nhận dispatch; CLI exit 0 vẫn cần file/build/test/readback. Stop chỉ dọn process group/container do chính launch tạo. Request STARTING bị ngắt giữ EXECUTION_UNKNOWN, không tự chạy lại. Checkpoint/log private ở state/fleet-v1, log có redaction.

## Git identity và sửa helper theo repo

Mỗi node cần identity thật, không chỉ `gh auth status`. Trong đúng môi trường service/user:
```sh
gh api user --jq .login
gh api repos/hungtranbkit/projectflow-workspace-manager --jq .permissions.push
```

Dell hiện 401. M910 có gh user-local nhưng service PATH chưa thấy và chưa login; NEC cần owner login riêng. Chủ máy chạy trên **chính node đó**, không gửi token vào chat:
```sh
"$HOME/.local/bin/gh" auth login --hostname github.com --git-protocol https --web
"$HOME/.local/bin/gh" auth setup-git --hostname github.com
```
Dell có `/usr/bin/gh`: dùng `gh` thay đường user-local nếu chưa cài đường đó. Sau identity/push PASS:
```sh
python3 scripts/fleet-permissions.py repair-git --workspace /exact/owned/task-worktree
```

Helper dùng absolute gh path, chỉ repo config, backup private; repeat không đổi, rollback bằng `rollback-git` chỉ khi config còn đúng giá trị đã cài. Không sửa global auth hoặc copy HP auth. PATH chỉ thêm user bin nếu owner/mode an toàn; thư mục group-writable được giữ nguyên và không tự chmod. Existing node installer/service PATH adoption có trong MCP PR, chưa restart các service đang làm việc.

## HP delivery bằng code-only bundle

Source node vẫn build/test/commit local dù GitHub auth chưa sẵn sàng. Owner duyệt **đúng repo, branch, base SHA, một child commit SHA, danh sách đường dẫn**. GitWorkspaceService.delivery_export tạo bundle giới hạn một commit; không đưa home, .env, auth, key, symlink, submodule hoặc binary. Kiểm tra nội dung code vẫn cần thiết; regex secret không phải chứng minh mọi source đều không có secret.

Chuyển bundle qua registered SSH đã pin, hoặc route được owner duyệt. Trên HP tạo manifest và checks argv riêng mode 0600; bundle không tự cung cấp commands. CLI:
```sh
python3 scripts/fleet-delivery.py --repo /exact/HP/repo --state /private/delivery-state --manifest /private/approved-manifest.json --bundle /private/code.bundle --checks /private/approved-checks.json
```

Manifest schema 1: request_key, task_id, source_node, repo, origin_url, source_branch, base_sha, commit_sha, remote_ref, allowed_paths, bundle_sha256. remote_ref phải là refs/heads/ của cùng task branch agent/. Origin fetch và effective push URL phải đúng owner destination. HP import vào ref/worktree riêng, kiểm tra exact bytes/commit/paths, chạy owner argv, identity + repo push, normal push, SHA readback, normal PR và readback. Existing policy pre-push hook vẫn chạy; advertised remote tip phải absent. Không force hoặc ghi main.

Receipt có source/HP/remote SHA và PR; lease theo request lẫn repo/ref; retry cùng input chỉ reconcile. Unknown push/PR không được gửi lại mù. Giữ code/source worktree và receipt làm checkpoint; không xóa published ref/PR khi rollback. Ví dụ thực tế đã chứng minh: HP product API → PR #85; Dell source → HP → PR #87; actual MCP create → HP → PR #88. Dell-origin delivery PASS không có nghĩa Dell-local Git auth PASS.

## Admin / SSH: owner action còn thiếu

NEC: agent hoạt động, sshd chưa có, sudo -n thất bại, chưa có approved admin wrapper. [Bootstrap V2](FLEET_ADMIN_V2.vi.md) có audit/dry-run, artifact root-owned/checksum, scope chỉ đọc, backup/checkpoint/rollback. Lệnh owner tối thiểu tại NEC:
```sh
sudo apt-get update && sudo apt-get install -y openssh-server && sudo systemctl enable --now ssh
```

Sau đó lane #128 setup/verify qua registered route, pin/user/hostname/nonce/exit và repeat unchanged. Không coi cài package là SSH_VERIFIED. MacBook còn Tailscale SSH/route collision; cần owner chọn thay đổi setting và đường phục hồi độc lập với #128; không retarget IP LAN colliding Docker. Các node offline vẫn OFFLINE.

## Rollback và giới hạn phát hành

Trust: DELETE API hoặc `fleet-permissions.py rollback --workspace ...`. Helper: `rollback-git`. User gh package: `rollback-gh` khi vẫn đúng receipt. Installer readiness rollback không đụng pairing/auth. Admin rollback chỉ owner qua console/recovery độc lập; không xóa SSH keys/package đã dùng bởi tác vụ khác. Dirty WIP hoặc drift bị chặn, không reset/clean.

Không merge, production rollout hoặc fleet restart trong proof này. PR mở để review. PR131 controller-only handoff được phối hợp riêng; giữ tmux PIDs/SSH profiles. Local tests và disposable product API/MCP canaries không thay production database. Xem evidence V2 cho exact gates, SHA, logs và những acceptance chưa có runtime proof.
