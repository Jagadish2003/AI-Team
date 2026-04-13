# SMOKE_DEMO_TASK12.md — S6/S7 run-scoped decisions + override round-trip

## Setup
1) Backend running + seeded
   - `python backend/seed_loader.py`
   - `uvicorn backend.app.main:app --reload --port 8000`
2) Frontend running
   - `.env.development`: VITE_API_BASE_URL + VITE_DEV_JWT
   - `npm install && npm run dev`

## Steps
1) S3: Start a run (confirm runId exists).
2) S6: Approve an opportunity → refresh → still APPROVED.
3) S6: Save override + reason → refresh → still present.
4) S7: Select same opportunity → details shows override rationale.
5) S7: Go to Review → S6 opens on same opportunity with same override.
6) S6: Audit shows newest-first; refresh; still newest-first.

✅ Pass = Task 12 complete.
