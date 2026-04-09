# Task 10 Integration (Backend)

This task ships **roadmap_engine.py** (Python port of buildRoadmap.ts) and updates how the Executive Report derives `sourcesAnalyzed`.

## Files included in this pack
- `backend/app/roadmap_engine.py` — reference implementation (pure function).
- `backend/app/main_task10_updated.py` — **full** `main.py` with Task 10 wiring applied.

## Apply (recommended)
Replace your existing `backend/app/main.py` with `backend/app/main_task10_updated.py` (rename it to `main.py`).

If you prefer manual edits, ensure these two changes are applied:
1) `/api/runs/{runId}/roadmap` returns `build_roadmap(get_opportunities(runId))`
2) `/api/runs/{runId}/executive-report` derives:
   - `totalConnected` from `run.inputs.connectedSources`
   - `recommendedConnected` by mapping connected source names to connector tiers
   - `uploadedFiles` from `run.inputs.uploadedFiles`

## Why this matters
- The UI story becomes deterministic: **what the user connected/uploaded is exactly what the report says was analyzed.**
- Contract tests validate the rule: **no "live state" fallback** for run-scoped analytics.
