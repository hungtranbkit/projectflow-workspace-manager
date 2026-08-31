from __future__ import annotations
import secrets

from fastapi import HTTPException, Request

"""ADR-003 (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md): in-house
double-submit-cookie CSRF, sized and shaped for the general B0.4 sweep
across every session-cookie-authenticated mutating route -- but B0.1
only applies it to the small number of NEW mutating routes it
introduces itself (logout, API-token create/revoke). The 143 pre-
existing routes are explicitly NOT swept here; that sweep is B0.3/
B0.4's own scope (see B0 Phasing), not silently pulled forward.

Bearer-token/webhook routes are never guarded by this (ADR-001/003's
own reasoning: a request carrying no ambient browser credential is
structurally immune to CSRF) -- `require_csrf` is only ever applied
alongside `current_user` (a session-cookie-backed identity), never
alongside API-token auth.

Token bound into `request.session` (the same signed, tamper-evident
cookie SessionMiddleware already provides for the session itself --
never a second, separately-signed value) -- comparing the session-held
value against the value the form/header actually sent is what proves
the request came from a page that could read that session's own
state, the standard double-submit property."""

_SESSION_KEY = "csrf_token"


def issue_csrf_token(request: Request) -> str:
    """Idempotent within one session: returns the existing token if one
    is already bound, only generates a fresh one the first time --
    otherwise every page render would silently invalidate every other
    open tab's already-embedded form token."""
    existing = request.session.get(_SESSION_KEY)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    request.session[_SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    """FastAPI dependency: verifies a `csrf_token` form field (or
    `X-CSRF-Token` header, for JS fetch()-based mutations -- see
    ADR-003's own migration-cost note about base.html's shared fetch()
    functions) against the session-bound value. 403, never a silent
    pass-through, on any mismatch -- including "no session-bound token
    at all" (a request arriving before issue_csrf_token() was ever
    called for this session is treated as unverifiable, not trusted).

    Real bug found and fixed during B0.2 implementation, retroactively
    also fixing B0.1's own routes that share this dependency: under
    `AUTH_MODE=none` SessionMiddleware is never installed at all (by
    design -- see ADR-004), so `request.session` doesn't exist yet as a
    scope key; touching it unconditionally raised an unhandled
    AssertionError (a real 500, not the clean 404 every B0.1/B0.2 GET
    route under `AUTH_MODE=none` already returns) for ANY POST route
    guarded by this dependency -- reachable by any stray/scanning
    request, not merely a theoretical gap. Checked first, before ever
    touching `request.session`."""
    if "session" not in request.scope:
        raise HTTPException(404)
    expected = request.session.get(_SESSION_KEY)
    if not expected:
        raise HTTPException(403, "CSRF_TOKEN_MISSING")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        form = await request.form()
        supplied = form.get("csrf_token")
    if not supplied or not secrets.compare_digest(str(supplied), expected):
        raise HTTPException(403, "CSRF_TOKEN_INVALID")


async def require_csrf_unless_bearer(request: Request) -> None:
    """B0.4's general-sweep counterpart to require_csrf -- the one this
    track's own `require_role()` guard (app/main.py) invokes on every
    one of the 143 pre-existing mutating routes, alongside B0.3's role
    check. Structurally skips the CSRF check for a Bearer-token/API
    request (this module's own docstring's standing reasoning: a
    request carrying no ambient browser credential -- no cookie the
    browser would auto-attach -- is structurally immune to CSRF; a
    script that can set its own Authorization header can just as
    easily not send a forged request in the first place). Every other
    request (cookie-session-backed, or entirely unauthenticated) goes
    through the real check -- an unauthenticated caller still needs a
    valid double-submit token bound to whatever anonymous session it
    already holds, closing the same "no ambient ability to read the
    token" gap require_csrf's own docstring describes, one level
    earlier than a logged-in user."""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return
    await require_csrf(request)
