#!/usr/bin/env bash
# Sprint 4 T6 smoke — LLM Enrichment Layer
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "1) Health"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health $code"; exit 1; }
echo "   ✅ ok"

echo "2) Start run"

RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{
  "connectedSources": ["ServiceNow", "Jira"],
  "uploadedFiles": [],
  "sampleWorkspaceEnabled": false,
  "mode": "offline",
  "systems": ["salesforce", "servicenow", "jira"]
}')

echo "Raw response:"
echo "$RUN_JSON"

RUN_ID=$(python -c "import json; d=json.loads('''$RUN_JSON'''); print(d.get('runId') or d.get('id') or '')")

test -n "$RUN_ID" || { echo "❌ missing runId"; exit 1; }
echo "   runId=$RUN_ID"

echo "3) Poll until complete"

STATUS=""

for i in $(seq 1 90); do
  SJSON=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status" || true)

  STATUS=$(python -c "import json; d=json.loads('''$SJSON''') if '''$SJSON''' else {}; print(d.get('status','running'))")

  echo "   status=$STATUS"

  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ]; then break; fi
  if [ "$STATUS" = "failed" ]; then echo "❌ run failed"; exit 1; fi
  sleep 2
done

[ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ] || {
  echo "❌ timed out"; exit 1;
}

echo "4) Check /llm-enrichment available"

ENRICH=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/llm-enrichment")

AVAILABLE=$(python -c "import json; d=json.loads('''$ENRICH'''); print(d.get('available', False))")

test "$AVAILABLE" = "True" || test "$AVAILABLE" = "true" || {
  echo "❌ llm-enrichment not available"
  echo "$ENRICH"
  exit 1
}

LLM_GENERATED=$(python -c "import json; d=json.loads('''$ENRICH'''); print(d.get('opportunitiesEnriched',0))")

echo "   available=true, opportunitiesEnriched=$LLM_GENERATED"
echo "   ✅ enrichment available"

echo "5) Check per-opportunity enrichment"

OPPS=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")

OPP_ID=$(python -c "
import json
d=json.loads('''$OPPS''')
print(d[0]['id'] if d else '')
")

test -n "$OPP_ID" || { echo "❌ no opportunities"; exit 1; }
echo "   checking oppId=$OPP_ID"

OPP_ENRICH=$(curl -sS "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/opportunities/${OPP_ID}/enrichment")

python -c "
import json
d=json.loads('''$OPP_ENRICH''')

summary = d.get('aiSummary','')

print('   aiSummary length:', len(summary))
print('   llmGenerated:', d.get('llmGenerated'))
print('   aiWhyBullets:', len(d.get('aiWhyBullets',[])))

if not summary:
    raise SystemExit('❌ aiSummary is empty')

for f in ['impact','effort','tier','decision']:
    if f in d:
        raise SystemExit(f'❌ enrichment contains scoring field {f}')

print('   ✅ per-opportunity enrichment ok')
"

echo "6) Check executive report"

EXEC=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/executive-report")

python -c "
import json
d=json.loads('''$EXEC''')

if 'aiExecutiveSummary' not in d:
    raise SystemExit('❌ aiExecutiveSummary missing')

print('   aiExecutiveSummary length:', len(d.get('aiExecutiveSummary','')))
print('   ✅ executive report ok')
"


echo "7) Replay — enrichment stable"

# Get BEFORE
BEFORE_JSON=$(curl -sS "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/opportunities/${OPP_ID}/enrichment")

SUMMARY_BEFORE=$(python -c "import json; d=json.loads('''$BEFORE_JSON'''); print(d.get('aiSummary',''))")

# Trigger replay
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' > /dev/null

# Get AFTER
AFTER_JSON=$(curl -sS "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/opportunities/${OPP_ID}/enrichment")

SUMMARY_AFTER=$(python -c "import json; d=json.loads('''$AFTER_JSON'''); print(d.get('aiSummary',''))")

# Compare
if [ "$SUMMARY_BEFORE" = "$SUMMARY_AFTER" ]; then
  echo "   ✅ deterministic after replay"
else
  echo "❌ changed after replay"
  exit 1
fi




echo "8) 404 check"

code=$(curl -sS -o /dev/null -w "%{http_code}" \
  "${hdr[@]}" "${BASE_URL}/api/runs/run_does_not_exist_xyz/llm-enrichment")

test "$code" = "404" || { echo "❌ expected 404, got $code"; exit 1; }

echo ""
echo "✅ Sprint 4 T6 smoke passed"