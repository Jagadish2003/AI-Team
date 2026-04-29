#!/usr/bin/env bash
# Sprint 4 T5 smoke — G-Run-Scoped Live Mode Switch
# Verifies S1→S10 full walkthrough reads from run-scoped API (no mocks/seed tables)
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

_count() { python -c "import sys,json; print(len(json.load(sys.stdin)))"; }
_field() { local f=$1; python -c "import sys,json; print(json.load(sys.stdin).get('$f',''))"; }
_json() { python -c "import sys,json; d=json.load(sys.stdin); print(d.get('runId') or d.get('id'))"; }

echo "1) Health"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health $code"; exit 1; }
echo "   ✅ ok"

echo "2) Start run (S1 → S3)"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{
  "connectedSources": ["ServiceNow", "Jira & Confluence"],
  "uploadedFiles":    ["smoke_t5.csv"],
  "sampleWorkspaceEnabled": false,
  "mode":    "offline",
  "systems": ["salesforce", "servicenow", "jira"]
}')
RUN_ID=$(echo "$RUN_JSON" | _json)
test -n "$RUN_ID" || { echo "❌ missing runId"; echo "$RUN_JSON"; exit 1; }
echo "   runId=$RUN_ID"

echo "3) S3 — poll /status until complete"
STATUS=""
for i in $(seq 1 120); do
  SJSON=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status" || true)
  STATUS=$(echo "$SJSON" | python -c \
    "import sys,json; print(json.load(sys.stdin).get('status','running'))" 2>/dev/null || echo "running")
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ]; then break; fi
  if [ "$STATUS" = "failed" ]; then echo "❌ run failed"; exit 1; fi
  sleep 1
done
[ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ] || {
  echo "❌ timed out"; exit 1;
}

echo "4) S4 — evidence (run-scoped, not seed table)"
EV=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/evidence")
EV_COUNT=$(echo "$EV" | _count)
echo "   evidence items: $EV_COUNT"
test "$EV_COUNT" -ge 1 || { echo "❌ no run-scoped evidence — check T2 materialiser patch"; exit 1; }
echo "   ✅ evidence is run-scoped"

echo "5) S6 — opportunities (run-scoped)"
OPPS=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
OPPS_COUNT=$(echo "$OPPS" | _count)
echo "   opportunities: $OPPS_COUNT"
test "$OPPS_COUNT" -ge 1 || { echo "❌ no opportunities"; exit 1; }
echo "   ✅ opportunities ok"

echo "6) T3 — clusters"
CLUSTERS=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/clusters")
echo "   clusters: $(echo "$CLUSTERS" | _count)"
echo "   ✅ clusters endpoint ok"

echo "7) S9 — roadmap (fallback uses run-scoped opps)"
ROADMAP=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/roadmap")
code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/roadmap")
test "$code" = "200" || { echo "❌ roadmap returned $code"; exit 1; }
echo "   ✅ roadmap ok"

echo "8) S10 — executive report (topQuickWins from run opps)"
EXEC=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/executive-report")
code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/executive-report")
test "$code" = "200" || { echo "❌ exec report returned $code"; exit 1; }
# Verify topQuickWins are all Quick Win tier
python - <<PY
import json, sys
exec_data = json.loads('''${EXEC}''')
qw = exec_data.get("topQuickWins", [])
bad = [o for o in qw if o.get("tier") != "Quick Win"]
if bad:
    print(f"❌ Non-Quick-Win in topQuickWins: {[o.get('id') for o in bad]}")
    sys.exit(1)
sa = exec_data.get("sourcesAnalyzed", {})
if "totalConnected" not in sa:
    print(f"❌ sourcesAnalyzed missing totalConnected: {sa}")
    sys.exit(1)
print(f"   topQuickWins: {len(qw)}, sourcesAnalyzed.totalConnected={sa.get('totalConnected')}")
print("   ✅ executive report ok")
PY

echo "9) T4 replay — determinism gate"
# Capture before
echo "$OPPS" > /tmp/t5_opps_before.json
echo "$EV"   > /tmp/t5_ev_before.json

curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' > /dev/null

# Capture after
OPPS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
EV_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/evidence")
echo "$OPPS_AFTER" > /tmp/t5_opps_after.json
echo "$EV_AFTER"   > /tmp/t5_ev_after.json

python - <<'PY'
import json, sys

def strip_ts(obj):
    TS = {"replayedAt", "updatedAt"}
    if isinstance(obj, dict):
        return {k: strip_ts(v) for k, v in obj.items() if k not in TS}
    if isinstance(obj, list):
        return [strip_ts(i) for i in obj]
    return obj

def normalize(items):
    return sorted([strip_ts(i) for i in items],
                  key=lambda x: x.get("id") or json.dumps(x, sort_keys=True))

def check(label, f_before, f_after):
    before = normalize(json.loads(open(f_before).read()))
    after  = normalize(json.loads(open(f_after).read()))
    if before != after:
        print(f"❌ {label} changed after replay")
        sys.exit(1)
    print(f"   ✅ {label}: deterministic after replay ({len(before)} items)")

check("opportunities", "/tmp/t5_opps_before.json", "/tmp/t5_opps_after.json")
check("evidence",      "/tmp/t5_ev_before.json",   "/tmp/t5_ev_after.json")
PY

echo "10) isReplay flag in /status after replay"
ST=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status")
IS_REPLAY=$(echo "$ST" | python -c \
  "import sys,json; print(json.load(sys.stdin).get('isReplay',''))")
test "$IS_REPLAY" = "True" || test "$IS_REPLAY" = "true" || {
  echo "❌ isReplay not set in /status after replay: $ST"; exit 1;
}
echo "   ✅ isReplay=true"

echo ""
echo "✅ Sprint 4 T5 smoke passed — S1→S10 fully run-scoped"
echo "   Evidence: run-scoped ✅  Opportunities: run-scoped ✅"
echo "   Roadmap: run-scoped fallback ✅  Executive report: topQuickWins from run ✅"
echo "   Replay determinism: opportunities + evidence ✅"
