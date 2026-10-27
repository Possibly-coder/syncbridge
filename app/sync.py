import base64
import json
import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.base import Connector, NormalizedItem, Page, PermanentError, RetryableError, to_naive_utc
from app.models import Connection, DeadLetter, Item, WebhookEvent, utcnow


def upsert_item(db: Session, conn: Connection, item: NormalizedItem) -> bool:
    """Insert or update by (connection, source_id). Older versions never overwrite newer ones,
    so duplicates and out-of-order deliveries are harmless. Returns True if a write happened."""
    updated_at = to_naive_utc(item.updated_at)
    row = db.scalar(select(Item).where(Item.connection_id == conn.id, Item.source_id == item.source_id))
    if row is not None and row.source_updated_at >= updated_at:
        return False
    if row is None:
        row = Item(tenant=conn.tenant, connection_id=conn.id, provider=conn.provider, source_id=item.source_id)
        db.add(row)
    row.kind = item.kind
    row.title = item.title[:512]
    row.status = item.status
    row.assignee = item.assignee
    row.priority = item.priority
    row.url = item.url
    row.labels = ",".join(item.labels)[:512]
    row.source_updated_at = updated_at
    row.raw_json = json.dumps(item.raw, default=str)
    return True


@dataclass
class SyncResult:
    status: str
    pages: int = 0
    fetched: int = 0
    applied: int = 0
    error: str = ""


def _fetch_with_retry(
    connector: Connector, cursor: Dict, max_attempts: int, base_delay: float, sleep: Callable[[float], None]
) -> Page:
    last = ""
    for attempt in range(1, max_attempts + 1):
        try:
            return connector.fetch_page(cursor)
        except RetryableError as exc:
            last = str(exc)
            if attempt < max_attempts:
                sleep(exc.retry_after if exc.retry_after is not None else base_delay * 2 ** (attempt - 1))
    raise RetryableError("gave up after {} attempts: {}".format(max_attempts, last))


def run_sync(
    db: Session,
    conn: Connection,
    connector: Connector,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = 1000,
) -> SyncResult:
    result = SyncResult(status="syncing")
    cursor = json.loads(conn.cursor_json or "{}")
    conn.status = "syncing"
    db.commit()

    for _ in range(max_pages):
        try:
            page = _fetch_with_retry(connector, cursor, max_attempts, base_delay, sleep)
        except (RetryableError, PermanentError) as exc:
            # Cursor still points at the last stored page, so the next run resumes there.
            conn.status = "error"
            conn.last_error = str(exc)
            db.add(DeadLetter(tenant=conn.tenant, connection_id=conn.id, kind="sync",
                              payload=json.dumps({"cursor": cursor}), error=str(exc)))
            db.commit()
            result.status, result.error = "error", str(exc)
            return result

        for item in page.items:
            result.applied += upsert_item(db, conn, item)
        result.pages += 1
        result.fetched += len(page.items)
        cursor = page.cursor
        conn.cursor_json = json.dumps(cursor)
        db.commit()  # items and cursor commit together, page by page
        if page.done:
            break

    conn.status = "ok"
    conn.last_error = None
    conn.last_success_at = utcnow()
    db.commit()
    result.status = "ok"
    return result


class InvalidSignature(Exception):
    pass


@dataclass
class WebhookResult:
    received: int = 0
    duplicates: int = 0
    applied: int = 0
    dead_lettered: bool = False


def _apply_events(db: Session, conn: Connection, connector: Connector, headers: Mapping[str, str],
                  body: bytes) -> WebhookResult:
    result = WebhookResult()
    for event in connector.parse_webhook(headers, body):
        result.received += 1
        seen = db.scalar(select(WebhookEvent.id).where(
            WebhookEvent.connection_id == conn.id, WebhookEvent.event_id == event.event_id))
        if seen is not None:
            result.duplicates += 1
            continue
        db.add(WebhookEvent(connection_id=conn.id, event_id=event.event_id))
        if event.item is not None:
            result.applied += upsert_item(db, conn, event.item)
    db.commit()
    return result


def process_webhook(db: Session, conn: Connection, connector: Connector, headers: Mapping[str, str],
                    body: bytes, now: float) -> WebhookResult:
    if not connector.verify_webhook(headers, body, conn.webhook_secret, now):
        raise InvalidSignature()
    try:
        return _apply_events(db, conn, connector, headers, body)
    except IntegrityError:
        db.rollback()  # the same delivery is being processed concurrently
        return WebhookResult(received=1, duplicates=1)
    except Exception as exc:  # malformed payload or mapping bug: keep it for replay, don't make the sender retry forever
        db.rollback()
        payload = {"headers": dict(headers), "body_b64": base64.b64encode(body).decode()}
        db.add(DeadLetter(tenant=conn.tenant, connection_id=conn.id, kind="webhook",
                          payload=json.dumps(payload), error="{}: {}".format(type(exc).__name__, exc)))
        db.commit()
        return WebhookResult(dead_lettered=True)


def replay_dead_letter(db: Session, dl: DeadLetter, conn: Connection, connector: Connector) -> str:
    """Retry a dead letter after the underlying problem is fixed. Returns 'resolved' or the new error."""
    if dl.kind == "sync":
        result = run_sync(db, conn, connector)
        ok, error = result.status == "ok", result.error
    else:
        payload = json.loads(dl.payload)
        try:
            # Signature was verified when the webhook first arrived.
            _apply_events(db, conn, connector, payload["headers"], base64.b64decode(payload["body_b64"]))
            ok, error = True, ""
        except Exception as exc:
            db.rollback()
            ok, error = False, "{}: {}".format(type(exc).__name__, exc)
    if ok:
        dl.resolved_at = utcnow()
        db.commit()
        return "resolved"
    dl.error = error
    db.commit()
    return error
