"""Second privacy boundary for Dell's authenticated AI Usage Monitor summary.

This module never accepts account names, plans, cost, tokens, prompts, paths
or arbitrary raw strings from an untrusted node response. Only strict,
bounded, finite quota percentages + reset epochs can enter public SSE.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

PROVIDERS = ("codex", "claude", "gemini", "antigravity")
WINDOWS = ("five_hour", "weekly")
_MAX_AGE_SECONDS = 900


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _percent(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if not 0 <= value <= 100:
        return None
    return round(float(value), 1)


def _reset(value: Any) -> int | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if not 946684800 <= value <= 4102444800:
        return None
    return int(value)


def unavailable_usage() -> dict[str, Any]:
    return {"schema_version": 1, "source_node": "dell-linux", "status": "unavailable",
            "sampled_at": None,
            "providers": [{"provider": name, "state": "unavailable", "windows": {}}
                          for name in PROVIDERS]}


def public_usage_projection(raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Validation and re-projection: no raw response can bypass this allowlist."""
    default = unavailable_usage()
    if not isinstance(raw, dict) or raw.get("kind") != "terminal-mcp-public-ai-usage":
        return default
    if raw.get("schema_version") != 1 or raw.get("source") != "dell-local-ai-usage-monitor":
        return default
    if raw.get("source_status") != "ok":
        return default
    sampled = _iso(raw.get("sampled_at"))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if sampled is None:
        return default
    age = (now - sampled).total_seconds()
    if age < -60:
        return default  # Future timestamps are invalid, not merely old.
    if age > _MAX_AGE_SECONDS:
        default["status"] = "stale"
        default["sampled_at"] = sampled.isoformat(timespec="seconds")
        return default
    default["sampled_at"] = sampled.isoformat(timespec="seconds")
    items = raw.get("providers")
    if not isinstance(items, list) or len(items) > len(PROVIDERS):
        return default
    # Name and order must be canonical. Duplicate or unknown provider names fail.
    names = [item.get("provider") if isinstance(item, dict) else None for item in items]
    if len(names) != len(set(names)) or any(name not in PROVIDERS for name in names):
        return default
    by_name = {item["provider"]: item for item in items if isinstance(item, dict)}
    providers = []
    for name in PROVIDERS:
        entry = by_name.get(name, {})
        raw_state = entry.get("state")
        state = raw_state if raw_state in ("available", "unavailable", "not_observed") else "unavailable"
        safe_windows = {}
        windows = entry.get("windows")
        if state == "available" and isinstance(windows, dict):
            for window_name in WINDOWS:
                obj = windows.get(window_name)
                if not isinstance(obj, dict):
                    continue
                used, remaining = _percent(obj.get("used_percent")), _percent(obj.get("remaining_percent"))
                if used is None or remaining is None or abs(used + remaining - 100) > 0.25:
                    continue
                safe_windows[window_name] = {
                    "used_percent": used, "remaining_percent": remaining,
                    "reset_at": _reset(obj.get("reset_at")),
                }
        providers.append({"provider": name,
                          "state": state if (state != "available" or safe_windows) else "not_observed",
                          "windows": safe_windows})
    return {"schema_version": 1, "source_node": "dell-linux", "status": "ok",
            "sampled_at": default["sampled_at"], "providers": providers}
