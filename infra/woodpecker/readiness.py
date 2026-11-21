#!/usr/bin/env python3
"""Read-only local bootstrap gate for Woodpecker CI. No network or secret output.

Result only validates *configuration presence and consistency*; it never
certifies OAuth callback, reachable agent, or a successful real CI pipeline.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def real(value: str) -> bool:
    return bool(value) and not any(x in value.upper() for x in ("REPLACE", "EXAMPLE", "YOUR_", "PRIVATE_VPN", "CHANGEME"))


def private_bind(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        # IPv4 RFC1918 or Tailscale's 100.64.0.0/10, never loopback or wildcard.
        return addr.version == 4 and (
            addr.is_private and not addr.is_loopback and not addr.is_link_local
            or addr in ipaddress.ip_network("100.64.0.0/10")
        )
    except ValueError:
        return False


def assess(server: dict[str, str], agent: dict[str, str]) -> dict[str, object]:
    host = server.get("WOODPECKER_HOST", "")
    parsed = urlsplit(host)
    host_ok = (parsed.scheme == "https" and bool(parsed.hostname) and real(host)
               and not parsed.username and not parsed.password and not parsed.query
               and not parsed.fragment and parsed.path in ("", "/"))
    client = server.get("WOODPECKER_GITHUB_CLIENT", "")
    secret = server.get("WOODPECKER_GITHUB_SECRET", "")
    shared = server.get("WOODPECKER_AGENT_SECRET", "")
    server_addr = agent.get("WOODPECKER_SERVER", "")
    addr_host, sep, addr_port = server_addr.rpartition(":")
    checks = {
        "https_host_configured": bool(host_ok),
        "github_oauth_configured": real(client) and len(client) >= 10 and real(secret) and len(secret) >= 20,
        "server_agent_secret_configured": bool(re.fullmatch(r"[0-9a-fA-F]{64}", shared)),
        "registrations_closed": server.get("WOODPECKER_OPEN", "false").lower() == "false",
        "private_grpc_bind_configured": private_bind(server.get("CI_GRPC_BIND_IP", "")),
        "dedicated_agent_endpoint_configured": sep == ":" and addr_port == "9000" and private_bind(addr_host),
        "shared_agent_secret_matches": bool(shared) and agent.get("WOODPECKER_AGENT_SECRET", "") == shared,
    }
    configured = all(checks.values())
    return {
        "component": "woodpecker_ci",
        "checks": checks,
        "local_configuration_ready": configured,
        "live_oauth_verified": False,
        "runner_registered_verified": False,
        "webhook_pipeline_verified": False,
        "status": "CONFIGURED_NOT_VERIFIED" if configured else "BLOCKED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--server-env", type=Path, default=base / ".env")
    parser.add_argument("--agent-env", type=Path, default=base / ".env.agent")
    args = parser.parse_args()
    report = assess(parse_env(args.server_env), parse_env(args.agent_env))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["local_configuration_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
