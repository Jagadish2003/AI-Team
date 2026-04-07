A-Task-6 Integration Instructions (Track A backend)

Goal
- Wire POST /api/runs/{runId}/replay to call replay_run(runId)

Important
- A-Task-3 v1.1 already has a replay endpoint path in main.py.
- Do NOT add a second duplicate route. Instead, update the existing handler to call replay_run.

Steps
1) Copy backend/app/replay.py into your backend at: backend/app/replay.py

2) Open backend/app/main.py (from A-Task-3 v1.1)
   Find the existing endpoint:
     @app.post("/api/runs/{run_id}/replay")
     def ...

3) Replace the handler body with:
     from backend.app.replay import replay_run
     ...
     def post_replay(run_id: str):
         return replay_run(run_id)

4) Ensure the endpoint is protected the same way as other /api/* endpoints (Depends(require_auth)).
   If your existing replay endpoint already has auth dependencies, keep them.

5) Run the smoke test:
   BASE_URL=http://localhost:8000 DEV_JWT=dev-token-change-me bash scripts/smoke_replay_determinism.sh

Notes
- This pack intentionally does NOT import OkResponse (not present in A-Task-3 v1.1 models.py).
  The endpoint returns a dict: {"ok": true, "runId": "run_001"} which matches the contract.
