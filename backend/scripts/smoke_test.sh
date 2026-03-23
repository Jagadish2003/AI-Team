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

echo "✅ Smoke test passed"
