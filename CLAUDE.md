# CLAUDE.md

This file is the Claude Code guidance document for the AgentIQ repository. Claude Code reads it automatically at the start of a session. Keep it accurate and in sync with the code — when the codebase changes in a way that affects these instructions, update this file in the same PR.

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

### Backend — App

* `backend/app/main.py`: FastAPI app, CORS, core API routes, route registration. Check here first to confirm any route module is actually registered.
* `backend/app/routes_*.py`: feature route modules. Registered modules include stack builder, stack builder launch, run lifecycle, replay, normalization, enrichment, blueprint, workspace catalog, connector/product APIs, temporal, DB connectors, and connector auth.
* `backend/app/db.py`: SQLite access, run records, run events, run-scoped KV helpers. Broad table access.
* `backend/app/run_store.py`: run read/start helpers. Handles start and retrieval of run records only — do not consolidate with `db.py` without checking all callers.
* `backend/app/materialize_t2.py`: run materialization, status, audit, events, roadmap/report persistence.
* `backend/app/materialize_t3_hook.py`: T3 hook materialization — a separate step from `materialize_t2.py`; do not conflate.
* `backend/app/llm_enrichment.py`: LLM enrichment post-processing (advisory only, must not mutate scoring fields).
* `backend/app/entity_extractor.py`: Stage 2 knowledge graph orchestration. Non-blocking — extraction failures are logged and never break the run.
* `backend/app/entity_resolution.py`: conservative entity resolution engine. Uses an N+1 lookup pattern for ambiguous rows; only confident matches are merged.
* `backend/app/routes_entities.py`: `GET /api/runs/{runId}/entities` — analyst+ only. Owns `ensure_entities_table()` (startup-only schema creation).
* `backend/app/graph_context_builder.py`: ENT-4 graph context builder — turns raw graph traversal rows into a ranked, capped `GraphContext` for LLM prompts. Hard caps (15 entities / 20 relationships) and deterministic ranking are not configurable per-run. Distinct from `graph_context.py` (the ENT-3 run-KV enrichment bridge) — do not conflate.
* `backend/app/graph_constants.py`: single source of truth for the graph-context hard caps (`GRAPH_CONTEXT_MAX_ENTITIES=15`, `GRAPH_CONTEXT_MAX_RELATIONSHIPS=20`, `SPARSE_GRAPH_THRESHOLD=3`). Both `graph_context_builder.py` and `graph_context.py` import these — do not redefine the caps locally.
* `backend/app/routes_graph.py`: ENT-4 graph API routes — **registered** in `main.py` via `register_graph_routes(app)` (unlike `routes_sprint4_t5.py`). Four GET endpoints under `/api/graph`, all analyst+ and org-scoped via `get_current_org_id()` (request-body `org_id` is never trusted): `/opportunity/{opp_id}/neighbourhood`, `/entity/{entity_id}/neighbourhood` (both accept an optional `limit` query param for display-sized payloads), `/path`, and `/org/summary`.
* `backend/app/executive_report_engine.py`: executive report generation.
* `backend/app/roadmap_engine.py`: roadmap build logic.
* `backend/app/cross_system_linker.py`: cross-system signal linking across Salesforce/ServiceNow/Jira.
* `backend/app/opportunity_display.py`: opportunity formatting and display utilities.
* `backend/app/trackb_runner.py`: Track B offline background runner.
* `backend/app/temporal.py` + `routes_temporal.py`: Temporal workflow orchestration definitions and routes.
* `backend/app/models_t2.py` / `models_clusters.py`: T2-specific and cluster Pydantic models.
* `backend/app/telemetry.py`: telemetry and analytics event tracking. Do not log sensitive field values (tokens, PII).
* `backend/app/rbac.py`: role-based access control helpers and role enforcement. Dev user is seeded as owner via `seed_owner` on startup.
* `backend/app/security.py`: auth/security helpers, bearer token validation.
* `backend/app/connector_health.py` / `connector_metrics.py`: connector health status and metrics.
* `backend/app/jobs/connector_health.py`: background connector health check job — starts on app startup, shuts down on SIGTERM. Do not block startup waiting for connectors.
* `backend/app/jobs/baseline_calculator.py`: background baseline metrics job — interval and window controlled by `BASELINE_*` env vars.
* `backend/app/jobs/token_refresher.py`: background OAuth token-refresher job — proactively renews vault tokens that are due to expire (via `vault.get_token(..., min_validity_seconds=...)`) so connected sources stay live without re-running the OAuth flow. Interval/lookahead via `TOKEN_REFRESH_*` env vars; gated by `AGENTIQ_DISABLE_BACKGROUND_JOBS`.
* `backend/app/middleware/tenancy.py`: multi-tenancy middleware, org scoping per request. Default local org is `default`.
* `backend/app/middleware/audit.py`: audit trail middleware — runs alongside tenancy middleware.
* `backend/app/auth/oauth.py`: OAuth `authorization_code` and `client_credentials` flows.
* `backend/app/auth/vault.py`: Fernet-encrypted token vault. Requires `CREDENTIAL_VAULT_KEY` in production.
* `backend/app/auth/secrets.py`: secret resolution from env vars. Connector secret env vars follow the pattern `{CONNECTOR_NAME}_CLIENT_SECRET` (uppercase). New connectors must follow this convention.
* `backend/app/auth/models.py`: `ConnectorAuthConfig`, `TokenRecord` data models.
* `backend/app/auth/README.md`: auth framework documentation — read before adding a new connector.
* `backend/app/db_connectors/`: DB connector API models and route handlers.

