#!/usr/bin/env bash
# Sprint 4 T3 smoke — CrossSystem LinkedClusters
# Changes from v1.0:
#   Fix 1: json.load(sys.stdin) parsed once into variable d before .get() calls
#          (v1.0 called json.load twice on an exhausted stream, fragile)
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "1) Health"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health returned $code"; exit 1; }
echo "   ✅ ok"

echo "2) Start run"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{
  "connectedSources": ["ServiceNow", "Jira & Confluence"],
  "uploadedFiles":    ["upload_001"],
  "sampleWorkspaceEnabled": false,
  "mode":    "offline",
  "systems": ["salesforce", "servicenow", "jira"]
}')
# Fix 1: parse stdin once into d, then access both keys from the same object
RUN_ID=$(echo "$RUN_JSON" | python -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('runId') or d.get('id'))")
test -n "$RUN_ID" || { echo "❌ missing runId"; echo "$RUN_JSON"; exit 1; }
echo "   runId=$RUN_ID"

echo "3) Poll status until complete"
STATUS=""
for i in $(seq 1 120); do
  SJSON=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status" || true)
  if [ -n "$SJSON" ]; then
    STATUS=$(echo "$SJSON" | python -c \
      "import sys,json; print(json.load(sys.stdin).get('status','running'))" || echo "running")
  fi
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ]; then break; fi
  if [ "$STATUS" = "failed"   ]; then echo "❌ run failed"; exit 1; fi
  sleep 1
done
[ "$STATUS" = "complete" ] || { echo "❌ timed out waiting for complete"; exit 1; }

echo "4) Fetch clusters"
C1=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/clusters")
COUNT=$(echo "$C1" | python -c \
  "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo "   clusters=$COUNT"
echo "$C1" | python -c \
  "import sys,json
clusters=json.load(sys.stdin)
for c in clusters[:3]:
    print(f'   {c[\"id\"]} key={c[\"key\"]} sources={c[\"sources\"]} evIds={len(c[\"evidenceIds\"])}')"

echo "5) 404 for unknown runId"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" \
  "${BASE_URL}/api/runs/run_does_not_exist_xyz/clusters")
test "$code" = "404" || { echo "❌ expected 404 for unknown run, got $code"; exit 1; }
echo "   ✅ 404 confirmed"

echo "6) Replay"
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' >/dev/null

echo "7) Poll status after replay"
STATUS=""
for i in $(seq 1 120); do
  SJSON=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status" || true)
  if [ -n "$SJSON" ]; then
    STATUS=$(echo "$SJSON" | python -c \
      "import sys,json; print(json.load(sys.stdin).get('status','running'))" || echo "running")
  fi
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ]; then break; fi
  if [ "$STATUS" = "failed"   ]; then echo "❌ run failed after replay"; exit 1; fi
  sleep 1
done
[ "$STATUS" = "complete" ] || { echo "❌ timed out after replay"; exit 1; }

echo "8) Fetch clusters after replay and compare (determinism gate)"
C2=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/clusters")
test "$C1" = "$C2" || {
  echo "❌ clusters not deterministic after replay"
  echo "Before replay: $C1"
  echo "After  replay: $C2"
  exit 1
}
echo "   ✅ clusters deterministic"

echo ""
echo "✅ Sprint 4 T3 smoke passed"
