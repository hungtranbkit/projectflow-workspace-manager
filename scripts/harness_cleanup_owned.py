"""Conservative cleanup for ONLY an isolated Harness-owned terminal session.

Requires an exact harness-* name on the local MCP host. Never auto-accepts
workspace trust or deletes an unrelated session. The MCP server performs its
own task/lease/grant checks and demands explicit confirmation.
"""
import asyncio
import json
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def call(c, action, **kw):
    result = await c.call_tool("terminal_turn", {"action": action, **kw})
    if getattr(result, "is_error", False):
        raise RuntimeError("tool error")
    for part in result.content:
        try:
            data = json.loads(part.text)
            if isinstance(data, dict) and "status" in data:
                return data
        except (ValueError, AttributeError):
            pass
    raise RuntimeError("unverifiable tool result")

async def main(name):
    if not name.startswith("harness-") or not name.replace("-","").isalnum():
        raise SystemExit("refused: non-harness session")
    async with streamable_http_client("http://127.0.0.1:8766/mcp") as streams:
        r,w = (streams.read_stream, streams.write_stream) if hasattr(streams, "read_stream") else streams[:2]
        async with ClientSession(r,w) as c:
            await c.initialize()
            s = await call(c,"inspect",targets=[name],tail_lines=4,compact=True)
            targets = s.get("result",{}).get("targets",[])
            if len(targets)!=1 or targets[0].get("session")!=name:
                raise RuntimeError("session ownership readback mismatch")
            state=targets[0].get("state")
            reason=str(targets[0].get("reason",""))
            # A newly-created Codex waiting at its unaccepted trust prompt
            # has not begun an agent task. Other input waits are protected.
            blocked_trust=(state=="WAITING_INPUT" and "trust prompt" in reason.lower())
            if state!="IDLE" and not blocked_trust:
                raise RuntimeError("refused: active/unknown session")
            d=await call(c,"delete_session",target=name,args={"confirm":True})
            result=d.get("result",{})
            gone=result.get("deleted") is True and result.get("receipt",{}).get("tmux_absent_verified") is True
            if gone:
                l=await call(c,"list_sessions",compact=True)
                gone=not any(x.get("name")==name for x in l.get("result",{}).get("sessions",[]))
            print("HARNESS_CLEANUP_RESULT="+json.dumps({
                "session":name,"prior_state":state,"deleted_verified":gone,
                "error":None if gone else str(result.get("error","not confirmed"))[:160]}))
            if not gone:
                raise SystemExit(2)

if __name__=="__main__":
    if len(sys.argv)!=2:
        raise SystemExit("usage: harness_cleanup_owned.py harness-<owned-name>")
    asyncio.run(main(sys.argv[1]))
