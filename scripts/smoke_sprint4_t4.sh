#!/usr/bin/env bash
# Sprint 4 T4 smoke — Deterministic Replay  v1.2 (Windows-compatible)
#
# Fix: Replaced /tmp file I/O with direct bash-variable-to-Python-arg passing.
# Git Bash translates /tmp to a Windows temp path, but the Windows Python
# executable resolves /tmp as C:\tmp (which doesn't exist), causing FileNotFoundError.
set -euo pipefail

# Windows Git Bash compatibility: python3 may be a Microsoft Store stub
if ! python3 --version &>/dev/null 2>&1; then
  python3() { PYTHONIOENCODING=utf-8 python "$@"; }
fi

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${DEV_JWT:-dev-token-change-me}"
hdr=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")

echo "1) Health"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${BASE_URL}/health")
test "$code" = "200" || { echo "❌ /health returned $code"; exit 1; }
echo "   ✅ ok"

echo "2) Start run"
RUN_JSON=$(curl -sS "${hdr[@]}" -X POST "${BASE_URL}/api/runs/start" -d '{
  "connectedSources": ["ServiceNow", "Jira"],
  "uploadedFiles":    ["smoke_file.csv"],
  "sampleWorkspaceEnabled": false,
  "mode":    "offline",
  "systems": ["salesforce", "servicenow", "jira"]
}')
RUN_ID=$(echo "$RUN_JSON" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('runId') or d.get('id'))")
test -n "$RUN_ID" || { echo "❌ missing runId"; echo "$RUN_JSON"; exit 1; }
echo "   runId=$RUN_ID"

echo "3) Poll until complete or partial"
STATUS=""
for i in $(seq 1 60); do
  SJSON=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status" || true)
  if [ -n "$SJSON" ]; then
    STATUS=$(echo "$SJSON" | python3 -c \
      "import sys,json; print(json.load(sys.stdin).get('status','running'))" || echo "running")
  fi
  echo "   status=$STATUS"
  if [ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ]; then break; fi
  if [ "$STATUS" = "failed" ]; then echo "❌ run failed"; exit 1; fi
  sleep 1
done
[ "$STATUS" = "complete" ] || [ "$STATUS" = "partial" ] || {
  echo "❌ timed out waiting for complete/partial"; exit 1;
}

echo "4) Capture pre-replay artifacts"
OPPS_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
COUNT_BEFORE=$(echo "$OPPS_BEFORE" | python3 -c \
  "import sys,json; print(len(json.load(sys.stdin)))")
echo "   opportunities before replay: $COUNT_BEFORE"
test "$COUNT_BEFORE" -ge 1 || { echo "❌ no opportunities before replay"; exit 1; }

CLUSTERS_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/clusters")

# Evidence: check if endpoint returns 200, else default to []
EV_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" \
  "${BASE_URL}/api/runs/${RUN_ID}/evidence")
if [ "$EV_CODE" = "200" ]; then
  EVIDENCE_BEFORE=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/evidence")
else
  EVIDENCE_BEFORE="[]"
fi

echo "5) Replay"
REPLAY_JSON=$(curl -sS "${hdr[@]}" -X POST \
  "${BASE_URL}/api/runs/${RUN_ID}/replay" -d '{}')
IS_REPLAY=$(echo "$REPLAY_JSON" | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('isReplay',''))")
test "$IS_REPLAY" = "True" || test "$IS_REPLAY" = "true" || {
  echo "❌ isReplay flag missing or false"
  echo "$REPLAY_JSON"
  exit 1
}
echo "   ✅ isReplay=true confirmed"

echo "6) Capture post-replay artifacts"
OPPS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/opportunities")
CLUSTERS_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/clusters")
if [ "$EV_CODE" = "200" ]; then
  EVIDENCE_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/evidence")
else
  EVIDENCE_AFTER="[]"
fi

echo "7) Determinism gate — opportunities, evidence, clusters (timestamps excluded)"
python3 - "$OPPS_BEFORE" "$OPPS_AFTER" "$CLUSTERS_BEFORE" "$CLUSTERS_AFTER" "$EVIDENCE_BEFORE" "$EVIDENCE_AFTER" <<'PY'
import json, sys

def strip_ts(obj):
    TS = {"replayedAt", "updatedAt", "completedAt", "startedAt"}
    if isinstance(obj, dict):
        return {k: strip_ts(v) for k, v in obj.items() if k not in TS}
    if isinstance(obj, list):
        return [strip_ts(i) for i in obj]
    return obj

def check(label, raw_before, raw_after):
    before = strip_ts(json.loads(raw_before))
    after  = strip_ts(json.loads(raw_after))
    if before != after:
        print(f"❌ {label} changed after replay")
        for i, (b, a) in enumerate(zip(before, after)):
            if b != a:
                print(f"  Index {i} differs:")
                print(f"  Before: {json.dumps(b)[:300]}")
                print(f"  After:  {json.dumps(a)[:300]}")
                break
        sys.exit(1)
    print(f"   ✅ {label}: {len(before)} items — identical before and after replay")

check("opportunities", sys.argv[1], sys.argv[2])
check("clusters",      sys.argv[3], sys.argv[4])

ev_before = strip_ts(json.loads(sys.argv[5]))
if ev_before:
    check("evidence", sys.argv[5], sys.argv[6])
else:
    print("   ⚠️  evidence: skipped (endpoint not yet wired or empty)")
PY

echo "8) Status reflects complete after replay"
ST_AFTER=$(curl -sS "${hdr[@]}" "${BASE_URL}/api/runs/${RUN_ID}/status")
STATUS_AFTER=$(echo "$ST_AFTER" | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('status',''))")
test "$STATUS_AFTER" = "complete" || {
  echo "❌ Expected status=complete after replay, got: $STATUS_AFTER"; exit 1;
}
echo "   ✅ status=complete"

echo "9) 404 for unknown runId"
code=$(curl -sS -o /dev/null -w "%{http_code}" "${hdr[@]}" \
  -X POST "${BASE_URL}/api/runs/run_does_not_exist_xyz/replay" -d '{}')
test "$code" = "404" || { echo "❌ expected 404 for unknown run, got $code"; exit 1; }
echo "   ✅ 404 confirmed"

echo ""
echo "✅ Sprint 4 T4 smoke passed — replay is deterministic"
echo "   Artifacts verified: opportunities, clusters (evidence if wired)"
