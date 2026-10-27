"""Spawn the MCP server over stdio and call its tools like an agent would.

    DATABASE_URL=sqlite:////abs/path/syncbridge.db SYNCBRIDGE_TENANT=acme python scripts/mcp_check.py "query"
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(query: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            status = await session.call_tool("sync_status", {})
            print("sync_status:", [block.text for block in status.content] or "no connections")
            found = await session.call_tool("search_items", {"query": query, "limit": 3})
            for block in found.content:
                print("search_items:", block.text.replace("\n", " ")[:160])
            if not found.content:
                print("search_items: no results for this tenant")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
