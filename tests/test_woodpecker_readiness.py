"""Unit checks for the fail-closed Woodpecker CI bootstrap gate."""
import json
from infra.woodpecker.readiness import assess, parse_env


def config():
    shared = "a" * 64
    return (
        {
            "WOODPECKER_HOST": "https://ci.example.net",
            "WOODPECKER_GITHUB_CLIENT": "abcdef1234567890",
            "WOODPECKER_GITHUB_SECRET": "b" * 40,
            "WOODPECKER_AGENT_SECRET": shared,
            "CI_GRPC_BIND_IP": "100.99.22.10",
            "WOODPECKER_OPEN": "false",
        },
        {
            "WOODPECKER_SERVER": "100.99.22.10:9000",
            "WOODPECKER_AGENT_SECRET": shared,
        },
    )


def test_absent_config_blocked():
    data = assess({}, {})
    assert data["status"] == "BLOCKED" and not data["local_configuration_ready"]


def test_placeholder_config_blocked(tmp_path):
    path = tmp_path / ".env"
    path.write_text("WOODPECKER_HOST=https://ci.example.com\nWOODPECKER_GITHUB_CLIENT=REPLACE_ME\n", encoding="utf8")
    out = assess(parse_env(path), {})
    assert out["status"] == "BLOCKED"
    assert out["checks"]["https_host_configured"] is False


def test_private_vpn_config_passes_but_never_claims_live():
    server, agent = config()
    result = assess(server, agent)
    assert result["local_configuration_ready"]
    assert result["status"] == "CONFIGURED_NOT_VERIFIED"
    assert not result["webhook_pipeline_verified"]
    assert not result["runner_registered_verified"]
    assert not result["live_oauth_verified"]


def test_mismatch_token_blocks_without_exposing_token():
    server, agent = config()
    secret = server["WOODPECKER_GITHUB_SECRET"]
    agent["WOODPECKER_AGENT_SECRET"] = "c" * 64
    result = assess(server, agent)
    assert result["status"] == "BLOCKED"
    assert not result["checks"]["shared_agent_secret_matches"]
    assert secret not in json.dumps(result)
    assert agent["WOODPECKER_AGENT_SECRET"] not in json.dumps(result)


def test_public_and_loopback_grpc_rejected():
    server, agent = config()
    for bad_ip in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "PRIVATE_VPN_SERVER_IP"):
        server["CI_GRPC_BIND_IP"] = bad_ip
        assert not assess(server, agent)["checks"]["private_grpc_bind_configured"]


def test_open_registration_rejected():
    server, agent = config()
    server["WOODPECKER_OPEN"] = "true"
    assert not assess(server, agent)["checks"]["registrations_closed"]
