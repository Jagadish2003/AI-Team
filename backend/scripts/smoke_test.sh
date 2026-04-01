#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "== Health"
curl -sS "${BASE_URL}/health" >/dev/null

echo "== Connectors"
curl -sS "${hdr[@]}" "${BASE_URL}/api/connectors" >/dev/null

echo "== Uploads"
curl -sS "${hdr[@]}" "${BASE_URL}/api/uploads" >/dev/null

echo "== Start run"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{"connectedSources":["ServiceNow"],"uploadedFiles":[],"sampleWorkspaceEnabled":true}')
RUN_ID=$(echo "$RUN_JSON" | python -c "import sys, json; print(json.load(sys.stdin)['runId'])")

echo "== Run scoped endpoints"
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/evidence" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/entities" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/mappings" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/permissions" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/audit" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/roadmap" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/executive-report" >/dev/null

echo "== Fetch run + events"
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}" >/dev/null
curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events" >/dev/null

echo "== Replay (determinism check)"
EVENTS_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' >/dev/null
EVENTS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
test "$EVENTS_BEFORE" = "$EVENTS_AFTER"
echo "✅ Replay is deterministic"

echo "== Invalid runId must be 404"
set +e
code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" "${BASE_URL}/api/runs/run_does_not_exist_zzz")
set -e
test "$code" = "404"

echo "✅ Smoke test passed"