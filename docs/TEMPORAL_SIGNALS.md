# Temporal Signals

## Overview

Temporal signals capture time-series metric snapshots from connected enterprise systems. They power the signal trend view and feed into the baseline calculator for scoring calibration.

Source: `backend/app/temporal.py`, `backend/app/routes_temporal.py`

---

## Data Flow

1. During a discovery run, signal values are captured and stored in the `signal_snapshots` table (SQLAlchemy ORM model at `backend/database/models/signal_snapshots.py`).
2. The background baseline calculator (`jobs/baseline_calculator.py`) periodically computes `baseline_mean` and `baseline_stddev` per `(org_id, signal_key)` over a configurable window.
3. `GET /api/runs/{runId}/signals` returns the temporal signals for a completed run.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/runs/{runId}/signals` | Return temporal signal data for a run |

Response: `TemporalSignal[]`

---

## Baseline Calculator

The baseline calculator runs as a background job on a configurable interval:

| Env var | Default | Purpose |
|---|---|---|
| `BASELINE_JOB_INTERVAL_HOURS` | `6` | How often the job runs |
| `BASELINE_MIN_RUNS` | `3` | Minimum signal snapshots required to compute a baseline |
| `BASELINE_WINDOW_DAYS` | `90` | Rolling window for baseline calculation |

The job is started in `main.py` lifespan alongside the connector health check job. It shuts down gracefully on SIGTERM.

---

## Signal Snapshot Schema

Stored in `signal_snapshots` table:

| Column | Type | Description |
|---|---|---|
| `org_id` | string | Organisation scope |
| `signal_key` | string | Unique signal identifier (e.g. `sf.case_backlog`) |
| `metric_value` | float | Captured value |
| `captured_at` | datetime | When the snapshot was taken |
| `baseline_mean` | float\|null | Computed rolling mean |
| `baseline_stddev` | float\|null | Computed rolling stddev |
| `baseline_window_days` | int\|null | Window used for last baseline calculation |
| `baseline_calculated_at` | datetime\|null | When baseline was last computed |

---

## Migrations

The `signal_snapshots` table is created by migration `0002_create_signal_snapshots.py` in `backend/migrations/`. Run `alembic upgrade head` before first use.
