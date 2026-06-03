# AgentIQ Claude Instructions

## Purpose

AgentIQ is a full-stack discovery and opportunity-analysis app. The backend ingests enterprise system signals, detects workflow/friction patterns, scores opportunities, materializes run-scoped artifacts, and serves contract-shaped API responses. The frontend is a Vite React app that guides users through integration setup, discovery runs, source intelligence, opportunity review, pilot roadmap, agent blueprint, and executive report views.

Keep this file short and actionable. Prefer reading the relevant code and contract files over relying on this file for every detail.

## First Rules

* Protect user work. Check `git status --short --branch` before edits and do not revert unrelated changes.
* Do not commit secrets, generated databases, local env files, token files, or server keys.
* Preserve API response shapes. `contracts/API_CONTRACT.md` and `contracts/CONTRACT_RULES.md` are the source of truth when frontend and backend disagree.
* Run-scoped endpoints must include `runId` in the URL. Do not add "latest run" fallbacks.
* Before editing shared behavior, inspect existing route/context/test patterns and make the smallest compatible change.
* After code changes, run the narrowest relevant tests first, then broader tests when the change affects contracts, shared API helpers, routing, or pipeline behavior.

## Tech Stack

* Backend: Python 3.11, FastAPI, Pydantic, SQLite JSON payload tables, pytest.
* Frontend: React 18, TypeScript, Vite, React Router, Tailwind, Vitest, Testing Library.
* Data/pipeline: offline fixtures by default, optional live Salesforce/ServiceNow/Jira/nCino ingestion, pack-aware detectors and LLM enrichment.
* Auth: bearer token via `DEV_JWT`; default local token is `dev-token-change-me`.

## Repository Map

* `backend/app/main.py`: FastAPI app, CORS, core API routes, route registration.
* `backend/app/routes_*.py`: feature route modules for stack builder, run lifecycle, replay, normalization, enrichment, blueprint, workspace catalog, and connector/product APIs.
* `backend/app/db.py`: SQLite access, run records, run events, run-scoped KV helpers.
* `backend/app/run_store.py`: run read/start helpers, separate from db.py.
* `backend/app/materialize_t2.py`: run materialization, status, audit, events, roadmap/report persistence.
* `backend/app/llm_enrichment.py`: LLM enrichment post-processing (advisory only, must not mutate scoring fields).
* `backend/app/executive_report_engine.py`: executive report generation.
* `backend/app/roadmap_engine.py`: roadmap build logic.
* `backend/app/cross_system_linker.py`: cross-system signal linking across Salesforce/ServiceNow/Jira.
* `backend/app/telemetry.py`: telemetry and analytics event tracking.
* `backend/app/rbac.py`: role-based access control helpers and role enforcement.
* `backend/app/security.py`: auth/security helpers, bearer token validation.
* `backend/app/connector_health.py` / `connector_metrics.py`: connector health status and metrics.
* `backend/app/jobs/`: background jobs (e.g., connector health check job started on app startup).
* `backend/app/middleware/tenancy.py`: multi-tenancy middleware, org scoping per request.
* `backend/app/auth/`: connector auth configs, secret validation, OAuth handling.
* `backend/app/db_connectors/`: DB connector implementations for live data sources.
* `backend/discovery/`: ingest, detectors, scoring, evidence building, pack config, runner CLI.
* `backend/discovery/calibration/`: scoring calibration logic.
* `backend/discovery/lending_scorer.py` / `strs_benefits_scorer.py`: pack-specific scorers for nCino and STRS packs.
* `backend/discovery/integration_verifier.py`: verifies integration signal completeness.
* `backend/discovery/track_a_adapter.py`: Track A ingestion adapter.
* `backend/discovery/offline_export.py`: offline fixture/data export utilities.
* `backend/database/seed_loader.py`: creates and seeds `backend/database/dev.db` from `backend/database/seed/`.
* `backend/tests/contract/`: backend contract and API tests. Contract tests use a temp SQLite DB.
* `backend/tests/unit/`: unit tests for individual backend modules.
* `backend/discovery/tests/`: discovery/ingest/detector/runner tests.
* `frontend/src/lib/apiClient.ts`: shared frontend API helpers and auth header.
* `frontend/src/api/`: typed frontend API wrappers.
* `frontend/src/context/`: data-loading state providers.
* `frontend/src/pages/`: routed product screens.
* `frontend/src/components/`: reusable UI and page-specific components.
* `frontend/src/services/`: frontend service layer (business logic separate from raw API calls).
* `frontend/src/utils/`: shared frontend utility helpers.
* `frontend/src/data/`: local static/fixture data files used by the frontend.
* `frontend/src/types/`: frontend schema references for backend response shapes.
* `contracts/`: API contract, mock-to-endpoint map, contract rules.
* `docs/`: integration notes, smoke demo notes, auth setup, detector/scoring docs.
* `scripts/`: shell smoke tests and contract helper.

