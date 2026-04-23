#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"

hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "1) Health"
code=$(curl -sS -o /tmp/t1_health.json -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health returned $code"; cat /tmp/t1_health.json; exit 1; }

echo "2) Start run"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{"connectedSources":["ServiceNow","Jira"],"uploadedFiles":["fileA.csv"],"sampleWorkspaceEnabled":false}')
RUN_ID=$(echo "$RUN_JSON" | python -c "import sys,json; print(json.load(sys.stdin)['runId'])")
test -n "$RUN_ID" || { echo "❌ missing runId"; echo "$RUN_JSON"; exit 1; }
echo "   runId=$RUN_ID"

echo "3) Compute (Track B)"
code=$(curl -sS -o /tmp/t1_compute.json -w "%{http_code}" "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/compute" -d '{"mode":"offline","systems":["salesforce","servicenow","jira"]}')
test "$code" = "200" || { echo "❌ compute returned $code"; cat /tmp/t1_compute.json; exit 1; }


# 3b) Poll run status until complete
echo "3b) Poll status until complete"
STATUS=""
for i in $(seq 1 30); do
  code=$(curl -sS -o /tmp/t1_status.json -w "%{http_code}" "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status")
  test "$code" = "200" || { echo "❌ status returned $code"; cat /tmp/t1_status.json; exit 1; }
  STATUS=$(python -c "import json; print(json.load(open('/tmp/t1_status.json')).get('status',''))")
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ]; then break; fi
  if [ "$STATUS" = "failed" ]; then echo "❌ run failed"; cat /tmp/t1_status.json; exit 1; fi
  sleep 1
done
[ "$STATUS" = "complete" ] || { echo "❌ timed out waiting for complete"; cat /tmp/t1_status.json; exit 1; }
echo "4) Fetch opportunities"
code=$(curl -sS -o /tmp/t1_opps.json -w "%{http_code}" "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
test "$code" = "200" || { echo "❌ opportunities returned $code"; cat /tmp/t1_opps.json; exit 1; }

python - <<'PY'
import json
with open("/tmp/t1_opps.json","r",encoding="utf-8") as f:
    opps=json.load(f)
assert isinstance(opps,list), "opps not list"
assert len(opps)>=1, "opps empty"
print(f"✅ opps={len(opps)}")
PY

echo "✅ Sprint4 T1 smoke passed"
