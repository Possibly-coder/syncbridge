from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

import httpx
from pydantic import BaseModel


class NormalizedItem(BaseModel):
    source_id: str
    kind: str = "issue"
    title: str
    status: str
    assignee: Optional[str] = None
    priority: Optional[str] = None
    url: Optional[str] = None
    labels: List[str] = []
    updated_at: datetime
    raw: Dict[str, Any] = {}


@dataclass
class Page:
    items: List[NormalizedItem]
    cursor: Dict[str, Any]  # persisted after the page is stored, so a crash resumes here
    done: bool


@dataclass
class IncomingEvent:
    event_id: str
    item: Optional[NormalizedItem] = None


class RetryableError(Exception):
    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    pass


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def checked_get(http: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """GET that classifies failures: retry 429/5xx/network, fail fast on everything else."""
    try:
        resp = http.get(url, **kwargs)
    except httpx.TransportError as exc:
        raise RetryableError("transport error: {}".format(exc))
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        raise RetryableError("rate limited", float(retry_after) if retry_after else None)
    if resp.status_code >= 500:
        raise RetryableError("server error {}".format(resp.status_code))
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        # GitHub signals an exhausted quota with 403 rather than 429.
        reset = resp.headers.get("X-RateLimit-Reset")
        wait = max(float(reset) - datetime.now(timezone.utc).timestamp(), 1.0) if reset else None
        raise RetryableError("rate limit exhausted", wait)
    if resp.status_code >= 400:
        raise PermanentError("HTTP {}: {}".format(resp.status_code, resp.text[:200]))
    return resp


class Connector:
    provider = "base"

    def fetch_page(self, cursor: Dict[str, Any]) -> Page:
        raise NotImplementedError

    def verify_webhook(self, headers: Mapping[str, str], body: bytes, secret: str, now: float) -> bool:
        raise NotImplementedError

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> List[IncomingEvent]:
        raise NotImplementedError
