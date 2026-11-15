"""Read-only connectivity probe for the local authenticated Terminal MCP endpoint.

Run on a trusted operator-owned node. Does not dispatch tasks or inspect sessions.
"""
import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    url = os.environ.get("HARNESS_MCP_URL", "http://127.0.0.1:8766/mcp")
    async with streamable_http_client(url) as streams:
        if hasattr(streams, "read_stream"):
            read, write = streams.read_stream, streams.write_stream
        else:
            read, write = streams[0], streams[1]
        async with ClientSession(read, write) as client:
            await client.initialize()
            catalog = await client.list_tools()
            names = [t.name for t in catalog.tools]
            print("MCP_CONNECTED", True)
            print("TOOLS", len(names))
            print("HAS_TERMINAL_TURN", "terminal_turn" in names)
            print("HAS_DELETE_SESSION", "terminal_delete_session" in names)
            print("RELEVANT_TOOLS", [x for x in names if "terminal" in x and ("turn" in x or "delete_session" in x or "task_status" in x)][:10])

if __name__ == "__main__":
    asyncio.run(main())
