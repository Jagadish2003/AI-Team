#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{"connectedSources":["ServiceNow"],"uploadedFiles":[],"sampleWorkspaceEnabled":false}')
RUN_ID=$(echo "$RUN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['runId'])")

EVENTS_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' >/dev/null
EVENTS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")

test "$EVENTS_BEFORE" = "$EVENTS_AFTER"
echo "✅ Replay is deterministic (events identical before/after replay)"
