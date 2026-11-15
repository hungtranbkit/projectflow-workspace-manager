"""Operator-invoked live Harness smoke test against LOCAL Terminal MCP.

One isolated shell session, no existing sessions, repositories, services or
production data are modified. HARNESS_EXECUTOR=codex runs the genuine Codex CLI
in read-only mode inside that shell and independently validates its output file.
Default deadline: 10 minutes.
Run only on the Terminal MCP host with an authorised local MCP connection.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessStore, HarnessError


def unwrap(response):
    if getattr(response, "is_error", False):
        raise RuntimeError("Terminal MCP returned is_error")
    body = getattr(response, "structured_content", None)
    if isinstance(body, dict) and body.get("status"):
        return body
    for part in getattr(response, "content", []):
        txt = getattr(part, "text", None)
        if isinstance(txt, str):
            try:
                value = json.loads(txt)
            except ValueError:
                continue
            if isinstance(value, dict) and "status" in value:
                return value
    raise RuntimeError("Terminal MCP returned no verified structured status")


async def call(client, action, **params):
    result = unwrap(await client.call_tool("terminal_turn", {"action": action, **params}))
    if result.get("status") in {"FAILED", "ERROR", "DENIED"}:
        raise RuntimeError(f"{action}: {str(result.get('result',{}).get('error','unknown'))[:100]}")
    return result


async def exercise(client):
    now = datetime.now(timezone.utc)
    mode = os.environ.get("HARNESS_EXECUTOR", "shell").strip().lower()
    if mode not in {"shell", "codex"}:
        raise RuntimeError("HARNESS_EXECUTOR must be shell or codex")
    codex_cwd = Path(os.environ.get(
        "HARNESS_CODEX_WORKDIR",
        "/home/dell/workspace/harness-mvp-impl-20261115")).resolve()
    if mode == "codex" and (not codex_cwd.is_dir() or not (codex_cwd/".git").exists()):
        raise RuntimeError("Codex requires an existing isolated Git clone")
    nonce = secrets.token_hex(5)
    name = f"harness-smoke-{nonce}"
    marker = f"HARNESS_PROBE_{nonce}"
    artifact = Path("/tmp") / f"{name}.txt"
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state"))) / "projectflow-harness"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = HarnessStore(state / "harness-mvp.db")
    task = store.create(key=name, main_goal="MVP Harness: one task executes, verifies and cleans safely",
                        child_goal=f"Verify one isolated {mode} command {nonce}",
                        dod="Observed command output AND matching on-disk artifact, followed by verified session cleanup",
                        agent=mode, node="dell-linux", created_at=now, ttl=timedelta(minutes=10))
    output = {"harness_task_id": task.id, "child": name, "executor": mode, "stage": "CREATED"}
    try:
        created = await call(client, "create_session", target=name, agent_type="shell",
                             node="dell-linux", working_directory="/home/dell/workspace")
        cr = created.get("result", {})
        if created.get("status") != "OK" or cr.get("session") != name or cr.get("state") not in {"READY","IDLE"}:
            raise RuntimeError("session was not explicitly created and READY")
        if mode == "codex":
            prompt = (
                f"Read-only one-task harness smoke. Reply with EXACTLY {marker} "
                "as your complete answer. Do not run tools or modify any files."
            )
            command = ("codex exec --sandbox read-only --cd " + shlex.quote(str(codex_cwd))
                       + " --output-last-message " + shlex.quote(str(artifact))
                       + " " + shlex.quote(prompt))
        else:
            payload = f"from pathlib import Path; Path({str(artifact)!r}).write_text({marker!r}); print({marker!r})"
            command = "python3 -c " + shlex.quote(payload)
        sent = await call(client, "send_wait", target=name,
                          text=command, timeout=20, compact=True)
        # Continue the same server-side wait token, never resend the prompt.
        for _ in range(5):
            wait = sent.get("wait", {})
            if wait.get("status") == "MATCHED" and wait.get("state") == "IDLE":
                break
            if wait.get("status") != "PENDING" or not wait.get("resume_token"):
                raise RuntimeError("task completion was not confirmed")
            sent = await call(client, "resume",
                              resume_token=wait["resume_token"], timeout=20, compact=True)
        else:
            raise RuntimeError("bounded completion wait exceeded")
        receipt = sent.get("send", {})
        if receipt.get("status") != "SUBMIT_CONFIRMED":
            raise RuntimeError("no confirmed submission receipt")
        task = store.dispatch(task.id, external_task_id=receipt.get("submission_id") or receipt.get("correlation_id"), at=datetime.now(timezone.utc))
        output["stage"] = "DISPATCHED"
        observed = await call(client, "inspect", targets=[name], tail_lines=9, compact=True)
        targets = observed.get("result", {}).get("targets", [])
        if len(targets) != 1 or targets[0].get("session") != name:
            raise RuntimeError("no matching live session readback")
        status = targets[0]
        state_name = status.get("state","UNKNOWN")
        # Process_alive describes a still-running *task*, not the idle shell.
        task = store.observe(task.id, backend_state=state_name,
                             process_alive=False if state_name=="IDLE" else None,
                             evidence_ref=f"terminal_mcp:inspect:{name}",
                             at=datetime.now(timezone.utc))
        output["observed_state"] = task.state
        tail = status.get("tail","")
        readback = artifact.read_text() if artifact.is_file() else None
        has_proof = state_name == "IDLE" and marker in tail and readback is not None and readback.strip() == marker
        if not has_proof:
            raise RuntimeError("did not verify command output and artifact independently")
        sha = hashlib.sha256(readback.encode()).hexdigest()
        task = store.accept(task.id, at=datetime.now(timezone.utc),
                            artifact_ref=str(artifact),
                            test_evidence=f"command-tail:{marker}",
                            runtime_readback=f"sha256:{sha}",
                            verifier=lambda a,t,r: a==str(artifact) and marker in t
                                and r==f"sha256:{sha}" and artifact.read_text().strip()==marker)
        output["stage"] = "DONE_VERIFIED"
        output["artifact_sha256"] = sha
        # Re-check terminal status before official confirmed deletion.
        check = await call(client, "inspect", targets=[name], tail_lines=3, compact=True)
        checked = check.get("result",{}).get("targets",[])
        if len(checked)!=1 or checked[0].get("state")!="IDLE":
            raise RuntimeError("cleanup refused: session not confirmed idle")
        deleted = await call(client, "delete_session", target=name, args={"confirm": True})
        dr = deleted.get("result",{})
        proof = dr.get("receipt",{})
        # Server owns authorization and task/watch/lease checks.
        gone = dr.get("deleted") is True and proof.get("tmux_absent_verified") is True
        if gone:
            listing = await call(client, "list_sessions", compact=True)
            present = listing.get("result", {}).get("sessions", [])
            gone = not any(item.get("name") == name for item in present)
        task = store.cleanup(task.id, at=datetime.now(timezone.utc),
                             executor_stopped=state_name=="IDLE", no_active_refs=gone,
                             dirty_worktree=False, evidence_preserved=True,
                             deletion_verified=gone)
        output["cleanup"] = task.cleanup_state
        if gone:
            artifact.unlink(missing_ok=True)
            output["artifact_removed"] = not artifact.exists()
        else:
            output["cleanup_reason"] = str(dr.get("error","cleanup not confirmed"))[:120]
        output["event_count"] = len(store.events(task.id))
        return output
    except Exception as exc:
        output["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        output["stage"] = store.get(task.id).state
        output["cleanup"] = "PENDING_REVIEW"
        return output


async def main():
    endpoint = os.environ.get("HARNESS_MCP_URL", "http://127.0.0.1:8766/mcp")
    async with streamable_http_client(endpoint) as streams:
        if hasattr(streams, "read_stream"):
            reader, writer = streams.read_stream, streams.write_stream
        else:
            reader, writer = streams[0],streams[1]
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            result = await exercise(client)
            print("HARNESS_LIVE_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
            if result.get("stage")!="DONE_VERIFIED" or result.get("cleanup")!="CLEANED_VERIFIED":
                raise SystemExit(2)

if __name__=="__main__":
    asyncio.run(main())
