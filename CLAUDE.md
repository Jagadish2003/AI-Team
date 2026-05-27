# AgentIQ Claude Instructions

## Purpose

AgentIQ is a full-stack discovery and opportunity-analysis app. The backend ingests enterprise system signals, detects workflow/friction patterns, scores opportunities, materializes run-scoped artifacts, and serves contract-shaped API responses. The frontend is a Vite React app that guides users through integration setup, discovery runs, source intelligence, opportunity review, pilot roadmap, agent blueprint, and executive report views.

Keep this file short and actionable. Prefer reading the relevant code and contract files over relying on this file for every detail.

## First Rules

* Protect user work. Check `git status --short --branch` before edits and do not revert unrelated changes.
* Do not commit secrets, generated databases, local env files, token files, or server keys.
* Preserve API response shapes. `contracts/API\\\_CONTRACT.md` and `contracts/CONTRACT\\\_RULES.md` are the source of truth when frontend and backend disagree.
* Run-scoped endpoints must include `runId` in the URL. Do not add "latest run" fallbacks.
* Before editing shared behavior, inspect existing route/context/test patterns and make the smallest compatible change.
* After code changes, run the narrowest relevant tests first, then broader tests when the change affects contracts, shared API helpers, routing, or pipeline behavior.

## Tech Stack

* Backend: Python 3.11, FastAPI, Pydantic, SQLite JSON payload tables, pytest.
* Frontend: React 18, TypeScript, Vite, React Router, Tailwind, Vitest, Testing Library.
* Data/pipeline: offline fixtures by default, optional live Salesforce/ServiceNow/Jira/nCino ingestion, pack-aware detectors and LLM enrichment.
* Auth: bearer token via `DEV\\\_JWT`; default local token is `dev-token-change-me`.

## Repository Map

* `backend/app/main.py`: FastAPI app, CORS, core API routes, route registration.
* `backend/app/routes\\\_\\\*.py`: feature route modules for stack builder, run lifecycle, replay, normalization, enrichment, blueprint, workspace catalog, and connector/product APIs.
* `backend/app/db.py`: SQLite access, run records, run events, run-scoped KV helpers.
* `backend/app/materialize\\\_t2.py`: run materialization, status, audit, events, roadmap/report persistence.
* `backend/discovery/`: ingest, detectors, scoring, evidence building, pack config, runner CLI.
* `backend/database/seed\\\_loader.py`: creates and seeds `backend/database/dev.db` from `backend/database/seed/`.
* `backend/tests/contract/`: backend contract and API tests. Contract tests use a temp SQLite DB.
* `backend/discovery/tests/`: discovery/ingest/detector/runner tests.
* `frontend/src/lib/apiClient.ts`: shared frontend API helpers and auth header.
* `frontend/src/api/`: typed frontend API wrappers.
* `frontend/src/context/`: data-loading state providers.
* `frontend/src/pages/`: routed product screens.
* `frontend/src/components/`: reusable UI and page-specific components.
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
.\\\\.venv\\\\Scripts\\\\Activate.ps1
python -m pip install -r requirements.txt
python database\\\\seed\\\_loader.py
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
python -m pytest tests\\\\contract\\\\test\\\_workspace\\\_catalog.py
```

Discovery tests:

```powershell
cd backend
python -m pytest discovery\\\\tests
```

Frontend build and tests:

```powershell
cd frontend
npm run build
npx vitest run
npx vitest run src\\\\\\\_\\\_tests\\\_\\\_\\\\OpportunityReviewPage.test.tsx
```

Contract mapping helper:

```powershell
cd frontend
python ..\\\\scripts\\\\validate\\\_contract.py
```

Smoke scripts are Bash scripts under `scripts/` and `backend/scripts/`; run them from Git Bash or WSL.

## Runtime And Env Notes

* Backend `.env` and frontend `.env` are intentionally untracked.
* Backend database defaults to `backend/database/dev.db`. Override with `DB\\\_PATH` for tests or isolated runs.
* Seed data location defaults to `backend/database/seed`; override with `SEED\\\_DIR`.
* Local backend CORS defaults allow `localhost:5173` through `localhost:5176`.
* Frontend API base uses `VITE\\\_API\\\_BASE\\\_URL`, defaulting to `http://localhost:8000` in dev.
* Frontend auth uses `VITE\\\_DEV\\\_JWT`, defaulting to `dev-token-change-me`.
* Optional LLM features use `ANTHROPIC\\\_API\\\_KEY`; deterministic fallbacks should still work without it.
* Live ingestion env vars include `SF\\\_INSTANCE\\\_URL`, `SF\\\_ACCESS\\\_TOKEN`, `SERVICENOW\\\_URL`, `SERVICENOW\\\_TOKEN`, `SERVICENOW\\\_USER`, `SERVICENOW\\\_PASS`, `JIRA\\\_URL`, `JIRA\\\_TOKEN`, `JIRA\\\_USER`, `JIRA\\\_PROJECT\\\_KEY`, `NCINO\\\_INSTANCE\\\_URL`, and `NCINO\\\_ACCESS\\\_TOKEN`.
* Token-generation scripts also use `SF\\\_CLIENT\\\_ID`/`SF\\\_USER`, `NCINO\\\_CLIENT\\\_ID`/`NCINO\\\_USER`, `STRS\\\_CLIENT\\\_ID`/`STRS\\\_USER`, and `LS\\\_CLIENT\\\_ID`/`LS\\\_USER`.