### Backend — Discovery

* `backend/discovery/runner.py`: main discovery execution runner.
* `backend/discovery/scorer.py`: base scoring engine.
* `backend/discovery/evidence_builder.py`: evidence aggregation.
* `backend/discovery/models.py`: discovery data models.
* `backend/discovery/log.py`: discovery logging utilities.
* `backend/discovery/lending_scorer.py` / `strs_benefits_scorer.py`: pack-specific scorers for nCino and STRS packs.
* `backend/discovery/packs/github_engineering_scorer.py`: scorer for the `github_engineering` pack. Elevates PR-bottleneck confidence MEDIUM → HIGH when Jira corroborates.
* `backend/discovery/integration_verifier.py`: verifies integration signal completeness.
* `backend/discovery/track_a_adapter.py`: Track A ingestion adapter.
* `backend/discovery/offline_export.py`: offline fixture/data export utilities.
* `backend/discovery/ingest/live_validator.py`: live data validation at ingest time.
* `backend/discovery/ingest/strs_jira_corroboration.py` / `strs_sn_corroboration.py`: STRS cross-source corroboration against Jira and ServiceNow.
* `backend/discovery/calibration/calibrator.py` / `ranking.py`: confidence calibration and entity ranking.
* `backend/discovery/packs/pack_config.py`: centralized pack selection. Current packs: `service_cloud`, `ncino`, `strs_benefits`, `sqlserver_opsignal`, `github_engineering`.

### Backend — Database & Connectors

* `backend/database/connection.py`: SQLAlchemy connection setup — separate from `backend/app/db.py` (raw SQLite). Different layers; different models.
* `backend/database/models/`: SQLAlchemy ORM models — `audit_log.py`, `credentials.py`, `signal_snapshots.py`, `telemetry.py`, `workspace_members.py`.
* `backend/database/seed_loader.py`: creates and seeds `backend/database/dev.db` from `backend/database/seed/` (11 JSON seed files).
* `backend/connectors/db/`: native DB connector subsystem — Oracle, PostgreSQL, SQL Server drivers with connection pooling.
* `backend/connectors/db/oracle_ingestor.py`: Oracle operational-signal ingestor. Missing scope does NOT fall back to Oracle's sample `HR` schema — it returns degraded signals instead, so a misconfigured scope surfaces rather than silently querying sample data.
* `backend/connectors/db/postgresql_ingestor.py`: PostgreSQL operational-signal ingestor. Boolean predicates retry with an integer fallback when PostgreSQL raises a datatype-mismatch `pgcode` (`42804`/`42883`).
* `backend/connectors/db/query_guard.py`: SQL injection prevention — must be invoked for every native DB connector query. Skipping it bypasses injection protection. Uses sqlparse token traversal to strip CTE aliases (`WITH x AS (...)`) so scope checks resolve real base tables; fail-closed on ambiguous extraction.
* `backend/connectors/db/scope.py`: table/column scope management for DB connectors.
* `backend/connectors/saas/github.py`: GitHub SaaS connector — REST ingestion backing the `github_engineering` pack (PRs, commits, branches).

