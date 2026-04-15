# A-Task-14 — Integrated Demo Smoke Walkthrough (S1 → S10)
Version: v1.0
Date: 2026-04-13

## Goal
Prove one coherent, deterministic, run-scoped walkthrough from S1 to S10:
- Same runId → same downstream artifacts
- Replay is deterministic
- Decisions + overrides persist and round-trip S6 ↔ S7 ↔ S6
- No-latest-run fallback

## Quick Setup
Backend:
- Start backend (seeded DB loaded)
- Ensure DEV_JWT matches frontend token

Frontend:
- VITE_API_BASE_URL=http://localhost:8000
- VITE_DEV_JWT=dev-token-change-me (or set localStorage.dev_jwt)

## UI Walkthrough Checklist
- S1: Connect ServiceNow + Jira (verify Connected)
- S2: Upload files (verify name/sizeLabel/uploadedLabel)
- S3: Start Run (verify runId shown + events load)
- S4: Evidence decision (verify audit newest-first)
- S5: Mapping row select (permissions filtered by source)
- S6: Save Override + Approve/Reject (verify persistence on refresh)
- S7: Override rationale shown in details; Go to Review selects same opp
- S8: Open an evidence detail view and verify `source`, `evidenceType`, `snippet`, and `confidence` render (quick sanity check).
- S9: 30/60/90 populated; readiness computed
- S10: sourcesAnalyzed derived from run.inputs; snapshot narrative consistent

## API Smoke Script
Run:
```bash
bash smoke_task14.sh
```
Expect: ✅ all checks pass