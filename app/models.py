from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Connection(Base):
    """One customer's link to one external tool (e.g. tenant acme -> GitHub repo acme/api)."""

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    webhook_secret: Mapped[str] = mapped_column(String(128))
    cursor_json: Mapped[str] = mapped_column(Text, default="{}")
    # never_synced | syncing | ok | error
    status: Mapped[str] = mapped_column(String(32), default="never_synced")
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("connection_id", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), default="issue")
    title: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), index=True)
    assignee: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    priority: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    labels: Mapped[str] = mapped_column(String(512), default="")
    source_updated_at: Mapped[datetime] = mapped_column(DateTime)
    raw_json: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("connection_id", "event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # sync | webhook
    payload: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