### Backend — Token Generation & Tests

* The `backend/token_generation/` server-key token-minting tooling has been **removed**. All connectors are now credentialed at runtime: Salesforce/Jira/ServiceNow via the Integration Hub OAuth flow (tokens in the credential vault, URLs captured at connect, sourced from the DB per org — see live ingest below); nCino and STRS run against the connected Salesforce org (the OAuth credentials in the per-run context) with optional `NCINO_*`/`STRS_*` env overrides.
* `backend/tests/contract/`: contract and API tests. Use a temp SQLite DB via `conftest.py`.
* `backend/tests/contract/fixtures/`: JSON fixtures for audit samples and connector health samples.
* `backend/tests/unit/`: unit tests for individual backend modules.
* `backend/discovery/tests/`: 15 discovery/ingest/detector/runner tests.
* `backend/connectors/db/tests/`: native DB connector tests including Oracle, PostgreSQL, and SQL Server smoke tests.

### Frontend

* `frontend/src/lib/apiClient.ts`: shared frontend API helpers and auth header.
* `frontend/src/api/`: typed frontend API wrappers — includes `blueprintApi.ts`, `enrichmentApi.ts`, `runScopedAuditApi.ts`, `runScopedS9S10Api.ts`.
* `frontend/src/context/`: data-loading state providers, including `ThemeContext.tsx` for dark mode/theming.
* `frontend/src/pages/`: all routed screens — including `BlueprintPage`, `DiscoveryFocusPage`, `DiscoveryPlanPage`, `PartialResultsPage`, `SourceIntelligencePage`, `SourceWeightingPage`, `YourSystemsPage`, and others.
* `frontend/src/components/`: reusable UI and page-specific components. Common components live in `frontend/src/components/common/`.
* `frontend/src/services/`: business logic layer, separate from raw API calls.
* `frontend/src/utils/`: shared utilities — `evidenceInterpreter.ts`, `nextBest.ts`, `sourceReadiness.ts`, `confidence.ts`, `apiErrors.ts`.
* `frontend/src/data/`: local static/fixture data files used by the frontend.
* `frontend/src/types/`: frontend schema references for backend response shapes.

### Contracts & Docs

* `contracts/API_CONTRACT.md`: API contract spec (v1.1, Contract Freeze). Source of truth.
* `contracts/mock_to_endpoint_map.json`: mapping of frontend mocks to backend endpoints.
* `contracts/CONTRACT_RULES.md`: contract rules and versioning policy.
* `docs/scoring_rubric.md`: opportunity scoring guidelines.
* `docs/detector_specs.md`: detector specifications.
* `docs/evidence_schema.md`: evidence field definitions.
* `docs/AUTH_SETUP.md`: auth configuration guide.
* `docs/proxy_metrics.md`: proxy metric definitions.
* `docs/SMOKE_DEMO_*.md` (5 files): smoke test walkthroughs.
* `docs/INTEGRATE_*.md` (4 files): integration guides per connector.
* `deployment/README.md`: production env var guide covering OAuth secrets, vault, and `CREDENTIAL_VAULT_KEY`.
* `scripts/`: shell smoke tests and contract helper. Bash — run from Git Bash or WSL.

## Setup Commands

Use PowerShell on Windows unless the user is already in Git Bash.

**Python version check:** Run `python --version` first. If the version is anything other than `3.11.9`, use `py -3.11` instead of `python` for venv creation and any command that must target 3.11 explicitly.

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

Reference `backend/.env.example` for the full list of env vars needed before first run.

## Verification Commands

Backend contract tests:

