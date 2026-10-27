import json
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import queries
from app.connectors import PROVIDERS, build_connector
from app.connectors.base import Connector
from app.db import Base, engine, get_db
from app.models import Connection, DeadLetter
from app.sync import InvalidSignature, process_webhook, replay_dead_letter, run_sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="SyncBridge", version="0.1.0", lifespan=lifespan)


def get_tenant(x_tenant_id: str = Header(...)) -> str:
    if not x_tenant_id.strip():
        raise HTTPException(400, "X-Tenant-ID required")
    return x_tenant_id.strip()


def get_connector_builder() -> Callable[[Connection], Connector]:
    return build_connector


def _connection(db: Session, tenant: str, connection_id: int) -> Connection:
    conn = db.scalar(select(Connection).where(Connection.id == connection_id, Connection.tenant == tenant))
    if conn is None:
        raise HTTPException(404, "connection not found")  # also for other tenants' connections
    return conn


class ConnectionIn(BaseModel):
    provider: str
    name: str
    config: Dict[str, Any] = {}
    webhook_secret: Optional[str] = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/connections")
def create_connection(body: ConnectionIn, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)) -> dict:
    if body.provider not in PROVIDERS:
        raise HTTPException(422, "provider must be one of {}".format(PROVIDERS))
    conn = Connection(
        tenant=tenant,
        provider=body.provider,
        name=body.name,
        config_json=json.dumps(body.config),
        webhook_secret=body.webhook_secret or secrets.token_hex(24),
    )
    db.add(conn)
    db.commit()
    out = queries.connection_dict(conn)
    out["webhook_secret"] = conn.webhook_secret  # shown once, at creation
    return out


@app.get("/connections")
def list_connections(tenant: str = Depends(get_tenant), db: Session = Depends(get_db)) -> List[dict]:
    return queries.sync_status(db, tenant)


@app.post("/connections/{connection_id}/sync")
def sync_connection(
    connection_id: int,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
    builder: Callable[[Connection], Connector] = Depends(get_connector_builder),
) -> dict:
    conn = _connection(db, tenant, connection_id)
    return asdict(run_sync(db, conn, builder(conn)))


@app.get("/items")
def list_items(
    q: str = "",
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    connection_id: Optional[int] = None,
    limit: int = 50,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> List[dict]:
    return queries.search_items(db, tenant, q, status, assignee, connection_id, limit)


@app.get("/items/{item_id}")
def get_item(item_id: int, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)) -> dict:
    item = queries.get_item(db, tenant, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return item


@app.get("/dlq")
def list_dead_letters(tenant: str = Depends(get_tenant), db: Session = Depends(get_db)) -> List[dict]:
    rows = db.scalars(select(DeadLetter).where(DeadLetter.tenant == tenant).order_by(DeadLetter.id.desc()))
    return [{"id": d.id, "connection_id": d.connection_id, "kind": d.kind, "error": d.error,
             "created_at": d.created_at.isoformat() + "Z", "resolved": d.resolved_at is not None} for d in rows]


@app.post("/dlq/{dl_id}/replay")
def replay(
    dl_id: int,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
    builder: Callable[[Connection], Connector] = Depends(get_connector_builder),
) -> dict:
    dl = db.scalar(select(DeadLetter).where(DeadLetter.id == dl_id, DeadLetter.tenant == tenant))
    if dl is None:
        raise HTTPException(404, "dead letter not found")
    if dl.resolved_at is not None:
        return {"result": "already resolved"}
    conn = _connection(db, tenant, dl.connection_id)
    return {"result": replay_dead_letter(db, dl, conn, builder(conn))}


@app.post("/webhooks/{connection_id}")
async def receive_webhook(
    connection_id: int,
    request: Request,
    db: Session = Depends(get_db),
    builder: Callable[[Connection], Connector] = Depends(get_connector_builder),
):
    conn = db.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(404, "unknown connection")
    body = await request.body()
    try:
        result = process_webhook(db, conn, builder(conn), request.headers, body, now=time.time())
    except InvalidSignature:
        raise HTTPException(401, "invalid signature")
    return JSONResponse(asdict(result), status_code=202 if result.dead_lettered else 200)
