"""Second-scope AI Usage privacy boundary and evidence freshness tests."""
from datetime import datetime, timedelta, timezone
import json

from public_usage import public_usage_projection, unavailable_usage


NOW = datetime(2026,11,22,3,30,tzinfo=timezone.utc)


def raw_usage():
    return {
        "schema_version":1, "kind":"terminal-mcp-public-ai-usage",
        "source":"dell-local-ai-usage-monitor","source_status":"ok",
        "sampled_at":NOW.isoformat(),
        "providers":[
            {"provider":"codex","state":"available","windows":{
                "weekly":{"used_percent":13.0,"remaining_percent":87.0,"reset_at":1795751440}
            },"account":"someone@private.invalid", "plan":"Pro"},
            {"provider":"claude","state":"available","windows":{
                "five_hour":{"used_percent":6.0,"remaining_percent":94.0,"reset_at":None},
                "weekly":{"used_percent":1.0,"remaining_percent":99.0,"reset_at":None}
            },"api_key":"sk-secret"},
            {"provider":"gemini","state":"not_observed","windows":{}},
            {"provider":"antigravity","state":"not_observed","windows":{}},
        ],
        "prompt":"secret private prompt",
    }


def test_accepts_real_provider_windows_and_drops_every_identifier():
    d=public_usage_projection(raw_usage(),now=NOW)
    assert d["status"]=="ok" and d["source_node"]=="dell-linux"
    assert d["providers"][0]["windows"]["weekly"]["used_percent"]==13.0
    assert d["providers"][1]["windows"]["five_hour"]["used_percent"]==6.0
    assert d["providers"][2]["state"]=="not_observed"
    safe=json.dumps(d)
    for secret in ("someone@","account","Pro","api_key","sk-secret","secret private prompt"):
        assert secret not in safe


def test_bad_percentages_and_source_input_never_become_valid_quota():
    raw=raw_usage()
    raw["providers"][0]["windows"]["weekly"]["used_percent"]=float("nan")
    d=public_usage_projection(raw,now=NOW)
    assert d["providers"][0]["state"]=="not_observed"
    assert d["providers"][0]["windows"]=={}
    raw["providers"][0]["windows"]["weekly"]["used_percent"]=80.0
    assert public_usage_projection(raw,now=NOW)["providers"][0]["windows"]=={}
    raw["providers"][0]["windows"]["weekly"]["used_percent"]=13.0
    raw["providers"][0]["provider"]="attacker"
    assert public_usage_projection(raw,now=NOW)["status"]=="unavailable"
    assert public_usage_projection({"raw":"sk-secret"},now=NOW)==unavailable_usage()


def test_stale_or_unavailable_are_not_reported_as_zero_usage():
    raw=raw_usage()
    d=public_usage_projection(raw,now=NOW+timedelta(minutes=17))
    assert d["status"]=="stale"
    assert all(p["windows"]=={} for p in d["providers"])
    raw["source_status"]="unavailable"
    d=public_usage_projection(raw,now=NOW)
    assert d["status"]=="unavailable"
    assert d["providers"][0]["windows"]=={}
    raw["source_status"]="ok"
    raw["sampled_at"]=(NOW+timedelta(hours=2)).isoformat()
    assert public_usage_projection(raw,now=NOW)["status"]=="unavailable"