```powershell
cd backend
python -m pytest
python -m pytest tests\contract\test_workspace_catalog.py        # single file
python -m pytest tests\contract\test_entity_extraction.py::test_name   # single test by node id
python -m pytest -k "entity and not resolution"                  # -k name-pattern filter
```

Discovery tests:

```powershell
cd backend
python -m pytest discovery\tests
```

Native DB connector tests:

```powershell
cd backend
python -m pytest connectors\db\tests
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

## CI / CD

* `.github/workflows/contract-tests.yml` runs backend contract tests on every PR and push to `main`. All contract tests must pass before merging.

## Runtime And Env Notes

* Backend `.env` and frontend `.env` are intentionally untracked. Use `backend/.env.example` as the setup reference.
* Backend database defaults to `backend/database/dev.db`. Override with `DB_PATH` for tests or isolated runs.
* Seed data location defaults to `backend/database/seed`; override with `SEED_DIR`.
* Local backend CORS defaults allow `localhost:5173` through `localhost:5176`. Override the allowed origins list with `CORS_ORIGINS` (comma-separated) for non-default dev ports or staging.
* Frontend API base uses `VITE_API_BASE_URL`, defaulting to `http://localhost:8000` in dev.
* Frontend auth uses `VITE_DEV_JWT`, defaulting to `dev-token-change-me`.
* Optional LLM features use `ANTHROPIC_API_KEY`; deterministic fallbacks should still work without it.
* Set `REQUIRE_CONNECTOR_SECRETS=1` in production to enforce that all connector secrets are present on startup. Dev and test environments intentionally leave this unset.
* Salesforce/Jira/ServiceNow live ingestion is OAuth-only and DB-sourced: tokens come from the credential vault and instance/site URLs are captured at OAuth connect time, both keyed per org. `app/live_ingest_credentials.py` `resolve_live_systems(org_id)` reads them from the DB and publishes them to a per-run `contextvars` context (`discovery/ingest/__init__.py` `set_live_connectors`/`get_live_connector`) that each connector's `_get_client()` reads — never process-global `os.environ`, so concurrent multi-tenant runs cannot read each other's credentials. `SF_*`/`SERVICENOW_*`/`JIRA_*` env vars are only a CLI/standalone fallback (`JIRA_PROJECT_KEY` is still read for live Jira). The remaining `.env` live ingestion vars are `NCINO_INSTANCE_URL`, `NCINO_ACCESS_TOKEN`.
* nCino and STRS live ingestion reuse the connected Salesforce org's OAuth credentials from the per-run context, with optional `NCINO_INSTANCE_URL`/`NCINO_ACCESS_TOKEN` and `STRS_INSTANCE_URL`/`STRS_ACCESS_TOKEN` env overrides to target a different instance.
* OAuth client secrets (production, enforced by `REQUIRE_CONNECTOR_SECRETS`): `SALESFORCE_CLIENT_SECRET`, `SERVICENOW_CLIENT_SECRET`, `JIRA_CLIENT_SECRET`, `GITHUB_CLIENT_SECRET`, `SLACK_CLIENT_SECRET`, `SAP_CLIENT_SECRET`, `DYNAMIC365_CLIENT_SECRET`.
* `CREDENTIAL_VAULT_KEY`: Fernet key for token vault encryption. Required in production; missing it causes connector secret storage to fail or fall back to plaintext.
* `DEV_JWT_ROLE`: role override for the dev token (`owner`/`analyst`/`viewer`). Used alongside `ADMIN_JWT`, `ANALYST_JWT`, `VIEWER_JWT` in contract tests.
* `INGEST_MODE`: `online`/`offline`/`test` — controls the ingestion path.
* Model provider gateway (R16-D1 / R17-D1): `MODEL_GENERATION_PROVIDER` and `MODEL_EMBEDDING_PROVIDER` select which provider serves generation and embedding **independently** (valid values: `hosted` (default), `in_boundary`). Resolved at call time; unknown values raise at startup via `validate_provider_config()`.
* In-boundary provider config (R17-D1, used only when a provider above is set to `in_boundary`; all owned inside `backend/app/model_gateway/` — no caller reads these): `IN_BOUNDARY_BASE_URL` (common OpenAI-compatible base; the adapter derives `/v1/chat/completions` and `/v1/embeddings`), `IN_BOUNDARY_GENERATION_ENDPOINT` / `IN_BOUNDARY_EMBEDDING_ENDPOINT` (override either path independently), `IN_BOUNDARY_API_KEY` (bearer token, resolved live per call — never logged), and the model names `IN_BOUNDARY_MODEL` (common fallback) / `IN_BOUNDARY_GENERATION_MODEL` / `IN_BOUNDARY_EMBEDDING_MODEL`. Leave all blank unless in-boundary mode is in use. If `in_boundary` is selected with no endpoint URL configured, `validate_provider_config()` logs a startup warning (calls never raise — they degrade to `ok=False`/`[]`).
* `REPLAY_RESETS_DECISIONS`: set to `1` to clear analyst overrides on replay. Default is off.
* `TRACKB_PYTHON` / `TRACKB_RUNNER_MODE`: Python path and mode (`offline`/`live`) for the Track B subprocess runner.
* `BASELINE_JOB_INTERVAL_HOURS` / `BASELINE_MIN_RUNS` / `BASELINE_WINDOW_DAYS`: control the background baseline calculator job.
* `TOKEN_REFRESH_JOB_INTERVAL_MINUTES` (default `10`) / `TOKEN_REFRESH_AHEAD_SECONDS` (default `900`): control the proactive OAuth token-refresher job. The job renews any vault token expiring within the lookahead window; keep the interval comfortably below the lookahead so a token is always refreshed before it lapses.
* Oracle native DB connector env vars: `ORACLE_HOST`, `ORACLE_PORT` (default `1521`), `ORACLE_DATABASE` (service name, default `ORCL`), `ORACLE_DB_USERNAME`, `ORACLE_DB_PASSWORD`. These diverge from the `{CONNECTOR_NAME}_CLIENT_SECRET` convention because native DB connectors authenticate with a database username/password pair (resolved via `username_key`/`password_key`), not an OAuth client secret.
* PostgreSQL native DB connector env vars: `POSTGRESQL_HOST`, `POSTGRESQL_PORT` (default `5432`), `POSTGRESQL_DATABASE` (default `postgres`), `POSTGRESQL_USERNAME`, `POSTGRESQL_PASSWORD`, and `POSTGRESQL_SSL_MODE` (`require`/`prefer`/`disable`). Same divergence rationale as Oracle above.
* `AGENTIQ_DISABLE_BACKGROUND_JOBS`: set to `1` to skip starting background jobs (connector health, baseline calculator, OAuth token refresher) on app startup. Useful for tests and isolated runs.

