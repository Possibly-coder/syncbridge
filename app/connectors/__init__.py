import json
from typing import Optional

import httpx

from app.connectors.base import Connector
from app.connectors.github import GitHubConnector
from app.connectors.tracker import TrackerConnector
from app.models import Connection

PROVIDERS = ("github", "tracker")


def build_connector(conn: Connection, http: Optional[httpx.Client] = None) -> Connector:
    cfg = json.loads(conn.config_json or "{}")
    if conn.provider == "github":
        return GitHubConnector(
            owner=cfg["owner"], repo=cfg["repo"], token=cfg.get("token"), since=cfg.get("since"), http=http
        )
    if conn.provider == "tracker":
        return TrackerConnector(base_url=cfg.get("base_url", ""), page_size=cfg.get("page_size", 50), http=http)
    raise ValueError("unknown provider {}".format(conn.provider))
