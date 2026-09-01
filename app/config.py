from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    db_path: Path
    test_timeout: int
    configured_worktree_root: Path | None = None
    agents: tuple[str, ...] = ("codex", "claude", "gemini", "aider", "other")
    default_base_branch: str = "main"
    configured_state_dir: Path | None = None
    max_running_sandboxes: int = 3
    sandbox_retention_hours: int = 24
    cleanup_poll_seconds: int = 60
    # B0.1: Design Principle #2 / ADR-004 -- "none" (default) is today's
    # exact, permanently-supported zero-AuthN behavior; "required" is
    # the new B0.1 login surface. Never a third value: an unrecognized
    # setting fails closed to "none" at load time below rather than
    # silently enabling auth. See docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md.
    auth_mode: str = "none"
    session_secret: str | None = None
    session_max_age_days: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "noreply@localhost"
    smtp_use_tls: bool = True
    # B0.7: app-wide Fernet key(s) for org_secrets envelope encryption --
    # comma-separated, newest first (MultiFernet); NEVER stored in the
    # database. Same "REFUSED, never guessed" discipline as
    # session_secret: required whenever AUTH_MODE=required, checked at
    # startup in app/main.py, not silently skipped.
    secret_encryption_keys: tuple[str, ...] = ()
    # B3.1 (docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md, ADR-001):
    # app-wide GitHub App credentials -- same "never in the database"
    # precedent as session_secret/secret_encryption_keys, since these
    # are ADR-001's own "app-wide, long-lived" credentials, never a
    # per-tenant secret. All optional: an unconfigured App means
    # GitHubMergeService falls back to B0.7's existing per-org PAT
    # consumer, never a hard startup failure (unlike session_secret,
    # which IS mandatory under AUTH_MODE=required -- an App is an
    # enhancement, not a prerequisite for AUTH_MODE=required itself).
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_webhook_secret: str | None = None
    # B6.1 (docs/B6_TRUSTED_PROXY_SUPPORT.md, closing ADR-003's own
    # flagged residual): empty by default -- the same "off unless
    # explicitly configured" precedent every other credential/trust
    # setting here uses. Non-empty enables uvicorn's own
    # ProxyHeadersMiddleware (a standard, already-transitive dependency,
    # not hand-rolled), which only honors X-Forwarded-For/-Proto when
    # the DIRECT connecting peer is itself in this list.
    trusted_proxy_ips: tuple[str, ...] = ()

    @property
    def worktree_root(self) -> Path:
        return (self.configured_worktree_root or (self.root / ".worktrees")).resolve()

    @property
    def state_dir(self) -> Path:
        return (self.configured_state_dir or Path.home() / ".local" / "state" / "projectflow-workspace-manager").resolve()


def load_settings() -> Settings:
    project_root = Path(__file__).resolve().parents[1]
    root = Path(os.getenv("WORKSPACE_MANAGER_ROOT", str(project_root.parent))).resolve()
    worktree_root = Path(os.getenv("WORKSPACE_MANAGER_WORKTREE_ROOT", str(root / ".worktrees"))).resolve()
    db_raw = Path(os.getenv("WORKSPACE_MANAGER_DB", "data/workspace-manager.db"))
    db_path = db_raw if db_raw.is_absolute() else project_root / db_raw
    state_raw = os.getenv("WORKSPACE_MANAGER_STATE_DIR")
    state_dir = Path(state_raw).resolve() if state_raw else None
    # B0.1: fail closed, never fail open -- anything other than the
    # literal string "required" stays "none" (today's exact default),
    # matching this file's own existing "REFUSED"-over-"guessed"
    # convention (see scripts/start.sh's identical host/port discipline).
    auth_mode = "required" if (os.getenv("WORKSPACE_MANAGER_AUTH_MODE", "none").strip().lower() == "required") else "none"
    return Settings(
        root, os.getenv("WORKSPACE_MANAGER_HOST", "127.0.0.1"), int(os.getenv("WORKSPACE_MANAGER_PORT", "8765")),
        db_path.resolve(), int(os.getenv("WORKSPACE_MANAGER_TEST_TIMEOUT", "1800")), worktree_root,
        configured_state_dir=state_dir,
        max_running_sandboxes=int(os.getenv("MAX_RUNNING_SANDBOXES", "3")),
        sandbox_retention_hours=int(os.getenv("SANDBOX_RETENTION_HOURS", "24")),
        cleanup_poll_seconds=int(os.getenv("WORKSPACE_MANAGER_CLEANUP_POLL_SECONDS", "60")),
        auth_mode=auth_mode,
        session_secret=os.getenv("WORKSPACE_MANAGER_SESSION_SECRET") or None,
        session_max_age_days=int(os.getenv("WORKSPACE_MANAGER_SESSION_MAX_AGE_DAYS", "30")),
        smtp_host=os.getenv("WORKSPACE_MANAGER_SMTP_HOST") or None,
        smtp_port=int(os.getenv("WORKSPACE_MANAGER_SMTP_PORT", "587")),
        smtp_user=os.getenv("WORKSPACE_MANAGER_SMTP_USER") or None,
        smtp_password=os.getenv("WORKSPACE_MANAGER_SMTP_PASSWORD") or None,
        smtp_from=os.getenv("WORKSPACE_MANAGER_SMTP_FROM", "noreply@localhost"),
        smtp_use_tls=(os.getenv("WORKSPACE_MANAGER_SMTP_TLS", "true").strip().lower() not in ("0", "false", "no")),
        secret_encryption_keys=tuple(
            k.strip() for k in os.getenv("WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS", "").split(",") if k.strip()),
        github_app_id=os.getenv("WORKSPACE_MANAGER_GITHUB_APP_ID") or None,
        # Real PEM content has embedded newlines -- a single-line env var
        # (e.g. a systemd EnvironmentFile without quoting, or a shell
        # export) commonly carries them as literal backslash-n; accepted
        # either way, matching how other tools in this ecosystem (e.g.
        # GOOGLE_APPLICATION_CREDENTIALS-adjacent conventions) handle it.
        github_app_private_key=(
            (os.getenv("WORKSPACE_MANAGER_GITHUB_APP_PRIVATE_KEY") or "").replace("\\n", "\n").strip() or None),
        github_webhook_secret=os.getenv("WORKSPACE_MANAGER_GITHUB_WEBHOOK_SECRET") or None,
        trusted_proxy_ips=tuple(
            k.strip() for k in os.getenv("WORKSPACE_MANAGER_TRUSTED_PROXY_IPS", "").split(",") if k.strip()),
    )
