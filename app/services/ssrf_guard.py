from __future__ import annotations
import ipaddress
import socket
from urllib.parse import urlparse

"""B1.2 (docs/B1_HOSTED_SERVICE_READ_ISOLATION.md): a tenant's own
PROJECT.yaml supplies `service.healthcheck.url`, and this app issues a
real `urllib.request.urlopen()` GET against it
(DeploymentService._check_health, SandboxRuntimeService.health_check) --
docs/PRODUCTIZATION_AUDIT.md P0.17 rates an unvalidated version of this
MUST_FIX_BEFORE_PUBLIC_BETA for a hosted service (a tenant can point
their own health check at the hosting network's internal admin panel,
another tenant's private service, or the cloud metadata endpoint).

Deliberately NOT wired in under AUTH_MODE=none: a self-hosted operator's
own real, already-audited DEV target genuinely IS 127.0.0.1/an internal
LAN address (app/services/deployment_service.py's own docstring target
audit) -- this guard only ever runs when the caller has already checked
AUTH_MODE=='required' (see both call sites), never on its own.

Resolves the hostname and checks the ACTUAL IP being connected to, not
just the literal string in the URL -- a hostname like
"metadata.internal.evil.example" that resolves to 169.254.169.254 is
just as real an SSRF vector as the raw IP would be, and checking only
the unresolved string would miss it entirely."""


class SSRFGuardError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or str(ip) == "169.254.169.254"  # cloud metadata (also link_local, listed for clarity)
    )


def check_url(url: str) -> None:
    """Raises SSRFGuardError if `url` is not safe to fetch from a hosted,
    multi-tenant process. Fail-closed: any resolution failure, missing
    host, or non-http(s) scheme is rejected too -- never treated as
    "couldn't check, so allow"."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SSRFGuardError("INVALID_URL", f"could not parse URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise SSRFGuardError("SCHEME_NOT_ALLOWED", f"scheme must be http(s), got: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SSRFGuardError("NO_HOST", "URL has no host")
    try:
        ip = ipaddress.ip_address(host)
        addrs = [ip]
    except ValueError:
        # A real hostname -- resolve it and check every address it
        # returns (A and AAAA both), not just the first one.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise SSRFGuardError("DNS_RESOLUTION_FAILED", f"could not resolve host {host!r}: {exc}") from exc
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addrs:
            raise SSRFGuardError("DNS_RESOLUTION_FAILED", f"host {host!r} resolved to no usable address")
    for a in addrs:
        if _is_blocked_ip(a):
            raise SSRFGuardError("TARGET_NOT_ALLOWED", f"{host!r} resolves to a blocked address ({a})")
