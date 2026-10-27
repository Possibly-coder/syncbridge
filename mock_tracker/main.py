"""A mock SaaS ticket tracker that behaves like real ones on a bad day:
rate limits (429 + Retry-After), random 500s, and keyset-cursor pagination over a
dataset that keeps changing while you page through it."""
import json
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

VERBS = ["Fix", "Add", "Investigate", "Refactor", "Document", "Migrate", "Speed up", "Remove"]
THINGS = ["login flow", "billing export", "webhook retries", "search index", "SSO setup",
          "CSV import", "audit log", "rate limiter", "dashboard filters", "PDF parser"]
PEOPLE = ["asha", "ben", "chen", "diego", "emma", None]
STATUSES = ["open", "in_progress", "closed"]
PRIORITIES = ["P0", "P1", "P2", "P3"]


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def webhook_payload(issue: Dict[str, Any], event_id: str) -> bytes:
    return json.dumps({"event_id": event_id, "type": "issue.updated", "issue": issue}).encode()


def create_app(
    n_issues: int = 250,
    failure_rate: float = 0.1,
    rate_limit_per_sec: int = 20,
    seed: Optional[int] = None,
) -> FastAPI:
    app = FastAPI(title="Mock Tracker")
    rng = random.Random(seed)
    data_rng = random.Random(1234)
    lock = threading.Lock()
    recent: List[float] = []
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    issues: Dict[int, Dict[str, Any]] = {}
    for i in range(1, n_issues + 1):
        issues[i] = {
            "id": i,
            "title": "{} {}".format(data_rng.choice(VERBS), data_rng.choice(THINGS)),
            "status": data_rng.choice(STATUSES),
            "assignee": data_rng.choice(PEOPLE),
            "priority": data_rng.choice(PRIORITIES),
            "labels": data_rng.sample(["backend", "frontend", "customer", "security", "infra"], 2),
            "url": "https://tracker.example/issues/{}".format(i),
            "updated_at": _iso(start + timedelta(minutes=7 * i)),
        }
    clock = {"last": start + timedelta(minutes=7 * n_issues)}

    def _chaos() -> Optional[JSONResponse]:
        with lock:
            now = time.monotonic()
            recent[:] = [t for t in recent if now - t < 1.0]
            if len(recent) >= rate_limit_per_sec:
                return JSONResponse({"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"})
            recent.append(now)
            if rng.random() < failure_rate:
                return JSONResponse({"error": "internal error"}, status_code=500)
        return None

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "issues": len(issues)}

    @app.get("/api/issues")
    def list_issues(limit: int = 50, cursor: Optional[str] = None, updated_since: Optional[str] = None):
        failure = _chaos()
        if failure is not None:
            return failure
        rows = sorted(issues.values(), key=lambda r: (r["updated_at"], r["id"]))
        if updated_since:
            rows = [r for r in rows if r["updated_at"] >= updated_since]
        if cursor:
            ts, _, last_id = cursor.partition("|")
            rows = [r for r in rows if (r["updated_at"], r["id"]) > (ts, int(last_id))]
        page = rows[: max(1, min(limit, 100))]
        next_cursor = None
        if len(rows) > len(page):
            next_cursor = "{}|{}".format(page[-1]["updated_at"], page[-1]["id"])
        return {"issues": page, "next_cursor": next_cursor}

    @app.post("/api/issues/{issue_id}")
    def update_issue(issue_id: int, patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        if issue_id not in issues:
            raise HTTPException(404, "no such issue")
        with lock:
            issue = issues[issue_id]
            for key in ("title", "status", "assignee", "priority"):
                if key in patch:
                    issue[key] = patch[key]
            clock["last"] = max(datetime.now(timezone.utc), clock["last"] + timedelta(seconds=1))
            issue["updated_at"] = _iso(clock["last"])
            return dict(issue)

    return app


app = create_app(
    failure_rate=float(os.getenv("TRACKER_FAILURE_RATE", "0.1")),
    rate_limit_per_sec=int(os.getenv("TRACKER_RATE_LIMIT_PER_SEC", "20")),
)
