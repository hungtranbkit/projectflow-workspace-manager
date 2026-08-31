from __future__ import annotations
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

"""B0.7 -- Secrets boundary (docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md). The general encrypted, org-scoped secret-storage
primitive -- reuses B0.2's `organizations` as the one tenant boundary
(never a parallel ownership model), the same `org_id` scoping every
other B0.3+ resolver already keys off.

Envelope encryption via `cryptography`'s Fernet (AES-128-CBC + HMAC,
authenticated -- a corrupted/tampered ciphertext raises, it is never
silently "decrypted" into garbage). The app-wide master key(s) live in
`Settings.secret_encryption_keys` (`WORKSPACE_MANAGER_SECRET_
ENCRYPTION_KEYS`, comma-separated, newest first) -- NEVER in this
database. `MultiFernet` gives real key rotation: the newest key
encrypts every new/rotated secret, but any still-configured older key
can still decrypt a secret written before rotation, until an operator
explicitly re-encrypts (rotate()) everything and drops the old key.

No plaintext ever touches `self.db` -- not in a column, not in a log
message, not in an exception message. `list_for_org()`/`get_meta()`
return metadata only (name/kind/timestamps); only `reveal()` and
`get_for_use()` ever decrypt, and both write an access-log row (action
only, never the value) before returning."""

ALLOWED_ACTIONS = ("CREATE", "REVEAL", "ROTATE", "REVOKE", "USE", "USE_FAILED")