## Setup Commands

Use PowerShell on Windows unless the user is already in Git Bash.

Backend:

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python database\seed_loader.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Frontend:

```powershell
cd frontend
npm install
npm run dev
```

App URL:

```text
http://localhost:5173/
```

Git Bash alternative for the backend:

```bash
cd backend
source .venv/Scripts/activate
./run.sh
```

## Verification Commands

Backend contract tests:

```powershell
cd backend
python -m pytest
python -m pytest tests\contract\test_workspace_catalog.py
```

Discovery tests:

```powershell
cd backend
python -m pytest discovery\tests
```

Frontend build and tests:

```powershell
cd frontend
npm run build
npx vitest run
npx vitest run src\__tests__\OpportunityReviewPage.test.tsx
```

Contract mapping helper:

```powershell
cd frontend
python ..\scripts\validate_contract.py
```

Smoke scripts are Bash scripts under `scripts/` and `backend/scripts/`; run them from Git Bash or WSL.

## Runtime And Env Notes

* Backend `.env` and frontend `.env` are intentionally untracked.
* Backend database defaults to `backend/database/dev.db`. Override with `DB_PATH` for tests or isolated runs.
* Seed data location defaults to `backend/database/seed`; override with `SEED_DIR`.
* Local backend CORS defaults allow `localhost:5173` through `localhost:5176`. Override the allowed origins list with `CORS_ORIGINS` (comma-separated) for non-default dev ports or staging.
* Frontend API base uses `VITE_API_BASE_URL`, defaulting to `http://localhost:8000` in dev.
* Frontend auth uses `VITE_DEV_JWT`, defaulting to `dev-token-change-me`.
* Optional LLM features use `ANTHROPIC_API_KEY`; deterministic fallbacks should still work without it.
* Set `REQUIRE_CONNECTOR_SECRETS=1` in production to enforce that all connector secrets are present on startup. Dev and test environments intentionally leave this unset.
* Live ingestion env vars include `SF_INSTANCE_URL`, `SF_ACCESS_TOKEN`, `SERVICENOW_URL`, `SERVICENOW_TOKEN`, `SERVICENOW_USER`, `SERVICENOW_PASS`, `JIRA_URL`, `JIRA_TOKEN`, `JIRA_USER`, `JIRA_PROJECT_KEY`, `NCINO_INSTANCE_URL`, and `NCINO_ACCESS_TOKEN`.
* Token-generation scripts also use `SF_CLIENT_ID`/`SF_USER`, `NCINO_CLIENT_ID`/`NCINO_USER`, `STRS_CLIENT_ID`/`STRS_USER`, and `LS_CLIENT_ID`/`LS_USER`.

## Architecture Notes

* The API protects most endpoints with `require_auth`; tests usually pass `Authorization: Bearer dev-token-change-me`.
* SQLite stores most tables as `{ id, payload }` JSON rows, plus `runs`, `run_events`, and run-scoped KV entries.
* Run lifecycle starts at `POST /api/runs/start`, then materialization writes status, events, opportunities, evidence, clusters, roadmap, executive report, and enrichment into run-scoped storage.
* Replay should re-serve persisted artifacts only. It must not call live ingestion or regenerate LLM output.
* LLM enrichment is advisory post-processing. It must not mutate scoring fields such as impact, effort, tier, decision, or evidence IDs.
* Pack selection is centralized in `backend/discovery/packs/pack_config.py`. Current packs include `service_cloud`, `ncino`, and `strs_benefits`.
* nCino and STRS packs have compliance guardrails. Do not suggest automated credit or benefit decisions; keep humans responsible for final decisions.
* Multi-tenancy is enforced via `middleware/tenancy.py`. Every request is scoped to an org; the default local org is `default`.
* RBAC is enforced via `rbac.py`. The dev user is seeded as owner of the default org on startup via `seed_owner`. Use `require_role` to gate privileged routes.
* A background connector health check job starts on app startup (`jobs/connector_health.py`) and shuts down on SIGTERM. Do not block startup waiting for connectors.
* Telemetry events are tracked via `telemetry.py`. Do not log sensitive field values (tokens, PII) in telemetry events.
* The frontend should fetch backend data through `frontend/src/lib/apiClient.ts` or typed wrappers in `frontend/src/api/`.
* Avoid direct frontend imports of backend seed data or fixture JSON unless a test explicitly owns that fixture.

