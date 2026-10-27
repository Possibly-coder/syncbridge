import hashlib
import hmac
import json
from typing import Any, Dict, List, Mapping, Optional

import httpx

from app.connectors.base import Connector, IncomingEvent, NormalizedItem, Page, checked_get, parse_ts

SIGNATURE_TOLERANCE_SEC = 300


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return "t={},v1={}".format(timestamp, mac)


class TrackerConnector(Connector):
    """The mock ticket tracker in mock_tracker/: keyset cursors and Stripe-style timestamped signatures."""

    provider = "tracker"

    def __init__(self, base_url: str = "", page_size: int = 50, http: Optional[httpx.Client] = None) -> None:
        self.http = http or httpx.Client(base_url=base_url, timeout=10.0)
        self.page_size = page_size

    @staticmethod
    def normalize(issue: Dict[str, Any]) -> NormalizedItem:
        return NormalizedItem(
            source_id=str(issue["id"]),
            title=issue["title"],
            status=issue["status"],
            assignee=issue.get("assignee"),
            priority=issue.get("priority"),
            url=issue.get("url"),
            labels=issue.get("labels", []),
            updated_at=parse_ts(issue["updated_at"]),
            raw=issue,
        )

    def fetch_page(self, cursor: Dict[str, Any]) -> Page:
        params: Dict[str, Any] = {"limit": self.page_size}
        if cursor.get("updated_since"):
            params["updated_since"] = cursor["updated_since"]
        if cursor.get("page_cursor"):
            params["cursor"] = cursor["page_cursor"]
        data = checked_get(self.http, "/api/issues", params=params).json()

        items = [self.normalize(issue) for issue in data["issues"]]
        high_water = cursor.get("high_water") or cursor.get("updated_since")
        for item in items:
            stamp = item.raw["updated_at"]
            if high_water is None or stamp > high_water:
                high_water = stamp

        if data.get("next_cursor"):
            return Page(items, {"updated_since": cursor.get("updated_since"),
                                "page_cursor": data["next_cursor"], "high_water": high_water}, False)
        return Page(items, {"updated_since": high_water, "page_cursor": None, "high_water": high_water}, True)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes, secret: str, now: float) -> bool:
        try:
            parts = dict(p.split("=", 1) for p in headers.get("x-tracker-signature", "").split(","))
            timestamp = int(parts["t"])
        except (KeyError, ValueError):
            return False
        if abs(now - timestamp) > SIGNATURE_TOLERANCE_SEC:
            return False  # replayed or badly delayed
        return hmac.compare_digest(sign(secret, body, timestamp), headers.get("x-tracker-signature", ""))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> List[IncomingEvent]:
        payload = json.loads(body)
        return [IncomingEvent(event_id=payload["event_id"], item=self.normalize(payload["issue"]))]
