from __future__ import annotations
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

"""B0.2 -- Organizations/Tenants (docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md, Design Principle #3's resolved design). `repositories.
organization_id` is the ONE scoping lever that transitively tenant-
scopes the entire existing E1-E13 schema (every other table already
FKs through `project_id` -> `repositories.id`) -- this module never
adds an `organization_id` column anywhere else, and never retrofits
the 143 pre-existing engineering-lifecycle routes with org-scoping;
that general per-route AuthZ sweep is explicitly B0.3's own scope (see
the B0.2 implementation report for the exact boundary this session
drew).

Coarse roles (OWNER/ADMIN/MEMBER/VIEWER) exist as real, enforced
values here -- every mutating method in this class checks the actor's
own role before acting (`_require_role`), a narrow, service-level
check scoped to only the org-management actions THIS module
introduces. This is deliberately NOT the general `require_role()`
FastAPI dependency B0.3 will build for the other 143 routes -- that
generalized mechanism stays B0.3's job; this module only proves the
role vocabulary and its data-layer enforcement work correctly for the
surface B0.2 itself owns.

Invitations mirror `AuthService`'s own login-token discipline exactly
(same file, same reasoning): SHA-256-hashed at rest, single-use,
short-TTL, the raw token returned/emailed exactly once and never
persisted. `AUTH_MODE=none` (default, unchanged) never calls any
method here at all -- every route that would is gated in app/main.py,
not inside this service, the same pattern B0.1 already established."""

