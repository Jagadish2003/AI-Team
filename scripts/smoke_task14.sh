#!/usr/bin/env bash
set -euo pipefail

# Windows Git Bash compatibility: use python if python3 is not available
if ! python3 --version &>/dev/null 2>&1; then
  python3() { PYTHONIOENCODING=utf-8 python "$@"; }
fi

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"

hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "== Health =="
curl -sS "${BASE_URL}/health" | python3 -c 'import sys,json; o=json.load(sys.stdin); assert o.get("ok") is True; print("✅ /health ok")'

echo "== Start run with explicit inputs =="
RUN_BODY='{"connectedSources":["ServiceNow","Jira"],"uploadedFiles":["incident_data.csv","cmdb_records.xlsx"],"sampleWorkspaceEnabled":false}'
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d "${RUN_BODY}")
RUN_ID=$(echo "$RUN_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["runId"])')
echo "RunId: ${RUN_ID}"

echo "== Run-scoped reads =="
for ep in \
  "/api/runs/${RUN_ID}" \
  "/api/runs/${RUN_ID}/events" \
  "/api/runs/${RUN_ID}/audit" \
  "/api/runs/${RUN_ID}/opportunities" \
  "/api/runs/${RUN_ID}/roadmap" \
  "/api/runs/${RUN_ID}/executive-report"
do
  code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" "${BASE_URL}${ep}")
  test "$code" = "200" || { echo "❌ $ep returned $code"; exit 1; }
done
echo "✅ run-scoped endpoints return 200"

echo "== No-fallback (invalid runId should 404) =="
BAD="run_does_not_exist_zzz"
for ep in \
  "/api/runs/${BAD}" \
  "/api/runs/${BAD}/events" \
  "/api/runs/${BAD}/evidence" \
  "/api/runs/${BAD}/opportunities" \
  "/api/runs/${BAD}/roadmap" \
  "/api/runs/${BAD}/executive-report"
do
  code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" "${BASE_URL}${ep}")
  test "$code" = "404" || { echo "❌ $ep expected 404, got $code"; exit 1; }
done
echo "✅ no fallback rule holds (404 on unknown runId)"

echo "== Wait for run to complete =="
STATUS=""
for i in $(seq 1 30); do
  ST=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status")
  STATUS=$(echo "$ST" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ] || [ "$STATUS" = "failed" ]; then break; fi
  sleep 1
done
test "$STATUS" != "failed" || { echo "❌ run failed"; exit 1; }
echo "✅ run status: ${STATUS}"

echo "== Persist decision (one opportunity) =="
OPPS=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
OPP_ID=$(echo "$OPPS" | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')

curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/opportunities/${OPP_ID}/decision" \
  -d '{"decision":"APPROVED"}' >/dev/null

OPPS2=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
DEC=$(echo "$OPPS2" | python3 -c 'import sys,json; opps=json.load(sys.stdin); print([o for o in opps if o["id"]=="'"${OPP_ID}"'"][0]["decision"])')
test "$DEC" = "APPROVED" || { echo "❌ decision did not persist"; exit 1; }
echo "✅ decision persisted"

echo "== Replay determinism (events) =="
EV_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}' >/dev/null
EV_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/events")
test "$EV_BEFORE" = "$EV_AFTER" || { echo "❌ replay events not deterministic"; exit 1; }
echo "✅ replay deterministic (events)"

echo "ALL DONE ✅"

echo ""
echo "== Override round-trip (S6 → S7 → S6) =="
OVERRIDE_JSON=$(cat <<EOF
{
  "rationaleOverride": "Override: route high-priority incidents to L2 immediately to reduce handoffs.",
  "overrideReason": "Pilot prioritization for operational impact",
  "isLocked": false
}
EOF
)
curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/${RUN_ID}/opportunities/${OPP_ID}/override" \
  -H "Content-Type: application/json" -d "${OVERRIDE_JSON}" >/dev/null
OPPS_AFTER_OVERRIDE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
python3 - "$OPPS_AFTER_OVERRIDE" "$OPP_ID" <<'PY'
import json,sys
opps=json.loads(sys.argv[1])
opp=next(o for o in opps if o["id"]==sys.argv[2])
ov=opp.get("override") or {}
assert "rationaleOverride" in ov, "override object missing"
assert ov.get("rationaleOverride","").startswith("Override:"), "override rationale not persisted"
print("✅ Override persisted and visible in opportunities payload")
PY

echo "== Audit newest-first check =="
AUDIT=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/audit")
python3 - "$AUDIT" <<'PY'
import json,sys
a=json.loads(sys.argv[1])
assert isinstance(a,list) and len(a)>0
epochs=[int(e.get("tsEpoch",0)) for e in a]
assert any(ep>0 for ep in epochs), "no tsEpoch values in audit entries"
assert epochs==sorted(epochs, reverse=True), "audit not newest-first by tsEpoch"
print("✅ Audit is newest-first by tsEpoch")
PY