## Architecture Notes

* The API protects most endpoints with `require_auth`; tests usually pass `Authorization: Bearer dev-token-change-me`.
* SQLite stores most tables as `{ id, payload }` JSON rows, plus `runs`, `run_events`, and run-scoped KV entries. `backend/app/db.py` handles this layer.
* `backend/database/connection.py` is a separate SQLAlchemy layer used by ORM models. Do not conflate with `db.py`.
* Run lifecycle starts at `POST /api/runs/start`, then materialization writes status, events, opportunities, evidence, clusters, roadmap, executive report, and enrichment into run-scoped storage.
* Replay should re-serve persisted artifacts only. It must not call live ingestion or regenerate LLM output.
* LLM enrichment is advisory post-processing. It must not mutate scoring fields such as impact, effort, tier, decision, or evidence IDs.
* `OppEnrichment.relationships` is intentionally different from the other enrichment fields: it is read live from `entity_relationships` through `graph_query.py`, not from a run-scoped KV artifact. The graph is cross-run state, so later relationship upserts can change what a historical run's relationship view returns.
* `OppEnrichment` also carries ENT-2 cross-system corroboration fields (`corroboration_sources`, `corroboration_label`, `triple_corroboration`, `corroboration_rule_ids`), populated from the stored opportunity record. See `backend/app/corroboration_engine.py` and `backend/discovery/packs/corroboration_rules.py`.
* Pack selection is centralized in `backend/discovery/packs/pack_config.py`. Current packs: `service_cloud`, `ncino`, `strs_benefits`, `sqlserver_opsignal`, `github_engineering`.
* **Pack invariants — pack versioning (R16-B1 §4):** every pack declares a `packVersion` in `PACK_REGISTRY` (resolved via `get_pack_version()`), and it is stamped onto every opportunity instance so pack governance (1.9) can later tell a *data* change from a *pack logic* change. This signal is only useful if the version actually moves: **bump a pack's `packVersion` in `pack_config.py` whenever you change that pack's detector, scorer, or corroboration-rule logic** — treat it as part of the PR checklist for any pack change. Use semantic bumps (patch for tweaks, minor/major for behaviour changes). Two runs stamped the same version must be reproducible from the same data.
* nCino, STRS, and `github_engineering` packs have compliance guardrails. Do not suggest automated credit/benefit decisions or automated merge approvals, branch deletions, or code changes; keep humans responsible for final decisions.
* Multi-tenancy is enforced via `middleware/tenancy.py`. Every request is scoped to an org; the default local org is `default`.
* RBAC is enforced via `rbac.py`. The dev user is seeded as owner of the default org on startup via `seed_owner`. Use `require_role` to gate privileged routes.
* Audit trail is enforced via `middleware/audit.py`. All mutating requests are logged automatically.
* A background connector health check job starts on app startup (`jobs/connector_health.py`) and shuts down on SIGTERM. Do not block startup waiting for connectors.
* A background baseline calculator job (`jobs/baseline_calculator.py`) recalculates scoring baselines on a configurable interval.
* A background OAuth token-refresher job (`jobs/token_refresher.py`) proactively renews vault tokens before they expire so connected sources stay live without the user re-running the OAuth flow. It only refreshes rows that hold a refresh token and have not previously failed; a genuine failure is left as `refresh_failed` for the user to reconnect.
* Telemetry events are tracked via `telemetry.py`. Do not log sensitive field values (tokens, PII) in telemetry events.
* Telemetry has a locked write signature: `record_event(event_type, payload)`. The `event_type` must be in `REGISTERED_EVENT_TYPES` — `record_event()` raises `ValueError` for an unregistered type. Register new event types (and their payload schema) before emitting them.
* Entity extraction (`entity_extractor.py`) is a non-blocking Stage 2 step — failures are logged and never break the run. Resolution (`entity_resolution.py`) is conservative and uses an N+1 lookup for ambiguous rows. Never write `canonical_name` from caller-supplied values; it is normalized internally. Display filtering uses `ENTITY_MIN_RUN_COUNT`, imported from `database/models/entities.py` — import it, do not redefine the threshold locally.
* `_verify_db_driver_imports()` runs at app startup to surface Oracle/PostgreSQL driver install problems early; it logs availability and does not block startup.
* The native DB connector subsystem (`backend/connectors/db/`) supports Oracle, PostgreSQL, and SQL Server. All queries must go through `query_guard.py` for injection prevention.
* The token vault (`auth/vault.py`) uses Fernet symmetric encryption. Set `CREDENTIAL_VAULT_KEY` before storing any connector credentials in production.
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
* Contract changes require a version bump (e.g. v1.0 → v1.1) and PR sign-off by both the FE and BE leads, per the "Contract PR sign-off" section of `contracts/CONTRACT_RULES.md`.

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
* When adding a new connector, follow the secret naming convention in `backend/app/auth/README.md` (`{CONNECTOR_NAME}_CLIENT_SECRET`) so that `secrets.py` resolution works automatically.