INVITATION_TTL_DAYS = 7
ROLES = ("OWNER", "ADMIN", "MEMBER", "VIEWER")
_MANAGE_ROLES = ("OWNER", "ADMIN")  # may invite/remove members, link repositories
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class OrganizationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class OrganizationService:
    def __init__(self, db, auth_service, email_sender):
        self.db = db
        self.auth_service = auth_service
        self.email_sender = email_sender

    # ---- orgs --------------------------------------------------------
    def _unique_slug(self, name: str) -> str:
        base = _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "org"
        slug = base
        n = 1
        while self.db.one("SELECT id FROM organizations WHERE slug=?", (slug,)):
            n += 1
            slug = f"{base}-{n}"
        return slug

    def create_org(self, name: str, owner_user_id: int) -> dict:
        name = (name or "").strip()
        if not name:
            raise OrganizationError("ORG_NAME_REQUIRED", "Organization name is required.")
        slug = self._unique_slug(name)
        oid = self.db.execute(
            "INSERT INTO organizations(name,slug,created_by_user_id) VALUES(?,?,?)", (name, slug, owner_user_id))
        self.db.execute(
            "INSERT INTO organization_members(org_id,user_id,role) VALUES(?,?,'OWNER')", (oid, owner_user_id))
        self.db.event("organization", oid, "ORG_CREATED", name)
        self.db.event("organization", oid, "MEMBER_JOINED", f"user_id={owner_user_id} role=OWNER")
        return self.get_org(oid)

    def get_org(self, org_id: int) -> dict | None:
        return self.db.one("SELECT * FROM organizations WHERE id=?", (org_id,))

    def list_orgs_for_user(self, user_id: int) -> list[dict]:
        return self.db.all(
            "SELECT o.*, m.role FROM organizations o JOIN organization_members m ON m.org_id=o.id "
            "WHERE m.user_id=? ORDER BY o.id", (user_id,))

    # ---- membership / the core data-layer boundary --------------------
    def member_role(self, org_id: int, user_id: int) -> str | None:
        """The one primitive every cross-org isolation check in this
        module (and every route calling into it) is built on -- returns
        None for a non-member, never raises, so callers decide what "not
        a member" means for their own route (404 vs 403 vs redirect)."""
        row = self.db.one("SELECT role FROM organization_members WHERE org_id=? AND user_id=?", (org_id, user_id))
        return row["role"] if row else None

    def require_member(self, org_id: int, user_id: int) -> str:
        role = self.member_role(org_id, user_id)
        if not role:
            raise OrganizationError("NOT_A_MEMBER", "You are not a member of this organization.")
        return role

    def _require_manage_role(self, org_id: int, user_id: int) -> str:
        role = self.require_member(org_id, user_id)
        if role not in _MANAGE_ROLES:
            raise OrganizationError("INSUFFICIENT_ROLE", "Only an OWNER or ADMIN can do this.")
        return role

    def list_members(self, org_id: int) -> list[dict]:
        return self.db.all(
            "SELECT m.id AS membership_id, m.role, m.created_at AS joined_at, u.id AS user_id, u.email "
            "FROM organization_members m JOIN users u ON u.id=m.user_id WHERE m.org_id=? ORDER BY m.id", (org_id,))

    def _owner_count(self, org_id: int) -> int:
        return self.db.one(
            "SELECT COUNT(*) c FROM organization_members WHERE org_id=? AND role='OWNER'", (org_id,))["c"]

    def remove_member(self, org_id: int, target_user_id: int, actor_user_id: int) -> None:
        self._require_manage_role(org_id, actor_user_id)
        target_role = self.member_role(org_id, target_user_id)
        if not target_role:
            raise OrganizationError("NOT_A_MEMBER", "That user is not a member of this organization.")
        if target_role == "OWNER" and self._owner_count(org_id) <= 1:
            raise OrganizationError("LAST_OWNER", "An organization must always have at least one OWNER.")
        self.db.execute("DELETE FROM organization_members WHERE org_id=? AND user_id=?", (org_id, target_user_id))
        self.db.event("organization", org_id, "MEMBER_REMOVED", f"user_id={target_user_id} by={actor_user_id}")

    def change_member_role(self, org_id: int, target_user_id: int, new_role: str, actor_user_id: int) -> None:
        actor_role = self.require_member(org_id, actor_user_id)
        if actor_role != "OWNER":
            raise OrganizationError("INSUFFICIENT_ROLE", "Only an OWNER can change member roles.")
        new_role = (new_role or "").strip().upper()
        if new_role not in ROLES:
            raise OrganizationError("INVALID_ROLE", f"Role must be one of {ROLES}.")
        current_role = self.member_role(org_id, target_user_id)
        if not current_role:
            raise OrganizationError("NOT_A_MEMBER", "That user is not a member of this organization.")
        if current_role == "OWNER" and new_role != "OWNER" and self._owner_count(org_id) <= 1:
            raise OrganizationError("LAST_OWNER", "An organization must always have at least one OWNER.")
        self.db.execute(
            "UPDATE organization_members SET role=? WHERE org_id=? AND user_id=?", (new_role, org_id, target_user_id))
        self.db.event("organization", org_id, "MEMBER_ROLE_CHANGED",
                       f"user_id={target_user_id} -> {new_role} by={actor_user_id}")

    # ---- invitations (mirrors AuthService's own login-token discipline) -
    def invite_member(self, org_id: int, inviter_user_id: int, email: str, role: str) -> tuple[dict, str] | None:
        """Returns (invitation_row, raw_token), or None if the email is
        already a member (a deliberate no-op, not an error -- avoids
        leaking membership existence any more than necessary while still
        giving the inviter unambiguous UI feedback via the returned
        None)."""
        self._require_manage_role(org_id, inviter_user_id)
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise OrganizationError("EMAIL_INVALID", "Enter a valid email address.")
        role = (role or "").strip().upper()
        if role not in ROLES:
            raise OrganizationError("INVALID_ROLE", f"Role must be one of {ROLES}.")
        existing_user = self.auth_service.get_user_by_email(email)
        if existing_user and self.member_role(org_id, existing_user["id"]):
            return None
        raw_token = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(days=INVITATION_TTL_DAYS)
        org = self.get_org(org_id)
        iid = self.db.execute(
            "INSERT INTO organization_invitations(org_id,email,role,token_hash,invited_by_user_id,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (org_id, email, role, _hash_token(raw_token), inviter_user_id, _iso(expires_at)))
        self.db.event("organization", org_id, "MEMBER_INVITED", f"email={email} role={role}")
        self.email_sender.send(
            email, f"You've been invited to {org['name']} on ProjectFlow",
            f"Join {org['name']} as {role}:\n\n/orgs/invitations/{raw_token}\n\n"
            f"This invitation expires in {INVITATION_TTL_DAYS} days.")
        return self.db.one("SELECT * FROM organization_invitations WHERE id=?", (iid,)), raw_token

    def peek_invitation(self, raw_token: str) -> dict | None:
        """Read-only, never consumes -- same GET-peeks-POST-consumes
        discipline as AuthService.peek_login_token(), for the same
        reason (never auto-act on a raw GET request)."""
        if not raw_token:
            return None
        row = self.db.one("SELECT i.*, o.name AS org_name FROM organization_invitations i "
                           "JOIN organizations o ON o.id=i.org_id WHERE i.token_hash=?", (_hash_token(raw_token),))
        if not row or row["accepted_at"] or row["revoked_at"]:
            return None
        expires_at = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if _now() > expires_at:
            return None
        return row

    def accept_invitation(self, raw_token: str) -> tuple[dict, dict]:
        """Single-use: consumed in the same call that validates it.
        Finds-or-creates the invited user (B0.2's own explicit multi-
        user entry point -- B0.1 alone can never create a second user)
        and adds the membership idempotently (INSERT OR IGNORE: a user
        who is somehow already a member when they accept simply stays
        at their existing role rather than erroring)."""
        row = self.peek_invitation(raw_token)
        if not row:
            raise OrganizationError("INVITATION_INVALID", "This invitation is invalid, expired, or already used.")
        self.db.execute("UPDATE organization_invitations SET accepted_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        user = self.auth_service.get_or_create_user(row["email"])
        self.db.execute(
            "INSERT OR IGNORE INTO organization_members(org_id,user_id,role) VALUES(?,?,?)",
            (row["org_id"], user["id"], row["role"]))
        self.db.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
        self.db.event("organization", row["org_id"], "MEMBER_JOINED", f"user_id={user['id']} role={row['role']}")
        return user, self.get_org(row["org_id"])

    def revoke_invitation(self, org_id: int, invitation_id: int, actor_user_id: int) -> None:
        self._require_manage_role(org_id, actor_user_id)
        row = self.db.one("SELECT * FROM organization_invitations WHERE id=? AND org_id=?", (invitation_id, org_id))
        if not row:
            raise OrganizationError("INVITATION_NOT_FOUND", "Invitation not found.")
        self.db.execute("UPDATE organization_invitations SET revoked_at=CURRENT_TIMESTAMP WHERE id=?", (invitation_id,))
        self.db.event("organization", org_id, "INVITATION_REVOKED", f"email={row['email']} by={actor_user_id}")

    def list_pending_invitations(self, org_id: int) -> list[dict]:
        return self.db.all(
            "SELECT * FROM organization_invitations WHERE org_id=? AND accepted_at IS NULL AND revoked_at IS NULL "
            "ORDER BY id DESC", (org_id,))

    # ---- repositories (the one existing table B0.2 actually scopes) ---
    def link_repository(self, org_id: int, repo_id: int, actor_user_id: int) -> None:
        """Cross-org isolation, enforced here, not just hidden in the UI:
        a repository already linked to a DIFFERENT organization can never
        be silently reassigned -- the acting org must unlink it (from
        that other org, by an actor with rights THERE) first. Never a
        route-level assumption; this check lives in the one place both
        the HTML route and any future API caller both go through."""
        self._require_manage_role(org_id, actor_user_id)
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        if not repo:
            raise OrganizationError("REPOSITORY_NOT_FOUND", "Repository not found.")
        if repo["organization_id"] and repo["organization_id"] != org_id:
            raise OrganizationError("REPOSITORY_ALREADY_LINKED", "This repository already belongs to another organization.")
        self.db.execute("UPDATE repositories SET organization_id=? WHERE id=?", (org_id, repo_id))
        self.db.event("organization", org_id, "REPOSITORY_LINKED", repo["repo_name"])

    def unlink_repository(self, org_id: int, repo_id: int, actor_user_id: int) -> None:
        self._require_manage_role(org_id, actor_user_id)
        repo = self.db.one("SELECT * FROM repositories WHERE id=? AND organization_id=?", (repo_id, org_id))
        if not repo:
            raise OrganizationError("REPOSITORY_NOT_FOUND", "That repository is not linked to this organization.")
        self.db.execute("UPDATE repositories SET organization_id=NULL WHERE id=?", (repo_id,))
        self.db.event("organization", org_id, "REPOSITORY_UNLINKED", repo["repo_name"])

    def list_org_repositories(self, org_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM repositories WHERE organization_id=? ORDER BY id", (org_id,))

    def list_unlinked_repositories(self) -> list[dict]:
        """Repositories with no organization yet -- what an org's own
        'link a repository' picker offers (never repos already claimed
        by a DIFFERENT org, matching link_repository()'s own boundary)."""
        return self.db.all("SELECT * FROM repositories WHERE organization_id IS NULL ORDER BY id")

    # ---- B0.1 -> B0.2 bootstrap migration ------------------------------
    def migrate_existing_data(self) -> dict:
        """Idempotent, safe, backward-compatible bootstrap-to-org
        backfill (explicit requirement: never silently assign existing
        data to the wrong tenant).

        Only ever acts when ownership is UNAMBIGUOUS: exactly one user
        exists globally with no organization membership at all yet. That
        is the only shape B0.1 alone can ever produce (its own bootstrap
        creates exactly one user; B0.1 has no invite/second-user path at
        all), so it is the only case this method will ever actually see
        in practice -- but the check is written generally, not merely
        assumed, so it stays correct if that invariant ever changes:
          * 0 users -> nothing to migrate, no-op.
          * exactly 1 user, already in >=1 org -> already migrated
            (or created their own org manually since) -- no-op, safe to
            call on every startup.
          * exactly 1 user, in 0 orgs -> creates one personal
            organization for them, makes them OWNER, and links every
            repository that has no organization yet to it (safe: with
            exactly one user in the whole system, no other tenant could
            possibly claim those repositories instead).
          * 2+ users with 0 organizations at all -> genuinely ambiguous
            (should be unreachable via B0.1 alone) -- refuses to guess,
            leaves every repository's organization_id untouched, and
            returns a warning for the caller to log."""
        users = self.db.all("SELECT id FROM users")
        if len(users) == 0:
            return {"action": "NONE", "reason": "no users exist yet"}
        unassigned_users = [u for u in users if not self.list_orgs_for_user(u["id"])]
        if not unassigned_users:
            return {"action": "NONE", "reason": "every existing user already belongs to an organization"}
        if len(users) > 1:
            # Not reachable via B0.1 alone (see docstring) -- defensive,
            # never guessed.
            return {"action": "SKIPPED_AMBIGUOUS",
                    "reason": f"{len(users)} users exist with no organization; ownership is ambiguous, refusing to auto-assign"}
        user = users[0]
        user_row = self.auth_service.get_user(user["id"])
        org = self.create_org(f"{user_row['email']}'s organization", user["id"])
        unlinked = self.list_unlinked_repositories()
        for repo in unlinked:
            self.db.execute("UPDATE repositories SET organization_id=? WHERE id=?", (org["id"], repo["id"]))
        if unlinked:
            self.db.event("organization", org["id"], "REPOSITORIES_BACKFILLED",
                           f"{len(unlinked)} repositories linked during B0.1->B0.2 migration")
        return {"action": "MIGRATED", "org_id": org["id"], "user_id": user["id"], "repositories_linked": len(unlinked)}
