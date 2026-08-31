from __future__ import annotations
import re

"""B0.7 -- Secrets boundary redaction layer (docs/B0_HOSTED_PLATFORM_
SECURITY_FOUNDATION.md). Agent transcripts/logs/error surfaces become
multi-tenant-visible the moment B0.2's organizations exist (a MEMBER of
one org viewing a Task's transcript must never incidentally see a
credential that leaked into it, whether that credential is a secret
this app itself stores or one the agent's own environment happened to
print) -- flagged `CAN_WAIT` in the original P0 audit, addressed here.

Two independent layers, both applied by `redact()`:
1. Every currently-active, decryptable value in `org_secrets` (passed
   in explicitly by the caller as `known_secrets` -- this module never
   queries the database or decrypts anything itself, keeping it a
   pure, trivially-testable text function) is replaced outright,
   wherever it appears, regardless of shape.
2. A fixed set of common credential-SHAPED patterns (GitHub tokens,
   AWS access keys, generic bearer/API-key-looking assignments,
   private-key PEM blocks) are redacted even when nothing was ever
   registered in this app's own secret store -- defense in depth for a
   credential an agent's own environment or a tenant's own code
   printed, never routed through SecretsService at all."""

_PATTERNS: list[re.Pattern] = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub PAT/OAuth/App/refresh token prefixes
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # common vendor secret-key shape (OpenAI-style, etc.)
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9/_+.\-]{12,}['\"]?"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]

_MASK = "***REDACTED***"


def redact(text: str, known_secrets: "list[str] | tuple[str, ...] | None" = None) -> str:
    """Pure function: returns a new string, never mutates/reads any
    external state. Safe to call on arbitrarily large transcript text --
    linear in len(text) * len(known_secrets), no backtracking-prone
    patterns."""
    if not text:
        return text
    out = text
    for secret in (known_secrets or ()):
        if secret and len(secret) >= 6:  # never redact on a trivially short/empty value (too many false positives)
            out = out.replace(secret, _MASK)
    for pattern in _PATTERNS:
        out = pattern.sub(_MASK, out)
    return out