## Architecture Notes

* The API protects most endpoints with `require\\\_auth`; tests usually pass `Authorization: Bearer dev-token-change-me`.
* SQLite stores most tables as `{ id, payload }` JSON rows, plus `runs`, `run\\\_events`, and run-scoped KV entries.
* Run lifecycle starts at `POST /api/runs/start`, then materialization writes status, events, opportunities, evidence, clusters, roadmap, executive report, and enrichment into run-scoped storage.
* Replay should re-serve persisted artifacts only. It must not call live ingestion or regenerate LLM output.
* LLM enrichment is advisory post-processing. It must not mutate scoring fields such as impact, effort, tier, decision, or evidence IDs.
* Pack selection is centralized in `backend/discovery/packs/pack\\\_config.py`. Current packs include `service\\\_cloud`, `ncino`, and `strs\\\_benefits`.
* nCino and STRS packs have compliance guardrails. Do not suggest automated credit or benefit decisions; keep humans responsible for final decisions.
* The frontend should fetch backend data through `frontend/src/lib/apiClient.ts` or typed wrappers in `frontend/src/api/`.
* Avoid direct frontend imports of backend seed data or fixture JSON unless a test explicitly owns that fixture.

## Contract Rules

* If changing a backend response consumed by the UI, inspect the matching `frontend/src/types/\\\*` type and `contracts/API\\\_CONTRACT.md`.
* Any `frontend/src/types/\\\*.ts` schema change requires a corresponding contract update and version bump.
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
* When changing user-visible flow behavior, update or add Vitest tests under `frontend/src/\\\_\\\_tests\\\_\\\_/`.

## Backend Conventions

* Register new route modules from `backend/app/main.py` following existing route-registration style.
* Use Pydantic response models for contract-sensitive routes where practical.
* Use `db.run\\\_kv\\\_get` and `db.run\\\_kv\\\_set` for run-scoped artifacts.
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
* The repo currently ignores expected server keys under provider-specific token directories. Treat any untracked `\\\*.key` file as sensitive.
* Generated `.db`, `.venv`, `node\\\_modules`, build output, and token JSON files should stay untracked.
* If a real credential file appears outside ignored paths, ask before moving it and suggest adding a precise `.gitignore` entry.

## Useful Prompts For This Repo

* "Trace this UI field from component to API response and contract before editing."
* "Update the backend to match the contract, then run the focused contract test."
* "Add a frontend test for this state transition and run only the affected Vitest file."
* "Check whether this should be pack-specific in `pack\\\_config.py` instead of hardcoded."
* "Verify offline mode still works without live credentials."