## Testing Guidance

* For backend API work, prefer focused contract tests in `backend/tests/contract/`.
* For discovery logic, test detectors/scorers/ingest in `backend/discovery/tests/` or the relevant `backend/tests/unit/` file.
* Contract tests seed an isolated temp DB through `backend/tests/contract/conftest.py`; do not make tests depend on local `dev.db`.
* For frontend API/state changes, mock API boundaries rather than reaching into backend files.
* For changes affecting the full run pipeline, verify start/status/artifact endpoints together.
* For native DB connector changes, run `backend/connectors/db/tests/` including the smoke tests for each driver.
* The CI gate (`.github/workflows/contract-tests.yml`) must pass on all PRs. Run `python -m pytest` locally before pushing.

## Database Migrations

Use `backend/migrations/` for all migration work. `backend/alembic.ini` points to this directory, and contract tests run these migrations against an isolated temporary DB.

* `backend/migrations/versions/`: active migration scripts (`0001_create_telemetry_events.py`, `0002_create_signal_snapshots.py`, `0003_create_entities.py`).
* The `0003_create_entities.py` migration imports `ALL_ENTITIES_DDL` from `database/models/entities.py` rather than hardcoding DDL. The same `ALL_ENTITIES_DDL` is executed by the runtime `ensure_entities_table()` helper, so the migration-applied schema and the runtime-created schema can never drift apart.
Apply pending migrations:

