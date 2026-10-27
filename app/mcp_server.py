"""MCP server exposing one tenant's synced work items to AI agents (Claude, Cursor, ...).

    SYNCBRIDGE_TENANT=acme DATABASE_URL=sqlite:////abs/path/syncbridge.db python -m app.mcp_server
"""
import os
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from app import queries
from app.db import Base, SessionLocal, engine

server = MCPServer(
    "syncbridge",
    instructions=(
        "Work items (issues, tickets, PRs) synced from this customer's tools. "
        "Use search_items to find items, get_item for details, sync_status to check data freshness."
    ),
)


def _tenant() -> str:
    tenant = os.getenv("SYNCBRIDGE_TENANT", "").strip()
    if not tenant:
        raise RuntimeError("SYNCBRIDGE_TENANT is not set; this server must be scoped to one tenant")
    return tenant


@server.tool()
def search_items(
    query: str = "",
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search work items across all connected tools, newest first.

    query matches title or labels (case-insensitive). status is the source status, e.g. "open",
    "closed", "in_progress". assignee is the source username. limit is capped at 200.
    """
    db = SessionLocal()
    try:
        return queries.search_items(db, _tenant(), query, status, assignee, None, limit)
    finally:
        db.close()


@server.tool()
def get_item(item_id: int) -> Dict[str, Any]:
    """Get one work item by its SyncBridge id (from search_items)."""
    db = SessionLocal()
    try:
        item = queries.get_item(db, _tenant(), item_id)
    finally:
        db.close()
    if item is None:
        raise ValueError("item {} not found".format(item_id))
    return item


@server.tool()
def sync_status() -> List[Dict[str, Any]]:
    """List connected tools with last successful sync time and any error, to judge data freshness."""
    db = SessionLocal()
    try:
        return queries.sync_status(db, _tenant())
    finally:
        db.close()


def main() -> None:
    Base.metadata.create_all(engine)
    server.run("stdio")


if __name__ == "__main__":
    main()
