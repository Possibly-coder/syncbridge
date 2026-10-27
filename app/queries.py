"""Read paths shared by the REST API and the MCP server. Every query is scoped to one tenant."""
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Connection, Item


def item_dict(item: Item, conn_name: str = "") -> Dict[str, Any]:
    return {
        "id": item.id,
        "connection_id": item.connection_id,
        "source": conn_name or item.provider,
        "provider": item.provider,
        "source_id": item.source_id,
        "kind": item.kind,
        "title": item.title,
        "status": item.status,
        "assignee": item.assignee,
        "priority": item.priority,
        "labels": [label for label in item.labels.split(",") if label],
        "url": item.url,
        "updated_at": item.source_updated_at.isoformat() + "Z",
    }


def search_items(
    db: Session,
    tenant: str,
    q: str = "",
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    connection_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    query = (
        select(Item, Connection.name)
        .join(Connection, Connection.id == Item.connection_id)
        .where(Item.tenant == tenant)
        .order_by(Item.source_updated_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    if q:
        like = "%{}%".format(q.lower())
        query = query.where(or_(Item.title.ilike(like), Item.labels.ilike(like)))
    if status:
        query = query.where(Item.status == status)
    if assignee:
        query = query.where(Item.assignee == assignee)
    if connection_id:
        query = query.where(Item.connection_id == connection_id)
    return [item_dict(item, name) for item, name in db.execute(query)]


def get_item(db: Session, tenant: str, item_id: int) -> Optional[Dict[str, Any]]:
    item = db.scalar(select(Item).where(Item.id == item_id, Item.tenant == tenant))
    return item_dict(item) if item else None


def connection_dict(conn: Connection) -> Dict[str, Any]:
    return {
        "id": conn.id,
        "provider": conn.provider,
        "name": conn.name,
        "status": conn.status,
        "last_success_at": conn.last_success_at.isoformat() + "Z" if conn.last_success_at else None,
        "last_error": conn.last_error,
        "webhook_url": "/webhooks/{}".format(conn.id),
    }


def sync_status(db: Session, tenant: str) -> List[Dict[str, Any]]:
    conns = db.scalars(select(Connection).where(Connection.tenant == tenant).order_by(Connection.id))
    return [connection_dict(c) for c in conns]
