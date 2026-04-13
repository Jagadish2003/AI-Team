# SMOKE_DEMO_TASK11 — Screen 3 + Screen 4 Run‑Scoped Read Wiring

Goal: prove that **Screen 3 (Discovery Run)** and **Screen 4 (Partial Results)** read from run‑scoped API endpoints and remain deterministic when replayed.

## Preconditions
- Backend running (A‑Task‑3 v1.1 + A‑Task‑10 v1.2 or later).
- Frontend running with `VITE_API_BASE_URL` set.
- Dev auth token available (either `VITE_DEV_JWT` or localStorage `dev_jwt`).

## Steps

### 1) Connect sources (S1)
- Open **Integration Hub**.
- Click **Connect** for ServiceNow and Jira (at least 1 source).
- Confirm tiles show **Connected**.

Expected:
- `/api/connectors` returns connected statuses (200).
- UI reflects connection without refresh.

### 2) Add files (S2)
- Open **Source Intake**.
- Upload 1–2 files (or keep existing uploaded list from seed data).
- Confirm files list shows entries.

Expected:
- `/api/uploads` returns file list (200).
- UI shows file names + size labels.

### 3) Start run (S3)
- Navigate to **Discovery Run**.
- If no run exists, the page starts a run automatically once (no duplicates).

Expected:
- `POST /api/runs/start` returns `{ runId, status, startedAt }`.
- `GET /api/runs/{runId}` returns the run model.
- `GET /api/runs/{runId}/events` returns a list of events (non‑empty in seed).

### 4) Verify run inputs reflect S1/S2
- On S3, confirm the **Run Inputs** card lists the connected sources and uploaded files you configured.

Expected:
- Run inputs are derived from the **request body** to start run (not hardcoded mock data).

### 5) Navigate to S4 (Partial Results)
- Click **Next: Partial Results**.

Expected:
- Partial Results loads from `GET /api/runs/{runId}/entities` and/or `GET /api/runs/{runId}/evidence` (depending on screen wiring).
- No mock JSON imports are required for S3/S4 after wiring.

### 6) Replay determinism
- Go back to S3 and click **Replay Run**.
- Click **Refresh**.
- Compare the event list before vs after replay.

Expected:
- Same runId → same event stream (order and content consistent).
- No duplicate run is created.

### 7) Error + retry
- Temporarily stop the backend, refresh S3.
- Confirm error panel appears with Retry.
- Restart backend, click Retry.

Expected:
- Retry successfully loads without a full page reload.

## Pass criteria
- All API calls return 200 (except during the intentional outage test).
- RunId is stable across navigation.
- Replay keeps output deterministic.
- UI shows meaningful error + retry when backend unavailable.