## Contract Rules

* If changing a backend response consumed by the UI, inspect the matching `frontend/src/types/*` type and `contracts/API_CONTRACT.md`.
* Any `frontend/src/types/*.ts` schema change requires a corresponding contract update and version bump.
* New run-scoped data should live under `/api/runs/{runId}/...`.
* Missing fields should generally be added by the backend to match the contract, not papered over in the UI.
* Audit lists are newest-first.
* Decision enums use uppercase values: `APPROVED`, `REJECTED`, `UNREVIEWED`.
* Readiness/status enums use the casing already defined in the relevant frontend type or contract.

## Frontend Conventions

* Keep UI changes consistent with existing page/component structure. This app is an operational tool, so favor dense, scan-friendly layouts over marketing-style sections.
* Use existing common components in `frontend/src/components/common/` when possible.
* Preserve route redirects in `frontend/src/App.tsx` unless the task is explicitly navigation cleanup.
* Keep API calls typed, handle `ApiError`, and avoid hardcoded localhost outside dev fallbacks.
* When changing user-visible flow behavior, update or add Vitest tests under `frontend/src/__tests__/`.

## Backend Conventions

* Register new route modules from `backend/app/main.py` following existing route-registration style.
* Use Pydantic response models for contract-sensitive routes where practical.
* Use `db.run_kv_get` and `db.run_kv_set` for run-scoped artifacts.
* Keep offline mode deterministic and usable without live credentials.
* Live connector failures should usually degrade to fixture/partial/error status rather than crashing the whole run, unless a test or contract says otherwise.
* Do not add duplicate endpoint registrations without checking FastAPI route order and existing tests.

## Testing Guidance

* For backend API work, prefer focused contract tests in `backend/tests/contract/`.
* For discovery logic, test detectors/scorers/ingest in `backend/discovery/tests/` or the relevant `backend/tests/unit/` file.
* Contract tests seed an isolated temp DB through `backend/tests/contract/conftest.py`; do not make tests depend on local `dev.db`.
* For frontend API/state changes, mock API boundaries rather than reaching into backend files.
* For changes affecting the full run pipeline, verify start/status/artifact endpoints together.

## Security And Data Hygiene

* Never print full tokens, private keys, or `.env` contents.
* The repo currently ignores expected server keys under provider-specific token directories. Treat any untracked `*.key` file as sensitive.
* Generated `.db`, `.venv`, `node_modules`, build output, and token JSON files should stay untracked.
* If a real credential file appears outside ignored paths, ask before moving it and suggest adding a precise `.gitignore` entry.

## Useful Prompts For This Repo

* "Trace this UI field from component to API response and contract before editing."
* "Update the backend to match the contract, then run the focused contract test."
* "Add a frontend test for this state transition and run only the affected Vitest file."
* "Check whether this should be pack-specific in `pack_config.py` instead of hardcoded."
* "Verify offline mode still works without live credentials."

## Known Gotchas

* `routes_sprint4_t5.py` exists in `backend/app/` but is **not registered** in `main.py`. Do not assume it is active.
* `PATCH_materialize_t2_t6.py` is a patch/migration file in `backend/app/`, not a regular module. Do not import it as a route or service.
* `routes_sprint4_t5.py` being unregistered means any routes defined in it are silently inactive — check `main.py` route registrations before assuming an endpoint exists.
* The CORS middleware allows any `localhost` port via regex in addition to the explicit origins list. This is intentional for dev flexibility.
* `run_store.py` and `db.py` both deal with runs but have different scopes: `run_store.py` handles start/read of run records, `db.py` handles broader KV and table access. Do not consolidate them without checking all callers.
