"""Public-safe session/project labeling for the read-only ProjectFlow fleet view.

Never publish unchecked session names or raw task descriptions. Each text label
is a curated, code-owned description selected by recognizable project/task
patterns. A hint from a session name is NOT verified execution evidence.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# All published project titles are fixed, curated strings.
_PROJECTS = (
    ("Zalo Group Hub", ("zgh-", "zalo-", "zalo-hub-")),
    ("MESFlow", ("mesflow-",)),
    ("ESP32 Kiosk / OTA", ("esp-", "esp32-", "ota-", "m910-claude-esp-", "m910-codex-esp-")),
    ("ProjectFlow", ("projectflow-", "qg-", "qualitygate-", "m910-projectflow-", "woodpecker-")),
    ("Brand Agent", ("brandagent-", "m910-codex-brandagent-")),
    ("Screen Remote", ("screenremote-", "screen-remote-")),
    ("RemoteOps", ("remoteops-",)),
)

# Most-specific first. These are deliberately not raw free-form session titles.
_TASK_HINTS = (
    ("photo-capacity", "Sửa lỗi giới hạn và chuyển tiếp ảnh"),
    ("autoreply", "Kiểm tra chuyển tiếp và tự động trả lời Zalo"),
    ("wifi", "Ổn định kết nối Wi-Fi Galaxy S7"),
    ("ondevice", "Nghiệm thu ứng dụng trực tiếp trên S7"),
    ("woodpecker", "Tích hợp CI nội bộ thay GitHub Actions"),
    ("qualitygate", "Kiểm tra Quality Gate và điều kiện phát hành"),
    ("qg-", "Gỡ blocker Quality Gate"),
    ("observability", "Xây dựng/kiểm tra theo dõi agent"),
    ("regression", "Chạy kiểm thử hồi quy"),
    ("authcheck", "Xác minh đăng nhập CLI và quyền truy cập"),
    ("independent-smoke", "Kiểm thử độc lập bản triển khai"),
    ("ota", "Kiểm thử hoặc nghiệm thu OTA"),
    ("keypad", "Kiểm thử bàn phím ESP32"),
    ("calibration", "Hiệu chỉnh thiết bị ESP32"),
    ("mobile-search", "Hoàn thiện tìm kiếm YouTube trên điện thoại"),
    ("bridge-ingress", "Kiểm tra kết nối điều khiển ứng dụng"),
    ("hardware", "Nghiệm thu trên phần cứng"),
    ("finalfix", "Sửa lỗi cuối trước nghiệm thu"),
    ("release", "Rà soát điều kiện phát hành"),
    ("qa", "Kiểm thử/đối chiếu kết quả"),
    ("readonly", "Kiểm tra chế độ chỉ đọc"),
    ("status", "Kiểm tra trạng thái hệ thống"),
)

_SAFE_SESSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,89}$")
_SAFE_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_SAFE_BRANCH = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,69}$")
_KNOWN_STATES = frozenset(("IDLE", "WAITING_INPUT", "BLOCKED", "EXITED", "STALLED"))
_FRESH_SECONDS = 300


def _time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def classify_public_session(session_name, *, state=None, checkpoint_at=None,
                            last_activity_at=None, last_commit=None,
                            git_branch=None, now=None):
    """Emit only curated labels and allowlisted short git evidence.

    OPEN_UNVERIFIED intentionally does not mean RUNNING.
    RUNNING_VERIFIED requires an explicit current trusted state + recent
    checkpoint, and is not inferred from an ACTIVE row, heartbeat or timestamp.
    """
    now = now or datetime.now(timezone.utc)
    name = session_name if isinstance(session_name, str) and _SAFE_SESSION.fullmatch(session_name) else ""
    project = next((p for p, prefixes in _PROJECTS if any(name.startswith(x) for x in prefixes)), None)
    task = next((t for slug, t in _TASK_HINTS if slug in name), None) if project else None
    # Only known project prefixes permit a displayed session slug.
    shown_session = name if project else None
    clean_commit = last_commit[:12] if isinstance(last_commit, str) and _SAFE_COMMIT.fullmatch(last_commit) else None
    clean_branch = git_branch if isinstance(git_branch, str) and _SAFE_BRANCH.fullmatch(git_branch) else None
    checkpoint = _time(checkpoint_at)
    activity = _time(last_activity_at)
    recent_checkpoint = bool(checkpoint and 0 <= (now - checkpoint).total_seconds() <= _FRESH_SECONDS)
    actual_state = str(state or "").upper()
    if actual_state == "RUNNING_VERIFIED" and recent_checkpoint:
        status = "RUNNING_VERIFIED"
    elif actual_state in _KNOWN_STATES and recent_checkpoint:
        status = actual_state
    else:
        status = "OPEN_UNVERIFIED"
    return {
        "session": shown_session,
        "project": project or "Chưa phân loại",
        "task_hint": task or "Chưa có mô tả task được xác minh",
        "task_source": "session_name_hint" if task else "unavailable",
        "execution_state": status,
        "evidence": "verified_recent_checkpoint" if status != "OPEN_UNVERIFIED" else "registry_only",
        "commit_ref": clean_commit,
        "branch_ref": clean_branch,
        "last_activity_at": activity.isoformat(timespec="seconds") if activity else None,
        "last_checkpoint_at": checkpoint.isoformat(timespec="seconds") if checkpoint else None,
        "running_verified": True if status == "RUNNING_VERIFIED" else None,
    }
