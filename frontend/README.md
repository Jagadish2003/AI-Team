# Task 2 — Seed Dataset Pack

## Purpose
Provide deterministic demo data for the backend. Frontend must never import seed data directly.

## One-command seed
```bash
python backend/seed_loader.py
```

Outputs:
- backend/dev.db (SQLite) containing JSON payload tables

## Edge cases included
- One connector in `error` state (Databricks)
- One evidence item `REJECTED`
- One opportunity with required permission missing (Microsoft 365)

## QA-critical opportunity values verified
- opp_002.effort = 7
- opp_005.effort = 7
- opp_006.decision = APPROVED
- opp_008.impact = 5, opp_008.effort = 2