class SecretsError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SecretsService:
    def __init__(self, db, master_keys: list[str]):
        self.db = db
        self._fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in master_keys]) \
            if master_keys else None

    def _require_fernet(self) -> MultiFernet:
        if self._fernet is None:
            # Fails closed: no master key configured means no secret can
            # ever be created, read, or used -- never a silent plaintext
            # fallback. Mirrors B0.1's own session_secret startup check.
            raise SecretsError("ENCRYPTION_NOT_CONFIGURED",
                                "No secret encryption key is configured on this instance.")
        return self._fernet

    def _log(self, secret_id: int, org_id: int, action: str, actor_user_id: int | None) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unknown secret access action: {action!r}")
        self.db.execute(
            "INSERT INTO secret_access_log(secret_id,org_id,actor_user_id,action) VALUES(?,?,?,?)",
            (secret_id, org_id, actor_user_id, action))

    # ---- lifecycle ---------------------------------------------------
    def create(self, org_id: int, name: str, plaintext: str, actor_user_id: int, kind: str = "GENERIC") -> int:
        name = (name or "").strip()
        if not name:
            raise SecretsError("NAME_REQUIRED", "Secret name is required.")
        if not plaintext:
            raise SecretsError("VALUE_REQUIRED", "Secret value is required.")
        if self.db.one("SELECT id FROM org_secrets WHERE org_id=? AND name=? AND revoked_at IS NULL", (org_id, name)):
            raise SecretsError("NAME_ALREADY_EXISTS", f"A secret named {name!r} already exists for this organization.")
        ciphertext = self._require_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
        secret_id = self.db.execute(
            "INSERT INTO org_secrets(org_id,name,kind,ciphertext,created_by_user_id) VALUES(?,?,?,?,?)",
            (org_id, name, kind, ciphertext, actor_user_id))
        self._log(secret_id, org_id, "CREATE", actor_user_id)
        self.db.event("organization", org_id, "SECRET_CREATED", name)
        return secret_id

    def rotate(self, org_id: int, name: str, new_plaintext: str, actor_user_id: int) -> None:
        row = self._active_row(org_id, name)
        if not new_plaintext:
            raise SecretsError("VALUE_REQUIRED", "Secret value is required.")
        ciphertext = self._require_fernet().encrypt(new_plaintext.encode("utf-8")).decode("ascii")
        self.db.execute(
            "UPDATE org_secrets SET ciphertext=?,updated_at=?,rotated_at=? WHERE id=?",
            (ciphertext, _now(), _now(), row["id"]))
        self._log(row["id"], org_id, "ROTATE", actor_user_id)
        self.db.event("organization", org_id, "SECRET_ROTATED", name)

    def revoke(self, org_id: int, name: str, actor_user_id: int) -> None:
        row = self._active_row(org_id, name)
        self.db.execute("UPDATE org_secrets SET revoked_at=?,updated_at=? WHERE id=?", (_now(), _now(), row["id"]))
        self._log(row["id"], org_id, "REVOKE", actor_user_id)
        self.db.event("organization", org_id, "SECRET_REVOKED", name)

    def re_encrypt_all(self, key_index_from: int = 1) -> int:
        """Operator-run key-rotation completion step: re-encrypt every
        still-active secret under the CURRENT newest key (index 0),
        decrypting with whichever configured key actually validates
        (MultiFernet tries each in order) -- after this runs, an
        operator can safely drop the older key(s) from configuration.
        Returns the count re-encrypted. Never touches revoked secrets
        (nothing to protect going forward)."""
        fernet = self._require_fernet()
        rows = self.db.all("SELECT id, ciphertext FROM org_secrets WHERE revoked_at IS NULL")
        count = 0
        for row in rows:
            plaintext = fernet.decrypt(row["ciphertext"].encode("ascii"))
            new_ciphertext = fernet.encrypt(plaintext).decode("ascii")
            self.db.execute("UPDATE org_secrets SET ciphertext=?,updated_at=? WHERE id=?",
                             (new_ciphertext, _now(), row["id"]))
            count += 1
        return count

    # ---- read paths ----------------------------------------------------
    def _active_row(self, org_id: int, name: str) -> dict:
        row = self.db.one(
            "SELECT * FROM org_secrets WHERE org_id=? AND name=? AND revoked_at IS NULL", (org_id, name))
        if not row:
            raise SecretsError("SECRET_NOT_FOUND", f"No active secret named {name!r} for this organization.")
        return row

    def list_for_org(self, org_id: int) -> list[dict]:
        """Metadata only -- id/name/kind/timestamps. Never ciphertext,
        never plaintext. Safe to render directly in a template/API
        response with no further filtering by the caller."""
        rows = self.db.all(
            "SELECT id,name,kind,created_at,updated_at,rotated_at,last_accessed_at "
            "FROM org_secrets WHERE org_id=? AND revoked_at IS NULL ORDER BY name", (org_id,))
        return [dict(r) for r in rows]

    def reveal(self, org_id: int, name: str, actor_user_id: int) -> str:
        """The one explicit, logged, human-initiated plaintext-reveal
        path (e.g. an admin re-copying a value into a third-party tool)
        -- distinct from get_for_use()'s own silent internal-consumer
        path, so a human reveal is always independently auditable."""
        row = self._active_row(org_id, name)
        try:
            plaintext = self._require_fernet().decrypt(row["ciphertext"].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretsError("DECRYPTION_FAILED", "Secret could not be decrypted with any configured key.") from exc
        self.db.execute("UPDATE org_secrets SET last_accessed_at=? WHERE id=?", (_now(), row["id"]))
        self._log(row["id"], org_id, "REVEAL", actor_user_id)
        return plaintext

    def get_for_use(self, org_id: int, name: str) -> str | None:
        """Internal-consumer path (e.g. GitHubMergeService resolving a
        stored token for one subprocess call) -- returns None (never
        raises) for "not found"/"revoked", so a caller can fall back to
        its own next option (e.g. AUTH_MODE=none's host `gh` CLI
        delegation) rather than crashing. Logs USE on success,
        USE_FAILED on a decryption failure (corrupted ciphertext / all
        configured keys rotated past what encrypted it) -- never raises
        SecretsError itself, so a transient key-rotation gap degrades to
        "secret unavailable," not an unhandled 500."""
        row = self.db.one(
            "SELECT * FROM org_secrets WHERE org_id=? AND name=? AND revoked_at IS NULL", (org_id, name))
        if not row:
            return None
        try:
            plaintext = self._require_fernet().decrypt(row["ciphertext"].encode("ascii")).decode("utf-8")
        except (InvalidToken, SecretsError):
            self._log(row["id"], org_id, "USE_FAILED", None)
            return None
        self.db.execute("UPDATE org_secrets SET last_accessed_at=? WHERE id=?", (_now(), row["id"]))
        self._log(row["id"], org_id, "USE", None)
        return plaintext
