import hashlib
import hmac
import json
import re
from typing import Any, Dict, List, Mapping, Optional

import httpx

from app.connectors.base import Connector, IncomingEvent, NormalizedItem, Page, checked_get, parse_ts

PRIORITY_RE = re.compile(r"^(p[0-4]|priority[:/ ].+)$", re.IGNORECASE)


class GitHubConnector(Connector):
    """GitHub Issues: Link-header pagination, `since` incremental sync, HMAC-SHA256 webhooks."""

    provider = "github"

    def __init__(
        self,
        owner: str,
        repo: str,
        token: Optional[str] = None,
        since: Optional[str] = None,
        per_page: int = 100,
        http: Optional[httpx.Client] = None,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = "Bearer " + token
        self.http = http or httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20.0)
        self.owner = owner
        self.repo = repo
        self.initial_since = since
        self.per_page = per_page

    def normalize(self, issue: Dict[str, Any]) -> NormalizedItem:
        labels = [label["name"] for label in issue.get("labels", []) if isinstance(label, dict)]
        priority = next((label for label in labels if PRIORITY_RE.match(label)), None)
        return NormalizedItem(
            source_id=str(issue["number"]),
            kind="pull_request" if "pull_request" in issue else "issue",
            title=issue["title"],
            status=issue["state"],
            assignee=(issue.get("assignee") or {}).get("login"),
            priority=priority,
            url=issue.get("html_url"),
            labels=labels,
            updated_at=parse_ts(issue["updated_at"]),
            raw=issue,
        )

    def fetch_page(self, cursor: Dict[str, Any]) -> Page:
        if cursor.get("next_url"):
            resp = checked_get(self.http, cursor["next_url"])
        else:
            params = {"state": "all", "sort": "updated", "direction": "asc", "per_page": self.per_page}
            since = cursor.get("since") or self.initial_since
            if since:
                params["since"] = since
            resp = checked_get(self.http, "/repos/{}/{}/issues".format(self.owner, self.repo), params=params)

        items = [self.normalize(issue) for issue in resp.json()]
        high_water = cursor.get("high_water") or cursor.get("since") or self.initial_since
        for item in items:
            stamp = item.raw["updated_at"]
            if high_water is None or stamp > high_water:
                high_water = stamp

        next_url = resp.links.get("next", {}).get("url")
        if next_url:
            return Page(items, {"since": cursor.get("since"), "next_url": next_url, "high_water": high_water}, False)
        # GitHub's `since` is inclusive, so the newest item is re-fetched next run;
        # the updated_at guard in the upsert makes that a no-op.
        return Page(items, {"since": high_water, "next_url": None, "high_water": high_water}, True)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes, secret: str, now: float) -> bool:
        given = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> List[IncomingEvent]:
        if headers.get("x-github-event") != "issues":
            return []  # ping, push, etc.
        payload = json.loads(body)
        return [IncomingEvent(event_id=headers["x-github-delivery"], item=self.normalize(payload["issue"]))]
