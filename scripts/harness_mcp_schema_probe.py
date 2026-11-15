"""Print only input field names of local Terminal MCP tools used by the Harness."""
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://127.0.0.1:8766/mcp") as streams:
        read, write = (streams.read_stream, streams.write_stream) if hasattr(streams,"read_stream") else streams[:2]
        async with ClientSession(read,write) as c:
            await c.initialize()
            for tool in (await c.list_tools()).tools:
                if tool.name in {"terminal_turn","terminal_delete_session","terminal_status","terminal_send_task","terminal_task_status","terminal_list_sessions","terminal_start_task","terminal_create_session"} or ("task_status" in tool.name):
                    s=tool.input_schema or {}
                    print(tool.name, "required=",s.get("required",[]),"fields=",list(s.get("properties",{})))

asyncio.run(main())
