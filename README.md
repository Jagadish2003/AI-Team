# Task 3 — Layer 1 API Skeleton (FastAPI)

## Purpose
Serve contract-shaped JSON responses backed by the Task 2 SQLite DB (`backend/dev.db`), protected by a JWT stub.
This enables the frontend to switch from mocks to fetch calls without UI refactors.

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

## Run
```bash
./backend/run.sh
```

## Auth
All `/api/*` endpoints require:
```
Authorization: Bearer $DEV_JWT
```

## Smoke test
```bash
export DEV_JWT=dev-token-change-me
./backend/scripts/smoke_test.sh
```
