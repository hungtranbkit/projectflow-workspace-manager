"""Read-only status readback for a single Harness-owned session."""
import asyncio
import json
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main(name):
    if not name.startswith("harness-"):
        raise SystemExit("refused: non-Harness session")
    async with streamable_http_client("http://127.0.0.1:8766/mcp") as streams:
        reader, writer = (streams.read_stream,streams.write_stream) if hasattr(streams,"read_stream") else streams[:2]
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            r=await client.call_tool("terminal_status",{"session":name})
            if r.is_error:
                raise RuntimeError("provider returned error")
            for part in r.content:
                raw=getattr(part,"text",None)
                if not isinstance(raw,str):
                    continue
                try: obj=json.loads(raw)
                except ValueError: continue
                if isinstance(obj,dict):
                    # Do not print pane output or secrets.
                    print(json.dumps({"status":obj.get("status"),
                                      "state":obj.get("state") or obj.get("result",{}).get("state"),
                                      "reason":obj.get("reason") or obj.get("result",{}).get("reason"),
                                      "input_required":obj.get("input_required") or obj.get("result",{}).get("input_required")},ensure_ascii=False))
                    return
            raise RuntimeError("missing structured response")

if __name__=="__main__":
    if len(sys.argv)!=2: raise SystemExit("usage: harness_status_probe.py harness-...")
    asyncio.run(main(sys.argv[1]))
