# Task 4 — Contract Validation Tests

## Purpose
Prevent API drift by validating real FastAPI responses (not fixtures).

## How it works
- Tests run with pytest + FastAPI TestClient.
- The app is imported from `app/main.py` (Task 3 skeleton).
- Tests validate:
  - auth required on /api routes
  - run-scoped endpoints require runId and return 200
  - response payloads contain required keys
  - write endpoints are run-scoped
  - Stage 90 is non-empty (seed guard)

## Run
```bash
pytest tests/contract/ -v
```

## Note
This pack is meant to be copied into the Task 3 backend repo:
- Copy `backend/tests/` and `.github/workflows/contract-tests.yml`
- Ensure Task 2 seed_loader has populated `backend/dev.db`
