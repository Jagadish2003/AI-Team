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

* Backend: Python 3.11, FastAPI, Pydantic, PostgreSQL (psycopg2) with `{ id, payload }` JSON payload tables, pytest.
* Frontend: React 18, TypeScript, Vite, React Router, Tailwind, Vitest, Testing Library.
* Data/pipeline: offline fixtures by default, optional live Salesforce/ServiceNow/Jira/nCino ingestion, pack-aware detectors and LLM enrichment.
* Auth: bearer token via `DEV_JWT`; default local token is `dev-token-change-me`.

## Repository Map

### Backend — App

* `backend/app/main.py`: FastAPI app, CORS, core API routes, route registration. Check here first to confirm any route module is actually registered.
* `backend/app/routes_*.py`: feature route modules. Registered modules include stack builder, stack builder launch, run lifecycle, replay, normalization, enrichment, blueprint, workspace catalog, connector/product APIs, temporal, DB connectors, and connector auth.
* `backend/app/db.py`: PostgreSQL (psycopg2) access, run records, run events, run-scoped KV helpers. Broad table access. Connections come from a process-wide pool via `connect()` — the returned object is a `_PooledConnection` proxy whose `.close()` returns the connection to the pool instead of closing the socket, so `closing(connect())` / `conn.close()` recycle. Pool size is tuned by `DB_POOL_MIN` (default 1) / `DB_POOL_MAX` (default 16).
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
* `backend/app/retrieval/`: R18-B1 retrieval substrate — `chunking.py` (per-content-type chunk policies + content hash), `embedder.py` (batch embedding via the model gateway ONLY; also owns the R18-B2 T5 model-version backfill `backfill_stale_model_for_org`/`_all_orgs` that re-embeds non-active-model vectors onto the active model — AC5), `store.py` (pgvector index, hard-partitioned by org_id; the per-vector `(embedding_model, embedding_model_version)` stamp is the compatibility marker — `search()` filters on the active pair so old-model vectors are never compared/mixed, and `fetch_stale_model_embedded`/`orgs_with_stale_model`/`count_stale_model` drive the backfill), `api.py` (`retrieve()` + `RetrievedChunk`), `ingest.py` (T5 producer contract), `evidence_source.py` (T6 — `retrieval_evidence_source(org_id)` builds the `evidence_source` callable for `assemble_context`; retrieval PROPOSES chunks, the assembler DECIDES under its floor/ordering/cap/selection-log rules; it deliberately does NOT pre-filter by the policy confidence floor so below-floor exclusions stay on the selection log, and for the same reason it PROPOSES stale chunks (`retrieve(include_stale=True)`) so the assembler excludes them as `excluded: stale` on the selection log rather than the exclusion vanishing silently (R18-B2 T4 / AC6); retrieval must never feed `llm_enrichment` directly — a structural test pins this). ALL content producers (document ingestion, Git content, Slack/Teams, Confluence/SharePoint) feed the substrate through ONE entry point: `ingest.ingest_content(org_id, artifacts)` — producers hand extracted text + provenance (`ContentArtifact`) and NOTHING else; chunk sizing, hashing, embedding, indexing, and metadata storage happen inside the substrate. Producer dicts carrying substrate-owned fields (`embedding`, `chunk_size`, …) are rejected; re-handing an artifact replaces its previous chunks; ingestion never embeds synchronously (the async worker below does) and never logs artifact content. Do not add a second vector-writing path for a new content source — extend the producer's extraction and call `ingest_content`.
* `backend/app/jobs/embedding_worker.py`: R18-B1 T3 background retrieval-embedding worker — drains the pending (`embedding IS NULL`) `retrieval_chunks` backlog for every org through `retrieval.embedder.embed_pending_all_orgs()`, off the discovery-run path, so embedding lag never blocks a run (AC7). Embeds in bounded batches via the model gateway ONLY and stamps each vector with the active model's identity+version. Each tick then runs the R18-B2 T5 managed model-version backfill (`retrieval.embedder.backfill_stale_model_all_orgs()`) — re-embedding vectors stamped by a NON-active model onto the active model after a provider/version repin, bounded per org so a migration is a gentle background convergence (the two passes read disjoint sets: `embedding IS NULL` vs embedded-but-old-model). Interval/caps via `RETRIEVAL_EMBED_*` / `RETRIEVAL_BACKFILL_MAX_CHUNKS_PER_ORG` env vars; gated by `AGENTIQ_DISABLE_BACKGROUND_JOBS`.
* `backend/app/retrieval/freshness.py` / `refresh_queue.py` / `refresh.py`: R18-B2 retrieval freshness. `freshness.py` (T1/T2) is the `ingestion.artifact_changed` subscriber — `updated`/`created` marks the artifact's chunks stale + enqueues a refresh; `deleted` calls the atomic `store.purge_artifact` immediately (chunks + queue row dropped together, before any queue, via the public `remove_artifact`). `refresh_queue.py` is the durable, org-scoped refresh work list (enqueue upserts so repeat events for one artifact collapse into one pending row). `refresh.py` (T3) is the async refresh worker: for each queued artifact it re-extracts via a producer-registered **content resolver** (`register_content_resolver(source_system, resolver)` — the substrate is push-based and never pulls from connectors itself), re-chunks through the SAME `ingest.build_records` path, hash-compares against stored chunks, re-embeds ONLY changed/new chunks via the model gateway (unchanged chunks carry their stored vector over — AC3), then `store.swap_artifact_chunks` replaces the whole set in ONE transaction (AC4) with `is_stale` cleared as part of that commit. Never re-embeds unchanged content; never raises into a run. T4 stale-in-retrieval handling: `retrieve()`/`store.search` take `include_stale` (default `False`) — stale chunks are excluded from default retrieval at the SQL layer (`is_stale = FALSE`, served by `idx_retrieval_chunks_org_stale`) and each `RetrievedChunk` carries an `is_stale` flag; the context assembler's stale exclusion (Rule 0, ahead of the confidence floor) is gated by `AssemblyPolicy.include_stale` and logged as `excluded: stale` (`REASON_STALE`) so freshness exclusions are visible on the selection log, not silent (AC6).
* `backend/app/retrieval/metrics.py` + `backend/app/routes_retrieval.py`: R18-B2 T6 freshness metrics (AC7 — "Lag is visible"). `metrics.freshness_metrics(org_id)` computes the org's lag picture from the SAME org-scoped primitives the workers run on: `pending_change_events` / `failed_refreshes` (refresh queue), `stale_chunks`, `pending_embeddings`, and `backfill` progress (embedded-total vs on-active-model vs awaiting-backfill, with a 0–1 `progress` ratio; an empty partition reads converged, a degraded gateway reads 0.0 — never a false complete). Served by `GET /api/retrieval/freshness` (**registered** in `main.py` via `register_retrieval_routes(app)`), analyst+ and org-scoped via `get_current_org_id()` only — the future run-health dashboard's data source. Deliberate posture: metrics NEVER degrade to zeros on a read failure (zeros would report perfect freshness while the store is down) — errors raise and surface as HTTP errors.
* `backend/app/jobs/refresh_worker.py`: R18-B2 T3 background retrieval-refresh worker — drains a bounded page of the `retrieval_refresh_queue` for every org through `retrieval.refresh.refresh_pending_all_orgs()`, off the discovery-run path, so re-embedding never blocks a run. Interval/caps via `RETRIEVAL_REFRESH_*` env vars; gated by `AGENTIQ_DISABLE_BACKGROUND_JOBS`.
* `backend/app/middleware/tenancy.py`: multi-tenancy middleware, org scoping per request. Default local org is `default`.
* `backend/app/middleware/audit.py`: audit trail middleware — runs alongside tenancy middleware.
* `backend/app/auth/oauth.py`: OAuth `authorization_code` and `client_credentials` flows.
* `backend/app/auth/vault.py`: Fernet-encrypted token vault. Requires `CREDENTIAL_VAULT_KEY` in production. Holds two record types on the shared `credentials` table, discriminated by the `kind` column with the same per-(org_id, connector_id) keying and encryption: OAuth token records (`kind='oauth'`, auto-refreshed) and static credentials (`kind='static'` — R17-D3 Addendum A T10: Jira API token, ServiceNow user/password, native DB connection credentials; `store/get/revoke_static_credential`, encrypted `enc_username`/`enc_secret` + non-secret `base_url`, no refresh/expiry). One credential per connector per org across both kinds — storing one kind replaces the other; `get_token()` never returns a static row and `get_static_credential()` never returns an OAuth row. R18-A3 T2 (AT-555) adds the Salesforce **JWT bearer** flow: the cert private key + SF username + login host are vaulted as a `static` record under the reserved id `{connector_id}:jwt` (`store/get/revoke_jwt_bearer_credential`); `get_token()` mints the access token outbound from it (RFC 7523 signed assertion, no callback) and caches it as the normal OAuth row, re-minting by re-assertion on expiry — so ingestion stays mode-agnostic.
* `backend/app/auth/secrets.py`: secret resolution from env vars. Connector secret env vars follow the pattern `{CONNECTOR_NAME}_CLIENT_SECRET` (uppercase). New connectors must follow this convention.
* `backend/app/auth/models.py`: `ConnectorAuthConfig`, `TokenRecord`, `StaticCredentialRecord` data models, plus the `AuthMode` type (R18-A3 T1). `StaticCredentialRecord` masks `username`/`secret` in `repr` — never log the raw values. `ConnectorAuthConfig.supported_auth_modes` declares which auth modes a connector supports (most-preferred first = default).
* `backend/app/auth/auth_modes.py`: R18-A3 T1 (AT-554) connector auth-mode abstraction. Recognises four modes (`authorization_code`, `client_credentials`, `jwt_bearer`, `static`); a per-connector registry (`get_supported_auth_modes`/`get_default_auth_mode`/`connector_supports_mode`) and a per-org selection resolver (`set_auth_mode`/`resolve_auth_mode`, persisted on the org's connector record). Every mode terminates in the same vault record shape, so ingestion stays mode-agnostic via the unchanged `get_connector_credentials()` — the mode concept never leaves the connect/setup edge (AC3). Salesforce now registers `jwt_bearer` (AT-555, built); AT-556/AT-557 (`client_credentials` for Graph/ServiceNow) register their mode here when built.
* `backend/app/auth/README.md`: auth framework documentation — read before adding a new connector.
* `backend/app/db_connectors/`: DB connector API models and route handlers.

### Backend — Discovery

* `backend/discovery/runner.py`: main discovery execution runner.
* `backend/discovery/scorer.py`: base scoring engine.
* `backend/discovery/evidence_builder.py`: evidence aggregation.
* `backend/discovery/models.py`: discovery data models.
* `backend/discovery/signals/operational_event.py`: MSP-B0 Operational Event Schema — the ONE normalised shape every MSP cloud-event source (AWS/B1, Azure/B2, Event History Bridge/B8) maps its provider-native payload onto, so detectors never see a provider-specific event. A *profile* of the common signal model: `CommonSignal` (org-scoped spine reusing the R16-B1 `EvidencePointer` provenance) + `OperationalEvent` (adds the closed vocabularies `RESOURCE_TYPES`/`EVENT_CLASSES`/`SEVERITY_LEVELS` — out-of-vocab fails at construction — plus `ResourceRef` and a provider-native `event_type`). `normalize_*` helpers + `OperationalEvent.build()` are the provider→canonical entry point. Auto-derives `event_signature` (see below). Documented in `docs/msp_operational_event_schema.md`.
* `backend/discovery/signals/event_signature.py`: MSP-B0/AT-636 deterministic `event_signature` construction — a stable recurrence fingerprint (`"{VERSION}:{sha256_128bit}"`) for dedup / recurrence detection / hotspot correlation. Rules are *per provider family* (`aws`/`azure`/`event_bridge`/`generic`, resolved from `source_system`; governs `event_type` normalisation) and *per event class* (`_CLASS_RECIPE`; access/audit/security additionally key on `principal`). Deliberately excludes timestamp, `signal_id`, `severity`, and free-form payload so repeated occurrences collapse. Bump `EVENT_SIGNATURE_VERSION` when the recipe/normalisation changes.
* `backend/discovery/detectors/runbook_match.py` + `runbook_composite.py`: MSP-B5 runbook matching and B6 composite lifecycle. Explicit citations are `observed`; retrieval candidates are `proposed`; only analyst acceptance produces `confirmed`. `absent` and `unavailable` remain distinct. Presentation labels come from `runbook_composite.presentation_for_state()` so findings, executive reports, and demo views never upgrade a proposal silently. Analyst decisions persist through `app/runbook_match_decisions.py` and the protected `routes_runbook_matches.py` API with append-only history and labelled accept/dismiss feedback.
* `backend/discovery/signals/reference_mappers.py`: MSP-B0/AT-637 provider mapping contract + reference mappers converting raw provider payloads → `OperationalEvent`: `map_cloudwatch`/`map_eventbridge`/`map_cloudtrail` (AWS) and `map_azure_monitor`/`map_azure_activity_log` (Azure), plus the `MAPPERS` registry and the `aws_resource_type_from_arn`/`azure_resource_type_from_id` derivations. All terminate in `OperationalEvent.build()` so every provider emits the identical detector-visible structure. Mappers carry only curated normalised scalars on `payload` — never the raw provider blob (that lives in the evidence store, AT-638). Fixtures, not live connections (live is B1/B2/B8); behaviour pinned by `discovery/tests/fixtures/msp_provider_mapping_golden.json`. Mapping contract documented in `docs/msp_operational_event_schema.md` §5.
* `backend/discovery/signals/evidence_store.py`: MSP-B0/AT-638 raw-payload storage + evidence-pointer resolution. `RawEventStore` (ABC) + `InMemoryRawEventStore` (default; deep-copies) persist raw provider payloads keyed by `(org_id, source_system, source_artifact)` — the exact tuple the event's OBSERVED `EvidencePointer` carries, so `resolve_raw_event()` walks a normalised event back to its raw payload. Hard org-partitioned: cross-org store/resolve raises `OrgScopeError`. `map_and_store()` maps + persists in one step. The detector-visible `OperationalEvent` never embeds the raw payload (T4-AC4) — raw JSON lives ONLY in the store. In-memory default here; DB-backed store drops in for live ingestion (B1/B2/B8). Documented in `docs/msp_operational_event_schema.md` §6.
* `backend/discovery/signals/ops_stream.py`: MSP-B7/AT-669 (T1) dedup at admission — the FIRST of MSP-B7's five event-volume disciplines. `OpsEventStream.admit(event)` folds re-firing `OperationalEvent`s into ONE `ActiveSignal` per `(org_id, event_signature, resource, active period)`, maintaining an occurrence count (over DISTINCT provider event ids — a redelivery of an already-counted firing is idempotent) and a first/last-seen span; a stuck alarm firing every 5 min is one fact with count 288/day, not 288 facts. The *active period* is an epoch-anchored time bucket (`DEFAULT_ACTIVE_PERIOD_SECONDS`, default one day; tunable per stream — T6 calibrates it). Deterministic (folding depends only on event fields, never arrival order: representative = earliest firing by `(observed_at, signal_id)`, count/span order-independent) and org-scoped (fold key includes `org_id`; cross-org admit/resolve raises `OrgScopeError`). The raw payloads are NEVER embedded — `ActiveSignal.resolve_raw_instances(store)` walks each folded firing's evidence pointer back to the raw-event store (AT-638) so an aggregate opens to its real instances (aggregation compresses volume, never evidence). Also HOSTS T4 (per-run budget, AT-672): the constructor takes `budget=N` and `admit` defers-and-counts events past the budgeted window via the injected `RunBudget` (see `budget.py`); `stream.budget_report()` returns the deferral proof. Documented in `docs/msp_operational_event_schema.md` §8.
* `backend/discovery/signals/budget.py`: MSP-B7/AT-672 (T4) per-run event-volume budgets — the FOURTH discipline (pipeline: dedup → floor → budget → aggregate), enforced INSIDE `OpsEventStream.admit` because a budget must stop the run *processing* everything (post-hoc filtering would already have paid the cost). `RunBudget(limit)` is the counter the stream consults: while `has_capacity()` an event is folded + `charge()`d; once exhausted every further event is `defer()`red-and-counted (`Admission.is_deferred`, `signal is None`) — LOUD, never silent truncation. Volume-based (re-fires count too) and arrival-ordered (the budgeted window is the first `limit` events; deliberately order-dependent unlike the other stages). `BudgetReport` (`budget`/`processed`/`deferred`/`seen`/`breached`/`deferred_by_source`/`deferred_window`/`reason`, JSON via `to_dict`) is the run-record + R18-C2 content-panel artifact. `limit=None` (default) = unbounded. This module never parses timestamps (the stream passes them pre-parsed → no dep on `ops_stream`, so `ops_stream` imports it cleanly). Documented in `docs/msp_operational_event_schema.md` §11.
* `backend/discovery/signals/ops_calibration.py`: MSP-B7/AT-674 (T6) event-volume calibration — the SINGLE evidence-based source of truth for the T3/T4/T5 volume defaults, derived from MSP-B8's measured month-scale run (`docs/MSP-B8_VOLUME_VALIDATION.md`, captured verbatim as `B8_MEASUREMENTS`). Derives: per-run event budget = `CALIBRATED_RUN_EVENT_BUDGET` = 250,000 (quantitative: `ceil(8 × 30,225-event month)` rounded up → ~6-min worst-case ingest at the measured 1.474 ms/event); noise floors `CALIBRATED_NOISE_FLOORS` (audit/state_change/access=5, else 1 — error/security never floored) and windows `CALIBRATED_CORRELATION_WINDOWS` (event↔event 15m kept tight vs measured ~42 events/hr, event↔incident 2h operational lag). `noise_floor.py`/`budget.py`/`correlation/windows.py` IMPORT these (no divergent hardcoded guesses). Honest about where evidence stops: budget is quantitatively derived, floors/incident-window are operationally-justified pending per-class-recurrence + incident-lag telemetry. `calibration_summary()` is the JSON audit surface. Recalibrate by editing `B8_MEASUREMENTS`. Documented in `docs/msp_operational_event_schema.md` §13.
* `backend/discovery/correlation/windows.py`: MSP-B7/AT-673 (T5) correlation-window service — the FIFTH discipline (Track B). A cross-stream join (`event_incident`, `event_event`) is valid ONLY within a configurable time window; `join_within_window(a, b, join_type, org_id=…)` returns a `WindowJoin` recording `(join_type, window_seconds, delta_seconds, within_window)` for the joined claim's evidence trace (recorded on success AND failure — a rejected coincidence is auditable, never silent). `CorrelationWindowPolicy` holds per-join-type defaults (`event_incident`=2h, `event_event`=15m; `DEFAULT_WINDOW_SECONDS`=1h fallback) + per-org overrides (`set_org_window`). `gate_operational_corroboration(event, incident)` is the corroboration integration surface: an event↔incident agreement INSIDE the window elevates MEDIUM→HIGH (same bar as COR-09/COR-10), the identical agreement OUTSIDE contributes ZERO — coincidence never inflates confidence. Reuses the `discovery/packs/corroboration_rules.py` confidence vocabulary. Tolerant timestamp extraction; inclusive boundary; unparseable ts → cannot-join. Reusable surface the MSP event corroboration rules (B4/B6) consult; does NOT rewire the app-friction COR-09/COR-10 (30-day freshness, not cloud joins). T6 calibrates window defaults. Documented in `docs/msp_operational_event_schema.md` §12.
* `backend/discovery/signals/aggregation.py`: MSP-B7/AT-670 (T2) aggregation roll-ups — the SECOND event-volume discipline, layered on T1 (`ops_stream.py`). `roll_up(active_signals)` / `aggregate_events(events)` project the T1 `ActiveSignal`s of high-cardinality classes (`HIGH_CARDINALITY_CLASSES` = `audit` floods + `state_change` storms; tunable, T6 calibrates) into a compact detector-visible `AggregateSignal` carrying the EXACT `member_count`, first/last span, a `severity_profile` (the spread the signature ignores), and a BOUNDED `sample_pointers` set (`DEFAULT_EVIDENCE_SAMPLE_SIZE`, default 10) — each of which resolves to a stored raw payload via `resolve_sample_raw(store)`. Raw retention is UNCHANGED (all raw payloads stay in the AT-638 store; only the pointers held on the aggregate are sampled). Sampling is deterministic + span-anchored (members sorted by `(source_timestamp, source_artifact)`, evenly spaced INCLUDING both endpoints) and org-scoped. Low-cardinality signals keep full T1 traceability (`only_high_cardinality=True` by default). T2 ONLY. Documented in `docs/msp_operational_event_schema.md` §9.
* `backend/discovery/signals/noise_floor.py`: MSP-B7/AT-671 (T3) noise floors — the THIRD event-volume discipline, sitting between T1 dedup and T2 aggregation (pipeline: dedup → floor → budget → aggregate). `apply_noise_floors(signals)` / `NoiseFloorPolicy.apply` partition the T1 folded `ActiveSignal`s into detector-visible (occurrence count ≥ the per-event-class floor) and suppressed-and-COUNTED (below floor). Suppression is LOUD, never silent: a `SuppressionReport` tallies suppressed signatures AND event volume per class (JSON-serialisable for the run record / R18-C2), self-describing via the recorded `floors`. `DEFAULT_NOISE_FLOORS` = `{audit:5, state_change:5, access:5}`; unlisted classes use `DEFAULT_FLOOR=1` (never suppressed) — `error`/`security` are deliberately never floored by default. Count == floor is visible (strictly-below is suppressed). Configurable per policy (T6 calibrates from B8). Deterministic + order-independent. T3 ONLY. Documented in `docs/msp_operational_event_schema.md` §10.
* `backend/discovery/signals/resource_graph.py`: MSP-B0/AT-639 event-driven, conservative promotion of event-referenced cloud resources into knowledge-graph entities. `create_resource_entities(events, run_id=...)` creates an entity ONLY for a resource an observed event references (no `resource` → nothing) — NO speculative estate modelling (parents/children/topology are B3's CMDB, not event inference); nodes only, no speculative edges. De-dups per `(org_id, resource_id)`; org-scoped via each event's `org_id`. Each resource resolves through the existing conservative `app.entity_resolution.resolve_or_create_entity` as `entity_type='system'` keyed on the provider id (ARN/Azure resource id) so repeat sightings collapse and distinct resources never false-merge. Resolver injectable + lazily imported (keeps the package import light). Documented in `docs/msp_operational_event_schema.md` §7.
* `backend/discovery/log.py`: discovery logging utilities.
* `backend/discovery/lending_scorer.py` / `strs_benefits_scorer.py`: pack-specific scorers for nCino and STRS packs.
* `backend/discovery/packs/github_engineering_scorer.py`: scorer for the `github_engineering` pack. Elevates PR-bottleneck confidence MEDIUM → HIGH when Jira corroborates.
* `backend/discovery/integration_verifier.py`: verifies integration signal completeness.
* `backend/discovery/track_a_adapter.py`: Track A ingestion adapter.
* `backend/discovery/offline_export.py`: offline fixture/data export utilities.
* `backend/discovery/ingest/live_validator.py`: live data validation at ingest time.
* `backend/discovery/ingest/strs_jira_corroboration.py` / `strs_sn_corroboration.py`: STRS cross-source corroboration against Jira and ServiceNow.
* `backend/discovery/ingest/java_app.py`: R17-A3 Java application change-based ingestor — AgentIQ's first non-SaaS source. Implements `ChangeBasedIngestor` over the OPERATIONAL surface only (Spring Boot Actuator health/diagnostics endpoints + application logs); never reads source code or external APM (AC8). Opaque per-app `{log_offset, metrics_ts, metrics_seq}` checkpoint. The live `JavaAppClient` closes its HTTP session after each read; operational SIGNAL is aggregated over the whole delta by `java_app_signals`, never per-record.
* `backend/discovery/ingest/java_app_config.py`: R17-A3 per-deployment Java app target configuration + vault credential resolution. Targets come from config (`JAVA_APP_TARGETS` / offline fixture), never network scanning; inline secrets in config are rejected and credentials resolve from the vault, never logged (AC3).
* `backend/discovery/ingest/java_app_signals.py`: R17-A3 Java operational signal **adapter** — thin wrapper that binds `source_system='java_app'` / the COR-09 corroboration key onto the SHARED extraction in `operational_signals.py`. It no longer contains the extraction logic itself (that was extracted to be shared with .NET — R17-A4/AC3); it exposes `build_evidence_pointer`, `build_java_app_signal`, `build_java_app_corroboration_payload`.
* `backend/discovery/ingest/operational_signals.py`: R17-A4 SHARED, platform-agnostic operational-signal extraction reused by BOTH Java and .NET (AC3): error patterns, latency/throughput degradation, exception clustering, resource pressure, the friction rollup, and the observed `EvidencePointer` builder (`origin='observed'`, `source_system` supplied by the caller). Reads NEUTRAL metric fields (`memory_used_ratio`, `cpu_usage`); output keys `heap_pressure`/`cpu_pressure` are a shared GC-heap/CPU concept. Do not re-copy this logic into a platform module — that is the exact drift the R17-A4 refactor prevents.
* `backend/discovery/ingest/operational_ingest.py`: R17-A4 SHARED change-ingestion base (`OperationalChangeIngestor`) + the opaque per-app cursor machinery (`encode/decode_checkpoint`, `app_cursor`) and the tolerant `parse_log_payload`. Java/.NET ingestors subclass it and implement only the collection hooks (`_load_targets`, `_raw_operational`, `_to_metric_record`, `_to_log_record`); `ingest_changes`, delta windowing, resumable batching, and provenance are inherited.
* `backend/discovery/ingest/operational_config.py`: R17-A4 SHARED credential/secret primitives for operational-app targets — `FORBIDDEN_SECRET_KEYS`, `find_inline_secret_keys`, and the vault-first/env-fallback `resolve_target_secret`. Shared by `java_app_config.py` and `dotnet_app_config.py` so the security-critical secret handling cannot drift between platforms.
* `backend/discovery/ingest/documents.py`: R18-A1 (T1) `DocumentIngestor` — change-based document ingestor (`connector_id='documents'`) on the R16-A1 `ChangeBasedIngestor` contract. Reads the actual CONTENT of files (the extraction layer between file-bearing sources and the R18-B1 retrieval substrate). Checkpoint is an opaque per-file `{artifact_id: signature}` map; only files whose content signature is new/changed are read + extracted (AC2), unchanged files are never re-read. Per-file failure isolation (AC5): a corrupt/unreadable file fails its OWN extraction only (recorded as `extraction.status='error'`, never run-fatal) and does NOT advance its checkpoint signature (retried next run); a DELIBERATE skip (`ExtractionSkipped`) IS recorded AND advances. Size cap + per-run extraction budget (T4/AC4): oversized files are skipped-with-reason `size_capped` (advances) and files past the per-run budget `budget_exceeded` (does not advance → retried); both configurable via `DOCUMENT_MAX_FILE_BYTES` / `DOCUMENT_EXTRACTION_BUDGET_BYTES` or constructor args. Every record carries the OBSERVED EvidencePointer spine (`source_system='documents'`, `origin='observed'`). Deletes: emits tombstones for files that vanish from a full-inventory source (`reports_deletes` mirrors the source). Does NOT chunk/embed/index and does NOT call `retrieval.ingest_content` itself (that is `documents_handoff.py`, T3) — the extracted `content`+provenance travels on the record ready for it. Source and extractor are both injectable.
* `backend/discovery/ingest/documents_handoff.py`: R18-A1 (T3) retrieval hand-off — the ONE place document extraction meets the R18-B1 substrate. `ingest_documents(org_id, ...)` drives the `DocumentIngestor` through the shared change runner and, per fully-read batch, maps each `extraction.status=='extracted'` record to a `ContentArtifact` and hands it to `retrieval.ingest_content(org_id, artifacts)` (AC1). Only extracted records are handed over — skips, per-file errors, and delete tombstones carry no text and are never sent (deletion/freshness is R18-B2). Translates the connector id `documents` → the substrate's canonical `source_system='document'` (the value in `KNOWN_SOURCE_SYSTEMS`); provenance carries `origin='observed'` + the full EvidencePointer spine + filename/location so a retrieval hit shows the correct source file (AC6). At-least-once: a substrate-reported artifact failure raises so the checkpoint does NOT advance past un-indexed content (re-handed next run; idempotent via `ingest_content`'s per-artifact replace). Does NOT chunk/embed/index. `ingest_fn` is injectable for tests.
* `backend/discovery/ingest/documents_source.py`: R18-A1 (T1) document source contract — `DocumentRef` (id + change signature + provenance, NO bytes) and `DocumentSource` (`list_documents` returns the FULL current inventory; `read` returns one file's bytes, called ONLY for a new/changed ref). Offline `FixtureDocumentSource` (`fixtures/documents_sample.json`); live `ConfiguredLocationSource` scans the per-deployment `DOCUMENT_LOCATIONS` directories (never network discovery). In live mode `default_source` composes these with the SharePoint/Confluence attachment sources (`documents_attachments.py`, T5) via `CompositeDocumentSource`.
* `backend/discovery/ingest/documents_attachments.py`: R18-A1 (T5) — wires the 1.7 SharePoint document-library and Confluence attachment connectors into the document path. `SharePointDocumentSource` / `ConfluenceDocumentSource` WRAP the existing `SharePointIngestor` / `ConfluenceIngestor` and reuse their access layer (OAuth client, granted-site/space filtering, fixture plumbing) to surface file driveItems / page attachments as `DocumentRef`s so their bytes flow through the DocumentIngestor — no new extraction/hand-off code. The only new capability is fetching bytes (the connectors deferred content to 1.8): live via `SharePointGraphClient.download_item_content` / `ConfluenceClient.download_attachment` (added on those clients), offline via inline fixture bytes. Incremental (AC2) rides the DocumentIngestor's per-file signature checkpoint — signature is the driveItem eTag/change-marker (SharePoint) or attachment version (Confluence), so an unchanged file/attachment is never re-fetched. `reports_deletes=False` (delta-oriented; deletion is R18-B2). `CompositeDocumentSource` fronts several sources, routing each `read` to the child that listed the ref and isolating a failing source (degrade, don't crash).
* `backend/discovery/ingest/extraction/`: R18-A1 document text-extraction plug point (the format boundary). `__init__.py` owns the contract — `ExtractedText` / `ExtractionSkipped` / `ExtractionError`, `detect_format`, and the handler registry (`register_handler`) — and imports the handler submodules (which self-register on import). Handlers (T2): `text.py` (text/markdown/CSV — pure decode), `pdf.py` (pypdf; text-based only — scanned-image → `SCANNED_IMAGE` skip, password-protected → `ENCRYPTED` skip), `docx.py` (python-docx; paragraphs + tables), `xlsx.py` (openpyxl; per-sheet cell text), `pptx.py` (python-pptx; slide text); `_office_common.py` detects encrypted OOXML (OLE-compound magic) uniformly. Adding a format = a new handler module calling `register_handler` — NO ingestor change. Loud skips, never silent emptiness: an unsupported type is `UNSUPPORTED_FORMAT`, a recognised-but-unhandled one `NO_HANDLER`, a corrupt file raises `ExtractionError` (isolated per-file by the ingestor), and an empty file is a truthful empty `ExtractedText`. Requires `pypdf`/`python-docx`/`openpyxl`/`python-pptx` (in `requirements.txt`); a missing parser degrades to a loud `NO_HANDLER` skip, never a crash.
* `backend/discovery/ingest/dotnet_app.py`: R17-A4 .NET application change-based ingestor — the .NET counterpart to `java_app.py`, completing the operational phase. Subclasses `OperationalChangeIngestor`; supplies only the .NET COLLECTION edge (ASP.NET Core health checks + EventCounters via `DotNetAppClient`, the `diagnostics_url` endpoint field, and .NET `LogLevel` normalisation `Critical→CRITICAL`). Operational surface only — never source code or external APM (AC8). Same opaque `{log_offset, metrics_ts, metrics_seq}` checkpoint as Java.
* `backend/discovery/ingest/dotnet_app_config.py`: R17-A4 per-deployment .NET app target configuration + vault credential resolution. Targets come from config (`DOTNET_APP_TARGETS` / offline fixture), never network scanning; declares a `diagnostics_url` (vs Java's `actuator_url`). Inline secrets are rejected and credentials resolve from the vault via the shared `operational_config` primitives, never logged (AC4).
* `backend/discovery/ingest/dotnet_app_signals.py`: R17-A4 .NET operational signal **adapter** — binds `source_system='dotnet_app'` / the COR-10 corroboration key onto the SHARED `operational_signals.py` extraction (identical to how `java_app_signals.py` does for Java). Exposes `build_evidence_pointer`, `build_dotnet_app_signal`, `build_dotnet_app_corroboration_payload`.
* `backend/discovery/ingest/git_content.py`: R18-A2 Git **content** change-based ingestor (`GitContentIngestor`, `connector_id='git_content'`) — reads repository file content into the retrieval substrate. Distinct from the GitHub *signal* connector (`connectors/saas/github.py`), which reads activity metadata only. The commit graph IS the change feed: the opaque checkpoint is the last-ingested commit SHA per repo (a per-repo cursor map; a plain SHA once synced, `{sha,offset}` while a first load streams so it resumes), a first run streams the HEAD tree as resumable checkpointed batches, and incremental runs process only the files `since..HEAD` touched. File content is handed to the substrate via `retrieval.ingest.ingest_content` (content_type `code`, `source_system='git'`, `origin='observed'`) — never a second vector-writing path; binaries are skipped-with-reason. The **commit-message corpus** is ingested alongside it (AT-532): each commit message is its own `conversation`-typed artifact (`source_artifact='{repo}@{sha}'`) with author/date provenance; content-only, never a delta record. Per-repo **include/exclude path filtering** (AT-530, `PathFilter` / `DEFAULT_EXCLUDE_GLOBS`) drops vendored deps / generated-build output / dependency lockfiles by default (applied to BOTH the first-load tree and the incremental diff); the defaults are editable per org (`path_defaults` in the config source / `GIT_CONTENT_PATH_DEFAULTS`) and per repo (`include` allow-list, `exclude` globs, `use_default_excludes` toggle). **Deletion propagation** (AT-533, AC3): a file removed by a commit is yielded as a `change_kind='deleted'` record (so the runner emits a deleted event) AND routed in the same batch to `retrieval.ingest.remove_content`, so its chunks leave retrieval and it stops being retrievable (idempotent, org-scoped; excluded paths are never routed). **Secret redaction** (AT-531, AC5): `_secret_scan` runs unconditionally between extraction and every `ingest_content` hand-off — for BOTH the file stream and the commit corpus — redacting key/token/password signatures (via `discovery/ingest/secret_redaction.py`) so a committed credential is never indexed into a retrievable store ("redact before index, always"). Each redaction is recorded as an `ingestion.secret_redacted` telemetry event + a WARNING (pattern types + counts only, never the value) for run-health visibility. Offline reads `fixtures/git_content_sample.json`; live shells out to `git` in a local clone declared per deployment via `GIT_CONTENT_REPOS`.
* `backend/discovery/ingest/secret_redaction.py`: R18-A2 / AT-531 content-source-agnostic secret-pattern scanner (`scan_and_redact`) — redacts key/token/password signatures (PEM keys, AWS/GitHub/Slack/Google tokens, JWTs, secret-named assignments) from plain text before it reaches the retrieval substrate. Dependency-free; wired into `git_content.py`'s `_secret_scan` seam and reusable by any future content producer. Never returns or logs the matched secret value. MSP-B11 / AT-700 (T5) adds `scan_and_redact_security` — the SAME base set PLUS a security IOC/artefact set (defanged indicators, URL-embedded credentials, bearer tokens, MD5/SHA1/SHA256 hashes, IPv4/IPv6/MAC, email indicators) for the highest-risk security-note corpus; base `scan_and_redact` is unchanged so ordinary IT content is not aggressively IOC-redacted. Both share one `_apply` loop.
* `backend/discovery/ingest/servicenow_security_notes_handoff.py`: MSP-B11 / AT-700 (T5) redaction-before-indexing seam for ServiceNow SIR **security work notes** (the counterpart to B4's `servicenow_notes_handoff.py`). Reads the note fields that the SIR workflow signal deliberately EXCLUDES (`work_notes`/`comments`/`additional_comments`/`close_notes`), redacts the combined text via `scan_and_redact_security` BEFORE any `ContentArtifact` exists, and hands only the sanitized text + an access-controlled evidence pointer to `retrieval.ingest_content` (`source_system='servicenow'`, `content_type='prose'`, `connector_id='servicenow_security_notes'`). The raw note is never persisted — reachable only via the evidence pointer + `source_url`. Reuses the `ingestion.secret_redacted` telemetry event (pattern types + counts only). Org-scoped; a callable seam (not auto-run in ingest), invoked by B12/retrieval prep. Does NOT widen the SIR field scope (AC2).
* `backend/discovery/calibration/calibrator.py` / `ranking.py`: confidence calibration and entity ranking.
* `backend/discovery/packs/pack_config.py`: centralized pack selection. Current packs: `service_cloud`, `ncino`, `strs_benefits`, `sqlserver_opsignal`, `github_engineering`, `enterprise_ops`, `cloud_ops`. The `cloud_ops` (Cloud-Operations Discovery, MSP-B6) pack keeps its calibration values, detector thresholds, and NOC terminology set in the external `cloud_ops_pack_config.json` (loaded via `cloud_ops_config.py`) so a config change alters behaviour with no code deploy. Its four record/stream detectors (`cloud_ops_recurring_resolution_loop`, `cloud_ops_alert_triage_toil`, `cloud_ops_reassignment_ping_pong`, `cloud_ops_queue_ageing`, MSP-B6 T2) read the ITSM/event block at `sn_data['cloud_ops']` and emit findings carrying the four-part contract built by `discovery/packs/cloud_ops_finding.py` (evidence, confidence, corroboration, source trace; groups/queues/services/CIs only — never individuals). The shared-CI hotspot detector (T3) and ops-impact scorer (T4) arrive next.

### Backend — Database & Connectors

* `backend/database/connection.py`: separate psycopg2 connection/session helpers (`get_db_connection` / `get_db_session`, the latter a thin psycopg2-backed session adapter used by telemetry) — distinct from `backend/app/db.py`. Different layers; do not conflate. Note: despite its docstring wording it is plain psycopg2, not SQLAlchemy, and does NOT use the `db.py` pool.
* `backend/database/models/`: SQLAlchemy ORM models — `audit_log.py`, `credentials.py`, `signal_snapshots.py`, `telemetry.py`, `workspace_members.py`.
* `backend/database/seed_loader.py`: seeds the PostgreSQL database (`DATABASE_URL`) from `backend/database/seed/` (11 JSON seed files).
* `backend/connectors/db/`: native DB connector subsystem — Oracle, PostgreSQL, SQL Server drivers with connection pooling.
* `backend/connectors/db/oracle_ingestor.py`: Oracle operational-signal ingestor. Missing scope does NOT fall back to Oracle's sample `HR` schema — it returns degraded signals instead, so a misconfigured scope surfaces rather than silently querying sample data.
* `backend/connectors/db/postgresql_ingestor.py`: PostgreSQL operational-signal ingestor. Boolean predicates retry with an integer fallback when PostgreSQL raises a datatype-mismatch `pgcode` (`42804`/`42883`).
* `backend/connectors/db/query_guard.py`: SQL injection prevention — must be invoked for every native DB connector query. Skipping it bypasses injection protection. Uses sqlparse token traversal to strip CTE aliases (`WITH x AS (...)`) so scope checks resolve real base tables; fail-closed on ambiguous extraction.
* `backend/connectors/db/scope.py`: table/column scope management for DB connectors.
* `backend/connectors/saas/github.py`: GitHub SaaS connector — REST ingestion backing the `github_engineering` pack (PRs, commits, branches).

### Backend — Token Generation & Tests

* The `backend/token_generation/` server-key token-minting tooling has been **removed**. All connectors are now credentialed at runtime: Salesforce/Jira/ServiceNow via the Integration Hub OAuth flow (tokens in the credential vault, URLs captured at connect, sourced from the DB per org — see live ingest below); nCino and STRS run against the connected Salesforce org (the OAuth credentials in the per-run context) with optional `NCINO_*`/`STRS_*` env overrides.
* `backend/tests/contract/`: contract and API tests. Run against a dedicated, disposable PostgreSQL test database via `conftest.py` (schema dropped and rebuilt from migrations at session start).
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
* `docs/INTEGRATE_*.md`: integration guides per connector — includes `docs/INTEGRATE_JAVA_APP.md` (R17-A3 Java application ingestion) and `docs/INTEGRATE_DOTNET_APP.md` (R17-A4 .NET application ingestion — setup + the shared-extraction / design-review reference).
* `docs/R17-A3_JAVA_APP_SCOPE.md` / `docs/R17-A4_DOTNET_APP_SCOPE.md`: R17-A3 (Java) and R17-A4 (.NET) application ingestion phase-one scope & boundaries (operational surfaces only — no source code (1.8) and no external APM), with the engineering/QA/product finding-evaluation rubric. The R17-A4 doc also records the shared-extraction contract (AC3).
* `deployment/README.md`: production env var guide covering OAuth secrets, vault, and `CREDENTIAL_VAULT_KEY`. Also documents the no-public-inbound deployment posture (R18-A3) and links the scoped-inbound package below.
* `deployment/SCOPED_INBOUND_CALLBACK.md`: R18-A3 T6 / AC7 — customer-facing security-team package for Approach B (scoped-inbound OAuth callback). Reverse-proxy patterns (nginx/Apache/cloud WAF), callback-path-only exposure, source-IP allowlist, and the security-review checklist. Documentation only; applies to connectors with no outbound-only auth mode (GitHub, Slack, and Teams/SharePoint until AT-556). Vendor-hosted relay (Approach C) is rejected.
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

Reference `backend/.env.template` for the full list of env vars needed before first run.

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

* Backend `.env` and frontend `.env` are intentionally untracked. Use `backend/.env.template` as the setup reference.
* Backend database is PostgreSQL, configured via `DATABASE_URL`. Contract tests use a dedicated `*_test` database derived from `DATABASE_URL` (or `TEST_DATABASE_URL`); they never touch the dev DB. `DB_POOL_MIN` / `DB_POOL_MAX` tune the `db.py` connection pool.
* Seed data location defaults to `backend/database/seed`; override with `SEED_DIR`.
* Local backend CORS defaults allow `localhost:5173` through `localhost:5176`. Override the allowed origins list with `CORS_ORIGINS` (comma-separated) for non-default dev ports or staging.
* Frontend API base uses `VITE_API_BASE_URL`, defaulting to `http://localhost:8000` in dev.
* Frontend auth uses `VITE_DEV_JWT`, defaulting to `dev-token-change-me`.
* Optional LLM features use `ANTHROPIC_API_KEY`; deterministic fallbacks should still work without it.
* Set `REQUIRE_CONNECTOR_SECRETS=1` in production to enforce that all connector secrets are present on startup. Dev and test environments intentionally leave this unset.
* Salesforce/Jira/ServiceNow live ingestion is OAuth-only and DB-sourced: tokens come from the credential vault and instance/site URLs are captured at OAuth connect time, both keyed per org. `app/live_ingest_credentials.py` `resolve_live_systems(org_id)` reads them from the DB and publishes them to a per-run `contextvars` context (`discovery/ingest/__init__.py` `set_live_connectors`/`get_live_connector`) that each connector's `_get_client()` reads — never process-global `os.environ`, so concurrent multi-tenant runs cannot read each other's credentials. `SF_*`/`SERVICENOW_*`/`JIRA_*` env vars are only a CLI/standalone fallback (`JIRA_PROJECT_KEY` is still read for live Jira). Per R17-D3 Addendum A T13 (AC12), `backend/.env` / `.env.example` / `.env.template` contain **no per-client connector credentials** — only instance configuration; do not add credential env vars to them.
* nCino and STRS live ingestion resolve their access token from the vault only (a dedicated `ncino`/`strs` static credential first, else the connected Salesforce org's OAuth credential from the per-run context) — the former `NCINO_ACCESS_TOKEN`/`STRS_ACCESS_TOKEN` env credentials are gone. `NCINO_INSTANCE_URL`/`STRS_INSTANCE_URL` survive in code as CLI/standalone instance-URL fallbacks (non-credential) but are intentionally absent from the env templates.
* Java (`java_app`) and .NET (`dotnet_app`) operational-app ingestion are configured, not OAuth-discovered: the in-scope applications are declared per deployment in `JAVA_APP_TARGETS` / `DOTNET_APP_TARGETS` (JSON arrays of secret-free target configs; the offline fixture is used when unset), and the credential is resolved from the vault (per-run context) with `JAVA_APP_TOKEN` / `DOTNET_APP_TOKEN` as CLI/standalone fallbacks. Inline secrets in a target config are rejected. These connectors are gated on `java_app` / `dotnet_app` being in the run's connected systems.
* Document ingestion (`documents`, R18-A1) is configured, not discovered: offline (default) reads the deterministic `fixtures/documents_sample.json`; live scans the per-deployment `DOCUMENT_LOCATIONS` (a JSON array of `{"location", "path"}` directory configs) for direct on-disk/mounted document locations AND composes in the SharePoint/Confluence attachment sources (T5, `documents_attachments.py`) — a connector not connected simply contributes nothing (the composite isolates each source). No credentials are read for the configured-location scan; the SharePoint/Confluence sources reuse their connectors' vaulted OAuth tokens.
* Document ingestion (`documents`, R18-A1) is configured, not discovered: offline (default) reads the deterministic `fixtures/documents_sample.json`; live scans the per-deployment `DOCUMENT_LOCATIONS` (a JSON array of `{"location", "path"}` directory configs) for direct on-disk/mounted document locations. SharePoint/Confluence attachment surfacing is a separate source wired in a later task (T5). No credentials are read here for the configured-location scan.
* Document extraction caps (R18-A1 T4): `DOCUMENT_MAX_FILE_BYTES` (default 26214400 = 25 MiB; `0` disables) is the per-file size cap — a larger file is skipped-with-reason `size_capped` (never read when the source knows the size). `DOCUMENT_EXTRACTION_BUDGET_BYTES` (default 268435456 = 256 MiB; `0` disables) is the per-run extraction budget — once that many content bytes have been read in a run, remaining changed files are skipped-with-reason `budget_exceeded` and retried next run. A `size_capped` skip advances the checkpoint (deterministic); a `budget_exceeded` skip does not (transient). Both are overridable per `DocumentIngestor` via constructor args.
* OAuth client secrets (production, enforced by `REQUIRE_CONNECTOR_SECRETS`): `SALESFORCE_CLIENT_SECRET`, `SERVICENOW_CLIENT_SECRET`, `JIRA_CLIENT_SECRET`, `GITHUB_CLIENT_SECRET`, `SLACK_CLIENT_SECRET`, `TEAMS_CLIENT_SECRET`, `SAP_CLIENT_SECRET`, `DYNAMIC365_CLIENT_SECRET`. Teams (Microsoft Graph) also reads `TEAMS_TENANT_ID` (Entra tenant GUID, or `organizations`; default `organizations`) — see `app/auth/configs.py`.
* `CREDENTIAL_VAULT_KEY`: Fernet key for token vault encryption. Required in production; missing it causes connector secret storage to fail or fall back to plaintext.
* `DEV_JWT_ROLE`: role override for the dev token (`owner`/`analyst`/`viewer`). Used alongside `ADMIN_JWT`, `ANALYST_JWT`, `VIEWER_JWT` in contract tests.
* `INGEST_MODE`: `online`/`offline`/`test` — controls the ingestion path.
* `NETWORK_PROFILE` (R18-A3 T5 / AT-558): deployment inbound-network posture — `standard` (default) or `no_public_inbound`. Read live via `app/network_profile.py` (`get_network_profile()` / `is_no_public_inbound()`); unset/unrecognised falls back to `standard`. In `no_public_inbound` the Integration Hub hides the authorization-code Connect button for any connector with an outbound-only mode and shows the outbound setup path instead (AC4). Exposed with a per-connector auth-capability map at `GET /api/network-profile` (viewer+, in `routes_connector_auth.py`), built from `auth_modes.get_connector_auth_capability()`. The flag lives at the connect/setup edge only — ingestion stays mode-agnostic.
* Model provider gateway (R16-D1 / R17-D1 / R17-D2): `MODEL_GENERATION_PROVIDER` and `MODEL_EMBEDDING_PROVIDER` select which provider serves generation and embedding **independently** (valid values: `hosted` (default), `in_boundary`, `customer_tenant`). Resolved at call time; unknown values raise at startup via `validate_provider_config()`. All three modes register through `register_provider()` in `model_gateway/__init__.py`; adding a provider requires no calling-code change.
* In-boundary provider config (R17-D1, used only when a provider above is set to `in_boundary`; all owned inside `backend/app/model_gateway/` — no caller reads these): `IN_BOUNDARY_BASE_URL` (common OpenAI-compatible base; the adapter derives `/v1/chat/completions` and `/v1/embeddings`), `IN_BOUNDARY_GENERATION_ENDPOINT` / `IN_BOUNDARY_EMBEDDING_ENDPOINT` (override either path independently), `IN_BOUNDARY_API_KEY` (bearer token, resolved live per call — never logged), and the model names `IN_BOUNDARY_MODEL` (common fallback) / `IN_BOUNDARY_GENERATION_MODEL` / `IN_BOUNDARY_EMBEDDING_MODEL`. Leave all blank unless in-boundary mode is in use. If `in_boundary` is selected with no endpoint URL configured, `validate_provider_config()` logs a startup warning (calls never raise — they degrade to `ok=False`/`[]`).
* Customer-tenant provider config (R17-D2, used only when a provider above is set to `customer_tenant`; owned inside `backend/app/model_gateway/customer_tenant_config.py` — no caller reads these): targets the customer's **managed** in-tenant model service (e.g. Azure OpenAI). `CUSTOMER_TENANT_ENDPOINT` (resource base; the adapter builds `{endpoint}/openai/deployments/{deployment}/chat/completions|embeddings?api-version=...`), `CUSTOMER_TENANT_API_VERSION` (defaults to a GA version), `CUSTOMER_TENANT_DEPLOYMENT` (common fallback) / `CUSTOMER_TENANT_GENERATION_DEPLOYMENT` / `CUSTOMER_TENANT_EMBEDDING_DEPLOYMENT`, `CUSTOMER_TENANT_GENERATION_ENDPOINT` / `CUSTOMER_TENANT_EMBEDDING_ENDPOINT` (verbatim full-URL overrides for non-Azure-path managed endpoints), and `CUSTOMER_TENANT_API_KEY` (tenant credential sent via the `api-key` header, resolved live per call — never logged). Same resilience/graceful-failure posture and self-emitted `provider='customer_tenant'` telemetry as the other two modes. Leave all blank unless customer-tenant mode is in use.
* Customer-tenant credential vault (R17-D2 T2): the tenant credential is Fernet-encrypted at rest in the shared `credentials` table (reused — no schema change) under the reserved `connector_id = "customer_tenant"`, keyed per org, exactly like connector OAuth tokens. `app/auth/vault.py` owns `store_customer_tenant_credential` / `get_customer_tenant_credential` / `revoke_customer_tenant_credential` (a static key — no OAuth refresh path). Only the gateway resolves it: `app/model_gateway/customer_tenant_vault.py` `resolve_customer_tenant_api_key()` reads the vault first (org from the tenancy context, default org otherwise), then falls back to the `CUSTOMER_TENANT_API_KEY` env var for dev/standalone. The read path never raises and never logs the value; a missing/revoked/undecryptable credential resolves to `""`, and the provider short-circuits to a graceful failure (`ok=False` / `[]`) with no network call. In production the credential lives only in the vault, so a customer revoke fully cuts access.
* `REPLAY_RESETS_DECISIONS`: set to `1` to clear analyst overrides on replay. Default is off.
* `TRACKB_PYTHON` / `TRACKB_RUNNER_MODE`: Python path and mode (`offline`/`live`) for the Track B subprocess runner.
* `BASELINE_JOB_INTERVAL_HOURS` / `BASELINE_MIN_RUNS` / `BASELINE_WINDOW_DAYS`: control the background baseline calculator job.
* `TOKEN_REFRESH_JOB_INTERVAL_MINUTES` (default `10`) / `TOKEN_REFRESH_AHEAD_SECONDS` (default `900`): control the proactive OAuth token-refresher job. The job renews any vault token expiring within the lookahead window; keep the interval comfortably below the lookahead so a token is always refreshed before it lapses.
* Oracle native DB connector env vars: `ORACLE_HOST`, `ORACLE_PORT` (default `1521`), `ORACLE_DATABASE` (service name, default `ORCL`), `ORACLE_DB_USERNAME`, `ORACLE_DB_PASSWORD`. These diverge from the `{CONNECTOR_NAME}_CLIENT_SECRET` convention because native DB connectors authenticate with a database username/password pair (resolved via `username_key`/`password_key`), not an OAuth client secret.
* PostgreSQL native DB connector env vars: `POSTGRESQL_HOST`, `POSTGRESQL_PORT` (default `5432`), `POSTGRESQL_DATABASE` (default `postgres`), `POSTGRESQL_USERNAME`, `POSTGRESQL_PASSWORD`, and `POSTGRESQL_SSL_MODE` (`require`/`prefer`/`disable`). Same divergence rationale as Oracle above.
* `AGENTIQ_DISABLE_BACKGROUND_JOBS`: set to `1` to skip starting background jobs (connector health, baseline calculator, OAuth token refresher, retrieval embedding worker) on app startup. Useful for tests and isolated runs.
* Retrieval embedding worker (R18-B1 T3 + R18-B2 T5): `RETRIEVAL_EMBED_JOB_INTERVAL_SECONDS` (default `60`) sets how often the async worker drains the pending `retrieval_chunks` backlog and runs the model-version backfill; `RETRIEVAL_EMBED_MAX_CHUNKS_PER_ORG` (default `512`, `0`/unset → unbounded) caps chunks embedded per org per tick; `RETRIEVAL_BACKFILL_MAX_CHUNKS_PER_ORG` (default `256`, `0`/unset → unbounded) caps the T5 model-version backfill (re-embedding old-model vectors onto the active model) per org per tick so a migration stays a gentle background convergence; `RETRIEVAL_EMBED_BATCH_SIZE` (default `64`) bounds the chunks per gateway `embed()` call. All embedding routes through `get_embedding_provider()` only.
* Retrieval refresh worker (R18-B2 T3): `RETRIEVAL_REFRESH_JOB_INTERVAL_SECONDS` (default `120`) sets how often the async worker drains a page of the `retrieval_refresh_queue`; `RETRIEVAL_REFRESH_MAX_ARTIFACTS_PER_ORG` (default `128`, `0`/unset → default page size) caps artifacts refreshed per org per tick; `RETRIEVAL_REFRESH_MAX_ATTEMPTS` (default `5`) is the retry budget before a repeatedly-failing artifact is parked `failed` (its chunks stay stale meanwhile, never served). Re-embedding of changed chunks routes through the model gateway only.

## Architecture Notes

* The API protects most endpoints with `require_auth`; tests usually pass `Authorization: Bearer dev-token-change-me`.
* PostgreSQL stores most tables as `{ id, payload }` JSON rows, plus `runs`, `run_events`, and run-scoped KV entries. `backend/app/db.py` handles this layer, serving every query from a process-wide connection pool (see the `db.py` entry above).
* `backend/database/connection.py` is a separate psycopg2 layer (telemetry connection/session helpers). Do not conflate with `db.py`; it does not share the `db.py` pool.
* Run lifecycle starts at `POST /api/runs/start`, then materialization writes status, events, opportunities, evidence, clusters, roadmap, executive report, and enrichment into run-scoped storage.
* Replay should re-serve persisted artifacts only. It must not call live ingestion or regenerate LLM output.
* LLM enrichment is advisory post-processing. It must not mutate scoring fields such as impact, effort, tier, decision, or evidence IDs.
* `OppEnrichment.relationships` is intentionally different from the other enrichment fields: it is read live from `entity_relationships` through `graph_query.py`, not from a run-scoped KV artifact. The graph is cross-run state, so later relationship upserts can change what a historical run's relationship view returns.
* `OppEnrichment` also carries ENT-2 cross-system corroboration fields (`corroboration_sources`, `corroboration_label`, `triple_corroboration`, `corroboration_rule_ids`), populated from the stored opportunity record. See `backend/app/corroboration_engine.py` and `backend/discovery/packs/corroboration_rules.py`.
* Operational-app friction is first-class OBSERVED evidence: **COR-09** (Java, keyed `java_app`) and **COR-10** (.NET, keyed `dotnet_app`) are *elevating* corroborators (MEDIUM→HIGH like ServiceNow/Jira), not Slack-capped. They are gated on the source being connected and only fire when the source's `operational_friction` block is fresh; a lone operational source stays MEDIUM (single-source, COR-08). Java and .NET share the SAME signal language (via `operational_signals.py`), so their corroboration behaviour is identical by construction — adding a third operational platform means: new adapter/ingestor over the shared base + a new parallel COR rule (rule registry + engine check + `_CORROBORATION_RULE_SYSTEMS` + label priority + a contract test), nothing else.
* Pack selection is centralized in `backend/discovery/packs/pack_config.py`. Current packs: `service_cloud`, `ncino`, `strs_benefits`, `sqlserver_opsignal`, `github_engineering`, `enterprise_ops`, `cloud_ops`. The `cloud_ops` (Cloud-Operations Discovery, MSP-B6) pack keeps its calibration values, detector thresholds, and NOC terminology set in the external `cloud_ops_pack_config.json` (loaded via `cloud_ops_config.py`) so a config change alters behaviour with no code deploy. Its four record/stream detectors (`cloud_ops_recurring_resolution_loop`, `cloud_ops_alert_triage_toil`, `cloud_ops_reassignment_ping_pong`, `cloud_ops_queue_ageing`, MSP-B6 T2) read the ITSM/event block at `sn_data['cloud_ops']` and emit findings carrying the four-part contract built by `discovery/packs/cloud_ops_finding.py` (evidence, confidence, corroboration, source trace; groups/queues/services/CIs only — never individuals). The shared-CI hotspot detector (T3) and ops-impact scorer (T4) arrive next.
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
* Contract tests seed an isolated, disposable PostgreSQL test database through `backend/tests/contract/conftest.py` (which installs its own connection pool and patches `db.connect`); do not make tests depend on a local dev database.
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
* `backend/database/connection.py` (psycopg2 telemetry helpers) and `backend/app/db.py` (pooled psycopg2 access) are different layers with independent connections. Mixing them will cause session/connection conflicts.
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
