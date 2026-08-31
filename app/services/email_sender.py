from __future__ import annotations
import logging
import smtplib
from email.message import EmailMessage

"""B0.1: real SMTP delivery for magic-link login emails -- stdlib
`smtplib`/`email.message` only (ADR-002's own dependency-minimalism
rationale: zero new dependency for something the standard library
already does correctly). Never a mock: when SMTP is genuinely
configured (`WORKSPACE_MANAGER_SMTP_HOST` set), this makes a real SMTP
connection and sends a real message.

When SMTP is NOT configured (the common case for a self-hosted
operator who hasn't stood one up -- ADR-002's own named self-hosted-
usability cost), `send()` logs the link instead of raising or silently
discarding it, clearly labelled so it is never mistaken for a real
delivery. This is a deliberate, honest fallback (visible in the
process's own log, matching the exact self-hosted-bootstrap spirit
ADR-002 already established for the first-admin console token) -- not
a mock standing in for production behavior, since real SMTP delivery
is exactly what runs the moment an operator configures it.

`smtp_client_factory` is injectable (same DI pattern this codebase
already uses throughout -- GitHubMergeService.runner,
AgentSessionManager.which, TerminalLauncherService's popen/which) so
tests can substitute a fake SMTP client that never opens a real
network connection, while production uses the real smtplib.SMTP."""

logger = logging.getLogger("projectflow.email")


def _default_smtp_client(host: str, port: int, timeout: int = 10) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


class EmailSenderService:
    def __init__(self, *, host: str | None, port: int, user: str | None, password: str | None,
                 from_addr: str, use_tls: bool = True, smtp_client_factory=_default_smtp_client):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.use_tls = use_tls
        self.smtp_client_factory = smtp_client_factory

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def send(self, to_addr: str, subject: str, body_text: str) -> bool:
        """Returns True if a real send was attempted (regardless of SMTP-
        level success/failure -- a real SMTP error is never swallowed,
        it propagates), False if it fell back to the log-only path
        because no SMTP host is configured at all."""
        if not self.configured:
            logger.warning("SMTP_NOT_CONFIGURED: would send to %s: %s\n%s", to_addr, subject, body_text)
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = to_addr
        msg.set_content(body_text)
        with self.smtp_client_factory(self.host, self.port) as server:
            if self.use_tls:
                server.starttls()
            if self.user:
                server.login(self.user, self.password or "")
            server.send_message(msg)
        return True
