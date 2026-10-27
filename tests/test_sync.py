import time

import httpx
from sqlalchemy import func, select

from app.connectors.base import RetryableError
from app.connectors.tracker import TrackerConnector
from app.models import DeadLetter, Item
from app.sync import replay_dead_letter, run_sync
from tests.conftest import add_connection, make_tracker


def no_sleep(_seconds):
    pass


def count_items(db):
    return db.scalar(select(func.count()).select_from(Item))


def test_full_sync_through_flaky_api_then_rerun_is_noop(db):
    tracker = make_tracker(failure_rate=0.3)
    conn = add_connection(db)
    connector = TrackerConnector(http=tracker, page_size=50)

    first = run_sync(db, conn, connector, base_delay=0, sleep=no_sleep)
    assert first.status == "ok"
    assert count_items(db) == 250
    assert conn.status == "ok" and conn.last_success_at is not None

    second = run_sync(db, conn, connector, base_delay=0, sleep=no_sleep)
    assert second.status == "ok"
    assert second.applied == 0
    assert second.fetched <= 1  # only the inclusive high-water item is re-read


def test_incremental_sync_picks_up_changes(db, tracker):
    conn = add_connection(db)
    connector = TrackerConnector(http=tracker)
    run_sync(db, conn, connector, sleep=no_sleep)

    tracker.post("/api/issues/7", json={"status": "closed", "title": "Ship the fix"}).raise_for_status()
    result = run_sync(db, conn, connector, sleep=no_sleep)

    assert result.applied == 1
    assert result.fetched <= 2
    item = db.scalar(select(Item).where(Item.source_id == "7"))
    assert item.status == "closed" and item.title == "Ship the fix"


class Outage:
    """Wraps a connector; fails every call after `ok_pages` successful pages while `down` is True."""

    def __init__(self, inner, ok_pages):
        self.inner, self.ok_pages, self.down, self.calls = inner, ok_pages, True, 0

    def fetch_page(self, cursor):
        if self.down and self.calls >= self.ok_pages:
            raise RetryableError("upstream outage")
        self.calls += 1
        return self.inner.fetch_page(cursor)


def test_outage_dead_letters_then_replay_resumes_from_cursor(db, tracker):
    conn = add_connection(db)
    connector = Outage(TrackerConnector(http=tracker, page_size=50), ok_pages=2)

    result = run_sync(db, conn, connector, max_attempts=3, base_delay=0, sleep=no_sleep)
    assert result.status == "error"
    assert conn.status == "error" and "outage" in conn.last_error
    assert count_items(db) == 100  # the two finished pages were committed
    dl = db.scalar(select(DeadLetter))
    assert dl.kind == "sync" and dl.resolved_at is None

    connector.down = False
    assert replay_dead_letter(db, dl, conn, connector) == "resolved"
    assert count_items(db) == 250
    assert connector.calls == 5  # 2 before the outage + 3 remaining pages, no restart from page 1
    assert dl.resolved_at is not None and conn.status == "ok"


def test_permanent_error_is_not_retried(db):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "bad token"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://tracker")
    conn = add_connection(db)
    result = run_sync(db, conn, TrackerConnector(http=http), sleep=no_sleep)

    assert result.status == "error" and "401" in result.error
    assert len(calls) == 1


def test_rate_limit_honours_retry_after(db):
    tracker = make_tracker(n_issues=40, rate_limit_per_sec=2)
    conn = add_connection(db)
    sleeps = []

    def real_sleep(seconds):
        sleeps.append(seconds)
        time.sleep(seconds)

    result = run_sync(db, conn, TrackerConnector(http=tracker, page_size=10), sleep=real_sleep)
    assert result.status == "ok"
    assert count_items(db) == 40
    assert sleeps and all(s == 1.0 for s in sleeps)  # Retry-After, not the exponential default
