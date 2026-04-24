#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "== Health =="
curl -sS "${BASE_URL}/health" | python3 -c 'import sys,json; o=json.load(sys.stdin); assert o.get("ok") is True; print("✅ /health ok")'

echo "== Start run =="
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" \
  -d '{"connectedSources":["ServiceNow"],"uploadedFiles":[],"sampleWorkspaceEnabled":false}')
RUN_ID=$(echo "$RUN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['runId'])")
echo "RunId: ${RUN_ID}"

echo "== Fetch events before replay =="
EVENTS_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
echo "Events fetched: OK"

echo "== Trigger replay =="
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' >/dev/null
echo "Replay triggered: OK"

echo "== Fetch events after replay =="
EVENTS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
echo "Events fetched: OK"

echo "== Compare events =="
test "$EVENTS_BEFORE" = "$EVENTS_AFTER" || { echo "❌ replay events not deterministic"; exit 1; }
echo "✅ Replay is deterministic (events identical before/after replay)"
