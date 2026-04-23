#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DEV_JWT="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${DEV_JWT}" -H "Content-Type: application/json")

echo "1) Health"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health returned $code"; exit 1; }

echo "2) Start run"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{
  "connectedSources": ["ServiceNow","Jira"],
  "uploadedFiles": ["runbook.csv","handoff.xlsx"],
  "sampleWorkspaceEnabled": false,
  "mode": "offline",
  "systems": ["salesforce","servicenow","jira"]
}')
RUN_ID=$(echo "$RUN_JSON" | python -c "import sys,json; print(json.load(sys.stdin)['runId'])")
echo "   runId=$RUN_ID"

echo "3) Poll status"
STATUS=""
for i in $(seq 1 30); do
  ST=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status")
  STATUS=$(echo "$ST" | python -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ] || [ "$STATUS" = "failed" ]; then break; fi
  sleep 1
done
test "$STATUS" != "failed" || { echo "❌ run failed"; exit 1; }

echo "4) Fetch opportunities"
OPPS=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
COUNT=$(echo "$OPPS" | python -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "   opps=$COUNT"
test "$COUNT" -ge 1 || { echo "❌ expected >=1 opportunity"; exit 1; }

echo "✅ OK"
