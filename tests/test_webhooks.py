import hashlib
import hmac
import json
import time

from app.connectors.tracker import sign
from mock_tracker.main import webhook_payload

ISSUE = {"id": 99, "title": "Webhook issue", "status": "open", "assignee": "asha", "priority": "P1",
         "labels": ["customer"], "url": "https://tracker.example/issues/99", "updated_at": "2026-10-20T10:00:00Z"}
ACME = {"X-Tenant-ID": "acme"}


def create(api, provider="tracker", config=None):
    resp = api.post("/connections", headers=ACME, json={"provider": provider, "name": "t", "config": config or {}})
    resp.raise_for_status()
    return resp.json()


def send(api, conn, body, ts=None, secret=None):
    sig = sign(secret or conn["webhook_secret"], body, int(ts or time.time()))
    return api.post("/webhooks/{}".format(conn["id"]), content=body, headers={"X-Tracker-Signature": sig})


def test_valid_webhook_is_applied_and_searchable(api):
    conn = create(api)
    resp = send(api, conn, webhook_payload(ISSUE, "evt_1"))
    assert resp.status_code == 200 and resp.json()["applied"] == 1

    items = api.get("/items", headers=ACME, params={"q": "webhook"}).json()
    assert [i["source_id"] for i in items] == ["99"]


def test_duplicate_delivery_is_ignored(api):
    conn = create(api)
    body = webhook_payload(ISSUE, "evt_1")
    send(api, conn, body)
    again = send(api, conn, body).json()
    assert again["duplicates"] == 1 and again["applied"] == 0


def test_bad_and_stale_signatures_are_rejected(api):
    conn = create(api)
    body = webhook_payload(ISSUE, "evt_1")
    assert send(api, conn, body, secret="wrong").status_code == 401
    assert send(api, conn, body, ts=time.time() - 3600).status_code == 401
    unsigned = api.post("/webhooks/{}".format(conn["id"]), content=body)
    assert unsigned.status_code == 401
    assert api.get("/items", headers=ACME).json() == []


def test_out_of_order_events_keep_newest_version(api):
    conn = create(api)
    newer = dict(ISSUE, title="Newer title", updated_at="2026-10-20T12:00:00Z")
    older = dict(ISSUE, title="Older title", updated_at="2026-10-20T11:00:00Z")
    send(api, conn, webhook_payload(newer, "evt_2"))
    late = send(api, conn, webhook_payload(older, "evt_1")).json()

    assert late["applied"] == 0
    assert api.get("/items", headers=ACME).json()[0]["title"] == "Newer title"


def test_malformed_payload_goes_to_dlq(api):
    conn = create(api)
    body = json.dumps({"event_id": "evt_bad", "issue": {"id": 1}}).encode()
    resp = send(api, conn, body)
    assert resp.status_code == 202 and resp.json()["dead_lettered"]

    dlq = api.get("/dlq", headers=ACME).json()
    assert len(dlq) == 1 and dlq[0]["kind"] == "webhook" and not dlq[0]["resolved"]
    replay = api.post("/dlq/{}/replay".format(dlq[0]["id"]), headers=ACME).json()
    assert replay["result"] != "resolved"  # still broken: replay reports the error instead of hiding it


def test_github_webhook_signature_and_ping(api):
    conn = create(api, provider="github", config={"owner": "o", "repo": "r"})
    issue = {"number": 5, "title": "From GitHub", "state": "open", "labels": [{"name": "P0"}],
             "html_url": "https://github.com/o/r/issues/5", "updated_at": "2026-10-20T10:00:00Z"}
    body = json.dumps({"action": "opened", "issue": issue}).encode()
    sig = "sha256=" + hmac.new(conn["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    url = "/webhooks/{}".format(conn["id"])

    ping = api.post(url, content=b"{}", headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d0",
                                                 "X-Hub-Signature-256": "sha256=" + hmac.new(
                                                     conn["webhook_secret"].encode(), b"{}",
                                                     hashlib.sha256).hexdigest()})
    assert ping.json()["received"] == 0

    bad = api.post(url, content=body, headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d1",
                                               "X-Hub-Signature-256": "sha256=deadbeef"})
    assert bad.status_code == 401

    ok = api.post(url, content=body, headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d1",
                                              "X-Hub-Signature-256": sig})
    assert ok.json()["applied"] == 1
    item = api.get("/items", headers=ACME).json()[0]
    assert item["priority"] == "P0" and item["provider"] == "github"