```powershell
cd backend
alembic upgrade head
```

## Security And Data Hygiene

* Never print full tokens, private keys, or `.env` contents.
* Connector credentials live in the database (Fernet-encrypted in the `credentials` vault table); there are no provider key files to manage. Treat any stray `*.key`/token JSON as sensitive and never commit it.
* `CREDENTIAL_VAULT_KEY` must never be committed. Add it only to production secrets management.
* Generated `.db`, `.venv`, `node_modules`, build output, and token JSON files should stay untracked.
* If a real credential file appears outside ignored paths, ask before moving it and suggest adding a precise `.gitignore` entry.

## Known Gotchas

* `routes_sprint4_t5.py` exists in `backend/app/` but is **not registered** in `main.py`. Routes defined in it are silently inactive — always verify `main.py` registrations before assuming an endpoint exists.
* `PATCH_materialize_t2_t6.py` is a patch/migration file in `backend/app/`, not a regular module. Do not import it as a route or service.
* `routes_stack_builder.py` and `routes_stack_builder_launch.py` both exist with different responsibilities. Check `main.py` before editing either.
* `materialize_t3_hook.py` is a separate T3 materialization step, not an extension of `materialize_t2.py`. Do not merge them.
* `run_store.py` and `db.py` both deal with runs but have different scopes: `run_store.py` handles start/read of run records; `db.py` handles broader KV and table access. Do not consolidate without checking all callers.
* `backend/database/connection.py` (SQLAlchemy ORM) and `backend/app/db.py` (raw SQLite) are different layers. Mixing them will cause session/connection conflicts.
* Two migration directories (`alembic/` and `migrations/`) serve overlapping purposes. Use `migrations/` for all new work — `alembic.ini` already points there.
* The CORS middleware allows any `localhost` port via regex in addition to the explicit origins list. This is intentional for dev flexibility.
* `backend/connectors/db/query_guard.py` must be invoked for every native DB query. Skipping it bypasses SQL injection protection silently.
* `auth/vault.py` requires `CREDENTIAL_VAULT_KEY` to be set in production. A missing key causes connector secret storage to fail or silently fall back to plaintext.
* `get_pack()` falls back to `service_cloud` for an unknown pack_id (it logs a WARNING rather than raising). A typo'd pack id therefore yields Service Cloud detectors instead of an error — check the log line if a run produces unexpected detectors.
* `record_event()` raises `ValueError` for an unregistered `event_type` — this is a deliberate change from the earlier silent-drop behavior. A new event type must be added to `REGISTERED_EVENT_TYPES` before it is emitted, or the call raises.
* `ensure_entities_table()` is startup-only — it creates the entities table once when `routes_entities` is registered. Do not call it per-request.

## Useful Prompts For This Repo

* "Trace this UI field from component to API response and contract before editing."
* "Update the backend to match the contract, then run the focused contract test."
* "Add a frontend test for this state transition and run only the affected Vitest file."
* "Check whether this should be pack-specific in `pack_config.py` instead of hardcoded."
* "Verify offline mode still works without live credentials."
* "Check `main.py` route registrations before assuming an endpoint from a `routes_*.py` file is active."
* "Read `backend/app/auth/README.md` before adding a new connector or OAuth flow."
