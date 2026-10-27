#!/usr/bin/env bash
# End-to-end demo against running servers:
#   TRACKER_FAILURE_RATE=0.15 TRACKER_RATE_LIMIT_PER_SEC=5 uvicorn mock_tracker.main:app --port 9200
#   uvicorn app.main:app --port 8200
set -euo pipefail
API=${API:-http://localhost:8200}
TRACKER=${TRACKER:-http://127.0.0.1:9200}
PY=${PY:-.venv/bin/python}
acme=(-H "X-Tenant-ID: acme" -H "Content-Type: application/json")

conn=$(curl -sf "${acme[@]}" -X POST "$API/connections" \
  -d "{\"provider\":\"tracker\",\"name\":\"Acme tracker\",\"config\":{\"base_url\":\"$TRACKER\",\"page_size\":25}}")
id=$(echo "$conn" | $PY -c 'import sys,json;print(json.load(sys.stdin)["id"])')
secret=$(echo "$conn" | $PY -c 'import sys,json;print(json.load(sys.stdin)["webhook_secret"])')
echo "connection $id created"

echo "--- full sync (tracker is returning 429s and 500s)"
curl -sf "${acme[@]}" -X POST "$API/connections/$id/sync"; echo
echo "--- re-sync (should apply nothing)"
curl -sf "${acme[@]}" -X POST "$API/connections/$id/sync"; echo

echo "--- change an issue in the tracker, then incremental sync"
curl -sf -X POST "$TRACKER/api/issues/42" -H "Content-Type: application/json" -d '{"status":"closed"}' > /dev/null
curl -sf "${acme[@]}" -X POST "$API/connections/$id/sync"; echo

echo "--- signed webhook, the same delivery again, then a forged one"
body='{"event_id":"evt_demo_1","type":"issue.updated","issue":{"id":7,"title":"Fix login flow (hotfix)","status":"closed","assignee":"asha","priority":"P0","labels":["customer"],"url":"https://tracker.example/issues/7","updated_at":"2030-01-01T00:00:00Z"}}'
sig=$($PY -c 'import sys,time;from app.connectors.tracker import sign;print(sign(sys.argv[1],sys.argv[2].encode(),int(time.time())))' "$secret" "$body")
curl -s -X POST "$API/webhooks/$id" -H "X-Tracker-Signature: $sig" --data-binary "$body"; echo
curl -s -X POST "$API/webhooks/$id" -H "X-Tracker-Signature: $sig" --data-binary "$body"; echo
curl -s -o /dev/null -w "forged signature -> HTTP %{http_code}\n" -X POST "$API/webhooks/$id" \
  -H "X-Tracker-Signature: t=$(date +%s),v1=00" --data-binary "$body"

echo "--- query as acme, then as another tenant"
curl -sf -H "X-Tenant-ID: acme" "$API/items?q=hotfix"; echo
curl -sf -H "X-Tenant-ID: globex" "$API/items?q=hotfix"; echo
curl -sf -H "X-Tenant-ID: acme" "$API/connections"; echo
