import asyncio
import json

from app import mcp_server

ACME = {"X-Tenant-ID": "acme"}
GLOBEX = {"X-Tenant-ID": "globex"}


def synced_connection(api):
    conn = api.post("/connections", headers=ACME, json={"provider": "tracker", "name": "Acme tracker"}).json()
    result = api.post("/connections/{}/sync".format(conn["id"]), headers=ACME).json()
    assert result["status"] == "ok" and result["applied"] == 250
    return conn


def test_tenant_header_required(api):
    assert api.get("/items").status_code == 422


def test_tenants_are_isolated(api):
    conn = synced_connection(api)
    item_id = api.get("/items", headers=ACME, params={"limit": 1}).json()[0]["id"]

    assert api.get("/items", headers=GLOBEX).json() == []
    assert api.get("/connections", headers=GLOBEX).json() == []
    assert api.get("/items/{}".format(item_id), headers=GLOBEX).status_code == 404
    assert api.post("/connections/{}/sync".format(conn["id"]), headers=GLOBEX).status_code == 404


def test_filters_and_connection_status(api):
    synced_connection(api)
    closed = api.get("/items", headers=ACME, params={"status": "closed", "limit": 200}).json()
    assert closed and all(i["status"] == "closed" for i in closed)
    status = api.get("/connections", headers=ACME).json()[0]
    assert status["status"] == "ok" and status["last_success_at"]
    assert "webhook_secret" not in status


def test_unknown_provider_rejected(api):
    resp = api.post("/connections", headers=ACME, json={"provider": "salesforce", "name": "x"})
    assert resp.status_code == 422


def test_mcp_tools_are_tenant_scoped(api, monkeypatch):
    synced_connection(api)
    monkeypatch.setenv("SYNCBRIDGE_TENANT", "acme")
    results = mcp_server.search_items(query="billing", limit=5)
    assert results and all("billing" in r["title"].lower() for r in results)
    assert mcp_server.get_item(results[0]["id"])["id"] == results[0]["id"]
    assert mcp_server.sync_status()[0]["status"] == "ok"

    monkeypatch.setenv("SYNCBRIDGE_TENANT", "globex")
    assert mcp_server.search_items(query="billing") == []


def test_mcp_server_lists_and_calls_tools(api, monkeypatch):
    synced_connection(api)
    monkeypatch.setenv("SYNCBRIDGE_TENANT", "acme")
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {t.name for t in tools} == {"search_items", "get_item", "sync_status"}

    result = asyncio.run(mcp_server.server.call_tool("search_items", {"query": "login", "limit": 3}))
    assert not result.is_error
    first = json.loads(result.content[0].text)
    assert "login" in first["title"].lower()
