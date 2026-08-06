# AgentIQ — API_CONTRACT.md (EPIC E0)
Version: v1.23
Date: 2026-08-06

> v1.23 — PR-fix pass (run-health checkpoint streams + adjustment rank scope).
> Two additive fields that shipped in code without a contract entry, recorded
> here so a backend reader treating this file as the source of truth sees them.
>
> 1. `GET /api/run-health/connectors` — each connector item may carry
> `checkpoint_streams` (`number | null`, optional): the number of per-stream
> checkpoint rows the reported `checkpoint_age_seconds` represents, for a
> connector that checkpoints per stream (`{connector_id}:{stream}`) rather than
> under its bare id. Absent/null for a single-cursor connector. The reported age
> is that of the NEWEST stream **that carries a timestamp** — streams with a null
> `captured_at` (an optional ServiceNow table that is absent or unreadable) sort
> last and never displace a real one, so a stalled connector cannot report a
> null age and thereby suppress its own stall alert. Mirrors
> `frontend/src/types/runHealth.ts` `ConnectorHealthItem`.
>
> 2. `_ranking` (opportunities, roadmap entries) and
> `GET /api/learning/adjustment/explain/{runId}/{opportunityId}` now carry
> `rankScope` (`string`): what `baseRank`/`adjustedRank` are relative to —
> `"run"` for the run-scoped surfaces, which order one flat list, and
> `"roadmap_stage"` for a roadmap entry, since the roadmap adjusts each stage
> separately. The SAME finding therefore legitimately holds different `baseRank`
> values across the two payloads; consumers must not compare ranks (or a
> "moved N places" figure) across differing scopes. Additive; pre-v1.23
> consumers are unaffected.
>
> Cross-org note (no shape change): the run-scoped learning-adjustment reads
> (`/preview/{runId}`, `/explain/{runId}/{opportunityId}`, `/base-order/{runId}`)
> now answer **404** for a run belonging to another org, matching the run-scoped
> cloud-ops and graph routes. Previously they served it.

> v1.22 — 2.0-B2 T5 (Cross-Source Entity Enrichment — unmerge & re-evaluation):
> added authenticated Analyst+ endpoints that REVERSE a resolution:
> `POST /api/entities/{entityId}/unmerge` (detach one constituent),
> `POST /api/entities/{entityId}/unmerge-all` (split completely),
> `GET /api/entity-unmerges[?status=&limit=]` (the org's unmerge log),
> `POST /api/entity-unmerges/{unmergeId}/release` (**Owner only** — re-permit
> automatic merging), and `GET /api/findings/reevaluation-flags[?status=&limit=]`.
> An unmerge response reports what actually happened: `outcome`
> (`unmerged` | `not_merged`), `survivorEntityId`, `detachedEntityId`, `unmergeId`,
> `previousRule`, `restoredEntityIds[]` (includes any sub-merge that travelled with
> the detached entity), `remainingConstituents`, `flaggedFindings`, and a
> `dependencySweep` carrying `findingsExamined`, `dependentFindings`,
> `unlinkedFindings`, `runsScanned` and `runsTruncated` — the bound is reported, never
> silently applied. An entity that is not merged answers `not_merged` with HTTP 200
> (a truthful answer, not an error); an unknown entity is a 404. Nothing is deleted by
> a reversal: the restored row keeps its identity, edges and `resolution_status`, and
> gains `metadata.unmerged_from`. **`POST /api/entity-merges/apply` gains a `blocked`
> count and a `blocked` outcome value** — the only change to a previously documented
> shape, and additive (existing counts and fields are unchanged): a pair that was
> unmerged is refused rather than re-merged, with the reason naming the unmerge.
> A `reevaluation-flags` entry carries `opportunityIdentity`, `status`
> (`pending` | `cleared`), `reason`, `triggerKind`, `triggerRef`, `entityIds[]`,
> `flaggedRunId`, `flaggedBy`, `flaggedAt`, and — once a later run has re-observed the
> finding — `clearedRunId` and `clearedAt`. Organization-scoped: an entity or unmerge
> id from another org returns 404, indistinguishable from an unknown id.

> v1.21 — 2.0-B2 T2 (Cross-Source Entity Enrichment — merged-entity provenance):
> added authenticated Analyst+ endpoints exposing what a merged entity is made of:
> `GET /api/entities/{entityId}/provenance` (one entity),
> `POST /api/entities/provenance` (many, bounded at 200 ids — the finding-view
> seam), and `POST /api/entity-merges/apply` (apply the merges T1's auto-merge
> tiers and T3's confirmed proposals authorised). A provenance body carries
> `constituents[]` — EVERY constituent source identity including the survivor's own
> (`is_origin: true`) — plus the `rule` that merged each one
> (`explicit_reference` | `alias_mapping` | `confirmed_proposal`), `rules[]`,
> `source_systems[]`, `constituent_count`, and `is_merged`. An entity that was
> never merged answers with its single own identity and `is_merged: false` (not a
> 404 — "not merged" and "not found" are different). The same block is stored on
> `entities.metadata.merge_provenance`, so it also travels with
> `GET /api/runs/{runId}/entities` — no separate call is required for a surface
> that already reads entities. Merging never deletes a row or changes
> `resolution_status`; the merged-away entity keeps its identity and edges and
> gains a `metadata.merged_into` pointer. Apply is idempotent
> (`merged` / `already_merged` / `skipped` counts). Organization-scoped: an entity
> in another org returns 404, indistinguishable from an unknown id. Additive — no
> previously documented shape changed.

> v1.20 — 2.0-B2 T3 (Cross-Source Entity Enrichment — confirmation review):
> added authenticated Analyst+ endpoints for the PROPOSED cross-source entity
> matches the resolution engine refuses to merge on its own:
> `GET /api/entity-match-proposals[?status=&limit=]`,
> `GET /api/entity-match-proposals/{proposalId}`,
> `POST /api/entity-match-proposals/{proposalId}/decision`, and
> `POST /api/entity-match-proposals/scan`. Only propose-only tiers appear — a pair
> resolved by an explicit cross-reference or the org alias table auto-merges and is
> never queued. `action` is one of `confirm | reject`; `changed=false` means the
> same decision was already current and no history row was added. A decision is
> RECORDED, not applied: nothing in these endpoints merges the graph. Answered
> pairs are never re-proposed — a re-scan reports them as
> `skipped_already_decided` rather than reopening them. Organization-scoped: a
> proposal id from another org returns 404, indistinguishable from an unknown id.
> Additive; existing consumers are unaffected.
> v1.19 - 2.0-A3 T3 (Adjustment explainability): a rank-adjusted opportunity's
> `_ranking` object now carries `reason` — STRUCTURED data (direction,
> ranksMoved, decisionCount, decisionsByAction, outcomeCount,
> outcomesByVerdict, wasCapped, cappedBy, evidenceStrength) plus
> `contributingDecisions` and `contributingOutcomes`, each with a resolvable
> `href`, and a rendered `summary` sentence. A finding that did not move
> carries no `reason`. New route GET /api/learning/adjustment/explain/
> {runId}/{opportunityId} (analyst+; 404 for an unadjusted finding). The
> reason is namespaced under `_ranking` and never appears inside or beside
> confidence, corroboration or the evidence trace. Additive; pre-v1.19
> consumers are unaffected.
>
> v1.18 - 2.0-A3 T2 (Bounded ranking adjustment): opportunities returned by
> GET /api/runs/{runId}/opportunities and the roadmap stages now carry an
> additive `_ranking` object: `baseRank`, `baseImpact`, `adjustedRank`,
> `moved`, `adjusted`, and the `caps` in force; when a learned adjustment
> applied it also carries `effectiveImpact`, `appliedDelta`, `requestedDelta`,
> `wasCapped` and `cappedBy`. Base scoring is unchanged — `impact`, `effort`,
> `tier`, `confidence`, evidence and corroboration are untouched, and the
> stored order remains the base order. New read routes under
> /api/learning/adjustment (state, history, recompute, preview, base-order).
> Additive; pre-v1.18 consumers are unaffected.
>
> v1.17 - 2.0-A2 T7 (No outcome without action): outcome measurement writes
> now require a current customer-recorded action on the opportunity lifecycle.
> Reopened opportunities clear that action and invalidate dependent stored
> movement rows; outcome read surfaces suppress invalidated/stale movements
> rather than exposing them as customer-visible outcomes. No frontend type shape
> changed.

> v1.16 - 2.0-A2 T6 (Outcome surfaces): added org-scoped, cross-run outcome
> surfaces `GET /api/outcomes` and `GET /api/outcomes/{opportunityIdentity}`
> (Analyst+), keyed by `opportunity_identity` rather than a single run. Responses
> are assembled from stored lifecycle and movement artifacts only, include
> `numberRefs[]` so displayed numbers resolve to evidence plus baseline/current
> run ids, and portfolio aggregates require `caveatedMeasurementCount`.
> `GET /api/runs/{runId}/executive-report` now includes `outcomeSection`, built
> from stored movement rows for that run and persisted during materialization.
> Frontend schemas changed in `frontend/src/types/outcome.ts`,
> `frontend/src/types/executiveReport.ts`, and
> `frontend/src/types/analystReview.ts`; this version requires FE and BE lead
> sign-off before merge.

> v1.15 — MSP-B13 (Cloud Connector Onboarding): added the multi-scope cloud
> connector routes for `aws_events` / `azure_events` (T3 / AT-745 — create with
> write-only vault credentials, `POST /{id}/test`, `GET/POST/DELETE /{id}/scopes`,
> `GET /{id}/scopes/{scope}/health`) documented under "Connectors & Confidence".
> T5 / AT-747 adds the per-connector security-artifact routes
> `GET /api/connectors/{id}/security-artifacts` (list) and
> `GET /api/connectors/{id}/security-artifacts/{artifactId}` (download), serving the
> shipped `deployment/` IAM-policy / RBAC-role docs (viewer+). T4 / AT-746 (system-count integration) extends `GET /api/license/limits`'s
> `LicenseLimitsResponse` with the additive optional fields `approachingCap`
> (`boolean`), `atCap` (`boolean`), and `notice` (`string | null`) — the
> approaching-capacity warning and at-cap hard-stop wording the Integration Hub /
> cloud-connector cards render. Each pinned AWS account / Azure subscription counts
> as one system against the licence's `max_systems`, enforced at pin time (HTTP 402
> hard stop). Additive; pre-v1.12 consumers are unaffected.

> v1.11 — R191-P1 T3 (Multi-Pack Discovery Runs — provenance tagging, AC1/AC6):
> documents the `packId` (`string`, optional) and `packVersion` (`string`,
> optional) fields on `OpportunityCandidate` (`GET /api/runs/{runId}/opportunities`)
> and the `packId` (`string`, optional) field on `EvidenceReview`
> (`GET /api/runs/{runId}/evidence`) — the backend already stamped `packId`/
> `packVersion` on stored opportunities (R16-B1 §4); this bump documents that
> existing field and newly extends the same stamp to every evidence item
> (previously undocumented and, for evidence, unstamped). Because
> `RoadmapStage.opportunities` (`src/types/pilotRoadmap.ts`) reuses
> `OpportunityCandidate`, every roadmap entry carries `packId`/`packVersion` too
> with no separate type change. All additive/optional — existing consumers are
> unaffected; absent on runs materialized before this field existed. The run
> record's `packIds` (`string[]`) / `packVersions` (`Record<string, string>`) /
> `packs` (per-pack execution metadata) fields were already added by R191-P1 T2
> and are unchanged here.

> v1.14 — R-1.9.1-L2 / T5 (AT-697): added the Owner-only pre-invoice usage-summary
> endpoint `GET /api/usage/summary?from=YYYY-MM-DD&to=YYYY-MM-DD`, returning the
> Owner-facing usage summary for the caller's org over the inclusive period:
> `{summary_version, org_id, period {from,to}, generated_at, runs {total,
> by_ai_mode, billable}, systems {connected, disconnected, net_change, ledger[],
> over_time[]}, event_count}`. `runs.by_ai_mode`/`total`, `systems.ledger`, the
> per-run `over_time` counts, and `event_count` are a PROJECTION of the same
> aggregation that backs `GET /api/usage/report` (AC6) — they equal the report's
> numbers for the same period by construction. `runs.billable` is the hosted-mode
> run count (the billable subset); `systems.over_time` is the connected-system
> count per run in completed-at order. Unlike the signed report the summary needs
> NO `report_key` and no installed license (an unsigned read-only preview), so an
> Owner can see usage before a report key is provisioned. Owner-only (Analyst/
> Viewer → 403); a malformed period → 400. Built LOCALLY from billing telemetry —
> no outbound contact (no-phone-home posture). Additive — no previously documented
> field changed.
>
> v1.13 — R-1.9.1-L2 / T4 (AT-696): extended the usage-report body
> (`GET /api/usage/report`) with a `tamper_evidence` block for deletion detection
> (AC4): `{algorithm, event_count, sequenced_count, unsequenced_count, seq_min,
> seq_max, expected_count, chain[{seq, entry_hash, chain_hash}], chain_root,
> consistent}`. Each billing event is stamped at emission with a per-org monotonic
> `seq`; the report covers a contiguous seq block, so an event deleted before
> generation leaves a gap — `sequenced_count`/`expected_count` mismatch — and the
> hash chain re-folds independently (`verify_tamper_evidence`), so a report over a
> period with locally deleted events is detectably inconsistent. `per_run` entries
> and ledger entries now also carry their `seq`. The whole block is inside the
> T3-signed report body, so it cannot be altered after generation. Additive — no
> previously documented field changed.
>
> v1.12 — R-1.9.1-L2 / T3 (AT-695): added the Owner-only signed usage-report
> endpoint `GET /api/usage/report?from=YYYY-MM-DD&to=YYYY-MM-DD`, returning the
> signed envelope `{report, signature, algorithm}`. The `report` body carries,
> for the inclusive period: `report_version`, `org_id`, the license `kid` and
> `license_org_id`, `period {from,to}`, `generated_at`, `runs {total, by_ai_mode,
> per_run[]}` (per-run system counts), `system_ledger[]` (connect/disconnect), and
> `event_count`. `signature` is the HMAC-SHA256 of the canonical (sorted-key)
> report bytes keyed by the per-installation `report_key` from the license payload
> (L1); `algorithm` is `"HMAC-SHA256"`. CloudFulcrum verifies with the same
> `report_key`, and any altered byte fails verification. The report is generated
> LOCALLY and never triggers outbound contact (no-phone-home posture). Owner-only
> (Analyst/Viewer → 403); a malformed period or a license without a `report_key`
> → 400. Also available offline as a CLI (`backend/scripts/generate_usage_report.py`).
> Additive — no previously documented shape changed.
>
> v1.11 — MSP-B5 T4: added authenticated Analyst+ runbook-match lifecycle
> endpoints: `GET /api/runbook-matches/{recurrenceId}`, `POST
> /api/runbook-matches/{recurrenceId}/decision`, and `GET
> /api/runbook-matches/{recurrenceId}/decision-history`. Decisions are
> organization-scoped and accept `accept`, `dismiss`, or `defer`. Real changes
> append history; repeating the current action is idempotent. The response keeps
> `proposed` visibly distinct from `observed` and `confirmed`, and represents a
> dismissed match as `absent`. Additive; existing consumers are unaffected.

> v1.11 — R191-P1 T3 (Multi-Pack Discovery Runs — provenance tagging, AC1/AC6):
> documents the `packId` (`string`, optional) and `packVersion` (`string`,
> optional) fields on `OpportunityCandidate` (`GET /api/runs/{runId}/opportunities`)
> and the `packId` (`string`, optional) field on `EvidenceReview`
> (`GET /api/runs/{runId}/evidence`) — the backend already stamped `packId`/
> `packVersion` on stored opportunities (R16-B1 §4); this bump documents that
> existing field and newly extends the same stamp to every evidence item
> (previously undocumented and, for evidence, unstamped). Because
> `RoadmapStage.opportunities` (`src/types/pilotRoadmap.ts`) reuses
> `OpportunityCandidate`, every roadmap entry carries `packId`/`packVersion` too
> with no separate type change. All additive/optional — existing consumers are
> unaffected; absent on runs materialized before this field existed. The run
> record's `packIds` (`string[]`) / `packVersions` (`Record<string, string>`) /
> `packs` (per-pack execution metadata) fields were already added by R191-P1 T2
> and are unchanged here.

> v1.11 — R-1.9.1-L1 / T1 + T2 (Licensing Completion & Hardening): extended the
> Owner-only `LicenseStatusResponse` (`GET /api/license`, also returned by
> `POST /api/license/update-key`) with two additive, optional-null fields:
> `deployment_type` (`string | null` — the payload v2 deployment topology,
> `"saas"` | `"customer_hosted"`, parsed from the signed license and exposed for
> the License UI; `null` for a pre-v2 key or any non-verifiable state — T1/AC5)
> and `reason` (`string | null` — the machine-readable invalid reason when
> `status` is `"invalid"`, notably `"org_mismatch"` for a key bound to a different
> installation org, so the UI can render a specific plain-language explanation;
> `null` for a healthy valid/grace status — T2/AC1). Org binding is enforced at
> verification time: a signature-valid key whose payload `org_id` does not match
> the installation org validates as `invalid: org_mismatch` and is rejected at
> paste time on `POST /api/license/update-key` (HTTP 400, detail "This license was
> issued to a different organisation"), leaving any previously installed key
> untouched. The license key format is otherwise unchanged (the new payload fields
> sit within the already-signed v2 payload). Additive — no previously documented
> field changed. Mirrors `src/types/license.ts`.
>
> v1.10 — R18-C0 P8 (Re-editable review decisions, AC8): extended
> `ReviewAuditEvent` with the optional `tsEpoch` (`number`, the newest-first sort
> key already emitted by the backend) and `previousDecision` (`Decision`, the
> prior decision a change replaced) fields on `GET /api/runs/{runId}/audit`.
> `POST /api/runs/{runId}/opportunities/{id}/decision` now APPENDS a new audit
> event on every decision change — never an overwrite — so a reviewer flipping
> Approve↔Reject preserves the prior event (actor + timestamp) and the full
> decision history stays queryable for audit and the 2.0 feedback-learning loop.
> A no-op re-submit of the current decision appends nothing. Additive/optional —
> existing consumers are unaffected.

> v1.9 — R17-D4 Addendum A / T12 (§2 "Dynamic Organisation Name"): added the
> organisation display-name endpoint `GET /api/license/org-name`, returning the
> `LicenseOrgNameResponse` shape: `orgName` (`string`) — the single resolved
> organisation display name every UI surface consumes (header, workspace labels,
> reports, License page). It is read from the org's live-validated license
> payload's `org_name` (added to the LIC-1 payload this task, defaulting to
> `customer`; the resolver falls back to `customer` for pre-addendum keys that
> omit it) by one server-side resolver — "one name, resolved once" (§5) — so no
> surface carries its own naming logic. Before a key is installed, or for any
> non-verifiable license state, `orgName` is a neutral default, never a stale or
> placeholder customer name (AC16); because the read is live and side-effect-free,
> pasting a key with a different `org_name` updates it immediately with no restart
> (AC15). Requires only authentication (any role, like `GET /api/license/banner`)
> so the name renders on every page for every role; side-effect-free. Additive —
> no previously documented shape changed, and the license key format is unchanged
> (the `org_name` field was carried within the already-reserved payload). Mirrors
> `src/types/license.ts`.
>
> v1.8 — R17-D4 Addendum A / T10 (AT-505): added the Integration-Hub
> license-limit endpoint `GET /api/license/limits`, returning the
> `LicenseLimitsResponse` shape: `systemsUsed` (`int` — connected Integration-Hub
> entities for the org, "one connected entity = one system"), `systemsLicensed`
> (`int | null` — the licensed `max_systems`; `null` for an unlimited/pre-addendum
> license), `unlimited` (`bool` — true when no numeric cap applies), and
> `canConnectMore` (`bool` — aggregate headroom: unlimited, or `systemsUsed <
> systemsLicensed`). The two counts are computed by the same `license_limits`
> helpers the connect-time gate (T9) enforces with, so the count the hub shows
> matches the count that is enforced (Addendum A §1 / AC14). Requires only
> authentication at viewer+ (matching `GET /api/connectors`) so every role that
> sees the Integration Hub sees its usage; side-effect-free. Additive — no
> previously documented shape changed. Mirrors `src/types/license.ts`.
>
> v1.7 — LIC-1 (PR review): extended `LicenseBannerResponse`
> (`GET /api/license/banner`) with the optional `grace_days_remaining`
> (`int | null`) — days left before a `grace` license crosses into read-only, so
> the grace banner can say "discovery runs will be blocked in N days" instead of
> a bare "expired". Populated only in the `grace` state; `null` otherwise.
> Additive — no previously documented shape changed. Mirrors `src/types/license.ts`.
>
> v1.6 — LIC-1 / T9 (AT-350): added the auth-only global-banner endpoint
> `GET /api/license/banner`, returning the minimal `LicenseBannerResponse`
> shape (`status` ∈ valid|grace|readonly|invalid; `expires_at` — `null` when
> there is no valid key; `reason` — optional, e.g. `no_license` /
> `signature_or_format` / `clock_rollback`, `null` for valid/grace and a
> past-grace expiry). `reason` lets the banner distinguish a never-licensed
> install ("No valid license installed") from an expired term ("License expired")
> and a clock anomaly (§5/AC6). Unlike the Owner-only `GET /api/license`, this
> endpoint requires only authentication (any role) so the global expiry banner
> renders on every page for every role — including analysts whose discovery runs
> are blocked (AC4/AC5). Additive — no previously documented shape changed.
> Mirrors `src/types/license.ts`.
>
> v1.5 — LIC-1 / T6 (AT-347): documented the Owner-only admin license endpoints
> `GET /api/license` and `POST /api/license/update-key`, and the
> `LicenseStatusResponse` shape (`status` ∈ valid|grace|readonly|invalid,
> `customer`, `term`, `expires_at`, `days_remaining`; detail fields are `null`
> when there is no valid key). `POST /api/license/update-key` validates before
> storing — an invalid key returns 400 and never replaces the stored key. Both
> endpoints require the Owner role (Analyst/Viewer → 403). Mirrors
> `src/types/license.ts`. Additive — no previously documented shape changed.
>
> v1.4 — ENT-6 / T3-S16-A: extended `OppEnrichment` with the optional
> `causal_hypothesis` (`CausalHypothesisSummary`: `cause_chain`,
> `falsifiability_condition`, `confidence`, `inferred`, `preliminary`,
> `preliminary_reason`). Loaded live from the `causal_hypotheses` table
> (most-recent row per opportunity); `null` when no hypothesis exists. Additive
> and backward-compatible — existing fields unchanged. Mirrors
> `src/types/enrichment.ts`.
>
> v1.3 — ENT-3 / T3-S15-A: extended `OppEnrichment` with the LLM enrichment
> enterprise-hardening fields — graph grounding (`llm_grounded`,
> `graph_entity_count`, `graph_entity_count_shown`, `graph_truncated`),
> hallucination-guard outcomes (`hallucination_removals`,
> `hallucination_rewrites`, `hallucination_llm_rewrites`), the preliminary
> quality gate (`preliminary`, `preliminary_reason`), and `corroboration_label`.
> All additive and backward-compatible — existing fields unchanged. Mirrors
> `src/types/enrichment.ts`.
>
> v1.2 — Documented the LLM-enrichment endpoints and the `OppEnrichment` shape,
> including the Track 3 Stage 1 temporal fields (`baseline_stddev`,
> `baseline_window_days`, `current_value`, `recent_values`, `signal_key`,
> `pack_id`) and the Stage 2 `entities` summary list. Mirrors
> `src/types/enrichment.ts`. No previously documented shape changed.

## Purpose
This contract is the **referee** between Frontend and Backend.

**Rule:** Every UI mock JSON file must have a corresponding API endpoint that returns the exact same JSON shape.

## Source of truth
- TypeScript types in `src/types/*` are the schema reference for Backend responses.
- Backend responses must match field names, required/optional, enum values, and nesting exactly.

## Critical architectural rule (non-negotiable)
**Run-scoped endpoints MUST include `runId` in the URL.**
- No “latest run” fallback.
- If `runId` is missing/invalid → return 404/400.

---

## Endpoint Table

### A) Connectors & Confidence (Screen 1)

#### GET /api/connectors
Replaces: `src/data/mockConnectors.json`  
Response: `Connector[]` (`src/types/connector.ts`)

#### POST /api/connectors/{connectorId}/connect
Purpose: persist connector connection status + metadata.  
Request (v1): `{ "status": "connected" }`  
Response: updated `Connector`

#### Cloud Connector Onboarding — AWS & Azure Events (MSP-B13 / AT-745)

Multi-scope cloud connectors (`aws_events`, `azure_events`): one connection, many
accounts/subscriptions, each scope a system. Secret fields are **write-only** —
encrypted into the per-org vault (R17-D3 path) and never returned. RBAC: Owner
creates/tests/pins/unpins; Analyst/Viewer read scopes + health only.

- `POST /api/connectors/{aws_events|azure_events}` — Owner: create/rotate the connection.
  - AWS request: `{ "partition": "aws"|"aws-us-gov", "access_key_id", "secret_access_key", "session_token"? }`
  - Azure request: `{ "environment": "AzureCloud"|"AzureUSGovernment", "mode": "lighthouse"|"direct", "tenant_id", "client_id", "client_secret" }`
  - Response: `CloudConnectionStatus` (metadata only — no secret): `{ connector_id, provider, configured, status, partition?, environment?, mode?, scope_count, updated_at }`
- `POST /api/connectors/{id}/test` — Owner: validate auth + reachability **before save** (never persists).
  - Response `TestConnectionResult`: `{ connector_id, provider, ok, reason?, message, identity? }` (HTTP 200 with the verdict; provider-specific `reason` on failure).
- `GET /api/connectors/{id}/scopes` — Viewer+: `{ connector_id, provider, scopes: ScopeView[], candidates: string[] }`. Candidates are discovered-but-unpinned scopes (never ingested until pinned).
- `POST /api/connectors/{id}/scopes` — Owner: pin (activate forward-only) a scope, validated by an assume-role (AWS) / auth (direct-keys, Azure) probe.
  - AWS request: `{ "account_id", "role_arn"?, "external_id"?, "regions"?: string[], "partition"?, "label"?, "access_key_id"?, "secret_access_key"? }`
  - Azure request: `{ "subscription_id", "label"? }`
  - Response: `ScopesResponse` (as GET).
- `DELETE /api/connectors/{id}/scopes/{scopeId}` — Owner: unpin (stops ingestion forward-only; history retained). Idempotent → 204.
- `GET /api/connectors/{id}/scopes/{scopeId}/health` — Viewer+: `ScopeHealthResponse` `{ connector_id, scope_id, status, healthy, message?, last_checkpoint_at?, event_volume_last_run?, surfaces_ok[], surfaces_failed{} }`. `status` uses the same vocabulary as run health (`pending`/`ok`/`auth_failed`/`partial`/`failed`).
- `GET /api/connectors/{id}/security-artifacts` — Viewer+ (T5 / AT-747): `{ connector_id, provider, artifacts: SecurityArtifact[] }` where `SecurityArtifact = { id, label, description, filename, media_type }`. The downloadable partner security docs (AWS minimal read-only IAM policy `iam_policy`/`iam_policy_guide`; Azure Reader RBAC role `rbac_role`/`rbac_role_guide`).
- `GET /api/connectors/{id}/security-artifacts/{artifactId}` — Viewer+: serves the artifact file (`Content-Disposition: attachment`) from the shipped `deployment/` docs — the single source of truth (B1/B2 AC9). Unknown connector/artifact → 404.

#### GET /api/confidence/explanation
Replaces: `src/data/mockConfidenceExplanation.json`  
Response: `ConfidenceExplanation` (`src/types/normalization.ts`)

---

### B) Source Intake (Screen 2)

#### GET /api/uploads
Replaces: `src/data/mockUploads.json`  
Response: `UploadedFile[]` (`src/types/upload.ts`)

#### POST /api/uploads
Purpose: add uploaded file metadata (binary upload handled later).  
Request (v1):
```json
{ "name": "incident_data.csv", "sizeLabel": "1.2 MB" }
```
Response: `UploadedFile`

> Note: the current UI type uses `UploadedFile { id, name, sizeLabel, uploadedLabel }`.  
> Contract fields must match that exact naming.

---

### C) Run Lifecycle (Screen 3)

#### POST /api/runs/start
Purpose: start a discovery run and mint a runId.  
Request: `RunInputs` (`src/types/discoveryRun.ts`)  
Response (H-min):
```json
{ "runId": "run_001", "status": "running", "startedAt": "2026-03-18T10:12:00Z" }
```

#### GET /api/runs/{runId}
Replaces: `src/data/mockDiscoveryRun.json`  
Response: `DiscoveryRun` (`src/types/discoveryRun.ts`)

#### GET /api/runs/{runId}/events
Replaces: `src/data/mockRunEvents.json`  
Response: `RunEvent[]` (`src/types/discoveryRun.ts`)

#### POST /api/runs/{runId}/replay
Purpose: reset + replay a run for deterministic demos.  
Response:
```json
{ "ok": true }
```

---

### D) Entities + Evidence (Screens 4 & 5)

#### GET /api/runs/{runId}/evidence
Replaces: `src/data/mockEvidence.json`  
Response: `EvidenceReview[]` (`src/types/partialResults.ts`)

#### POST /api/runs/{runId}/evidence/{evidenceId}/decision
Purpose: set evidence decision with run context.  
Request:
```json
{ "decision": "APPROVED" }
```
Response: updated `EvidenceReview`

#### GET /api/runs/{runId}/entities
Replaces: `src/data/mockEntities.json`  
Response: `ExtractedEntity[]` (`src/types/partialResults.ts`)

---

### E) Normalization (Screen 5)

#### GET /api/runs/{runId}/mappings
Replaces: `src/data/mockMappings.json`  
Response: `MappingRow[]` (`src/types/normalization.ts`)

#### GET /api/permissions
Replaces: `src/data/mockPermissions.json`  
Response: `PermissionRequirement[]` (`src/types/normalization.ts`)

---

### F) Analyst Review + Opportunity Map (Screens 6 & 7)

#### GET /api/runs/{runId}/opportunities
Replaces: `src/data/mockOpportunities.json`  
Response: `OpportunityCandidate[]` (`src/types/analystReview.ts`)

#### GET /api/runs/{runId}/audit
Purpose: persist audit trail events for Analyst Review.  
Response: `ReviewAuditEvent[]` (`src/types/analystReview.ts`)  
Order: newest first

Response shape (must match TS type):
```json
[
  {
    "id": "ae_001",
    "tsLabel": "2026-03-18T10:12:00Z",
    "tsEpoch": 1773828720,
    "action": "APPROVED",
    "previousDecision": "REJECTED",
    "by": "Architect Name",
    "opportunityId": "opp_001"
  }
]
```
`tsEpoch`, `previousDecision`, and `opportunityId` are optional. `tsEpoch`
carries the sort key (newest-first). `previousDecision` is present on
decision-change events (R18-C0 P8): an Approve/Reject change appends a NEW event
preserving the prior one — decisions are never overwritten — so the full
decision history stays queryable for audit and outcome tracking.

#### POST /api/runs/{runId}/opportunities/{id}/override
Purpose: save reasoning override for a specific run.  
Request:
```json
{ "rationaleOverride": "text", "overrideReason": "text", "isLocked": false }
```
Response: updated `OpportunityCandidate`

#### POST /api/runs/{runId}/opportunities/{id}/decision
Purpose: set decision on an opportunity for a specific run.  
Request:
```json
{ "decision": "APPROVED" }
```
Response: updated `OpportunityCandidate`

#### GET /api/runbook-matches/{recurrenceId}
Purpose: return the current runbook-match lifecycle state for one recurrence.
Requires: authenticated Analyst or Owner. The organization comes only from the
authenticated request.

Response:
```json
{
  "org_id": "org_001",
  "recurrence_id": "rec_001",
  "base_state": "proposed",
  "current_state": "proposed",
  "current_action": null,
  "revision": 0,
  "current_match": {
    "org_id": "org_001",
    "recurrence_id": "rec_001",
    "match_state": "proposed",
    "origin": "proposed",
    "runbook": {
      "source_system": "document",
      "source_artifact": "runbooks/restart.md"
    },
    "runbook_evidence": {},
    "citing_incident_evidence": [],
    "cited_references": [],
    "match_confidence": 0.89,
    "label": "Proposed match, pending confirmation",
    "lifecycle": {
      "state": "proposed",
      "label": "Proposed match, pending confirmation",
      "documented_status": "proposed",
      "composite_status": "provisional",
      "ranking_treatment": "provisional",
      "evidence_status": "proposed",
      "active": true
    }
  },
  "lifecycle": {
    "state": "proposed",
    "label": "Proposed match, pending confirmation",
    "documented_status": "proposed",
    "composite_status": "provisional",
    "ranking_treatment": "provisional",
    "evidence_status": "proposed",
    "active": true
  },
  "updated_by": null,
  "updated_at": "2026-07-21T10:00:00Z"
}
```

#### POST /api/runbook-matches/{recurrenceId}/decision
Purpose: accept, dismiss, or defer a proposed runbook match.
Requires: authenticated Analyst or Owner.

Request:
```json
{ "action": "accept" }
```

`action` is one of `accept | dismiss | defer`. Accept returns
`current_state="confirmed"`; dismiss returns `current_state="absent"` and
`current_match=null`; defer keeps `current_state="proposed"`. `changed=false`
means the same action was already current and no history/feedback row was added.

#### GET /api/runbook-matches/{recurrenceId}/decision-history
Purpose: return the append-only analyst decision history, newest first.
Requires: authenticated Analyst or Owner. Each item includes `revision`,
`action`, `previous_action`, `previous_state`, `resulting_state`, `actor_id`, and
`decided_at`.

#### GET /api/entity-match-proposals
Purpose: the organization's review queue of PROPOSED cross-source entity matches
(2.0-B2 T3). Only propose-only tiers appear here: a pair resolved by an explicit
cross-reference or by the org alias table auto-merges and is never queued.
Requires: authenticated Analyst or Owner. The organization comes only from the
authenticated request.

Query: `status` (optional — `pending | confirmed | rejected`; an unrecognised
value is a 400), `limit` (optional, 1–1000, default 200).

Response:
```json
{
  "proposals": [
    {
      "org_id": "org_001",
      "proposal_id": "emp_9f2c…",
      "entity_type": "system",
      "left_entity_id": "e1",
      "right_entity_id": "e2",
      "tier": "name_similarity",
      "confidence": 0.7,
      "status": "pending",
      "evidence": {
        "subject": {
          "entity_id": "e1", "display_name": "Billing",
          "canonical_name": "billing", "entity_type": "system",
          "source_system": "servicenow", "source_record_id": "sn-2"
        },
        "target": {
          "entity_id": "e2", "display_name": "billing",
          "canonical_name": "billing", "entity_type": "system",
          "source_system": "git", "source_record_id": "repo-1"
        },
        "tier": "name_similarity",
        "confidence": 0.7,
        "reason": "exact normalised name match across sources with a corroborating observed relationship",
        "corroborating_relationships": [
          { "relationship_type": "depends_on", "entity_id": "e9" }
        ]
      },
      "revision": 0,
      "decided_by": null,
      "decided_at": null,
      "note": null,
      "first_proposed_at": "2026-08-03T10:00:00+00:00",
      "last_proposed_at": "2026-08-03T10:00:00+00:00"
    }
  ],
  "counts": { "pending": 1, "confirmed": 0, "rejected": 0 },
  "status": "pending"
}
```

`counts` always carries all three statuses (zero-filled).

#### GET /api/entity-match-proposals/{proposalId}
Purpose: one proposal plus its append-only decision history, newest first.
Requires: authenticated Analyst or Owner. A proposal id belonging to another
organization returns 404 — indistinguishable from an unknown id.

Response: `{ "proposal": <as above>, "history": [ { "revision", "action",
"previous_status", "resulting_status", "actor_id", "note", "decided_at" } ] }`

#### POST /api/entity-match-proposals/{proposalId}/decision
Purpose: confirm or reject one proposed match.
Requires: authenticated Analyst or Owner.

Request:
```json
{ "action": "confirm", "note": "same service, different system of record" }
```

`action` is one of `confirm | reject` (there is no `defer` — a proposal nobody
has answered is already `pending`). `changed=false` means the same decision was
already current and no history row was added. Reversing a decision is allowed and
APPENDS a new forward row; history is never rewritten.

**A decision is recorded, not applied.** Confirming records a durable,
attributable statement that two entities are the same thing and stops the pair
being re-proposed; it does not merge the graph. Applying a confirmed identity
with its provenance is a separate step.

Response: `{ "proposal", "action", "previous_status", "resulting_status",
"revision", "changed", "actor_id", "decided_at" }`

#### POST /api/entity-match-proposals/scan
Purpose: recompute the organization's proposals from the ranked resolution
engine. Writes nothing to the graph.
Requires: authenticated Analyst or Owner.

Response:
```json
{ "created": 1, "refreshed": 0, "skipped_already_decided": 2,
  "entity_types": ["system", "team", "project", "object"] }
```

`skipped_already_decided` counts pairs the engine proposed again that a human has
already answered — reported rather than hidden, since those never reopen.

#### GET /api/entities/{entityId}/provenance
Purpose: what a merged entity is made of — every constituent source identity and
the rule that merged each (2.0-B2 T2 / AC2).
Requires: authenticated Analyst or Owner. An entity in another organization
returns 404, indistinguishable from an unknown id.

Response:
```json
{
  "version": 1,
  "entity_id": "e1",
  "constituents": [
    { "entity_id": "e1", "source_system": "servicenow", "source_record_id": "sn-1",
      "display_name": "Payments Platform", "canonical_name": "payments platform",
      "rule": null, "confidence": null, "merged_at": null, "merged_by": null,
      "evidence": {}, "is_origin": true },
    { "entity_id": "e2", "source_system": "jira", "source_record_id": "PAY",
      "display_name": "Payments", "canonical_name": "payments",
      "rule": "explicit_reference", "confidence": 1.0,
      "merged_at": "2026-08-03T10:00:00+00:00", "merged_by": "system",
      "evidence": { "tier": "explicit_reference" }, "is_origin": false }
  ],
  "rules": ["explicit_reference"],
  "source_systems": ["jira", "servicenow"],
  "constituent_count": 2,
  "is_merged": true,
  "last_merged_at": "2026-08-03T10:00:00+00:00"
}
```

The survivor's OWN identity is always present as `is_origin: true`, so
`source_systems` is the complete set of systems the entity speaks for. `rule` is
`null` on the origin (it was not merged in) and names the rule on every other
constituent. An entity that was never merged returns its single own identity with
`is_merged: false` — "not merged" and "not found" are different answers.

The same block is stored on `entities.metadata.merge_provenance`, so it also
travels with `GET /api/runs/{runId}/entities`.

#### POST /api/entities/provenance
Purpose: the same, for many entities in one round trip — a finding view resolving
provenance for every entity it traverses must not issue one request per node.
Requires: authenticated Analyst or Owner.

Request: `{ "entity_ids": ["e1", "e2"] }` — at most 200 ids (400 beyond that).
Response: `{ "provenance": { "<entityId>": <as above> }, "requested": 2,
"resolved": 2 }`. An unknown id is simply absent from the map.

#### POST /api/entity-merges/apply
Purpose: apply the merges already authorised — T1's auto-merge tiers (explicit
cross-reference, org alias table) and, unless `include_confirmed` is false, the
pairs a human confirmed in the Entity Matches review surface.
Requires: authenticated Analyst or Owner.

Request: `{ "entity_types": ["system"], "include_confirmed": true }` (both
optional).
Response: `{ "merged": 1, "already_merged": 0, "skipped": 0, "blocked": 0,
"outcomes": [...] }`

`blocked` (v1.18) counts pairs refused because they were UNMERGED — distinct from
`skipped` ("this applier had no authority here") on purpose, so a reversal being
honoured never looks like a merge that merely did not apply.

Idempotent: a pair already merged is reported as `already_merged` and is not
written again. A name-similarity proposal is never merged by this route — only a
confirmed one is, and it is credited to the `confirmed_proposal` rule rather than
to the tier that proposed it. Every applied merge emits an audit event.

#### POST /api/entities/{entityId}/unmerge
Purpose: reverse a resolution — detach this entity from the one it was merged into,
restore it as an independent entity, and flag every dependent finding for
re-evaluation on the next run.
Requires: authenticated Analyst or Owner.

Request (both optional): `{ "reason": "different services", "max_runs": 25 }`
Response:
```json
{
  "outcome": "unmerged",
  "survivorEntityId": "e1",
  "detachedEntityId": "e2",
  "unmergeId": "unm_...",
  "previousRule": "explicit_reference",
  "restoredEntityIds": ["e2"],
  "remainingConstituents": 0,
  "flaggedFindings": 1,
  "reason": "detached from e1",
  "dependencySweep": {
    "identities": ["<opportunityIdentity>"],
    "findingsExamined": 12,
    "dependentFindings": 1,
    "unlinkedFindings": 3,
    "runsScanned": 25,
    "runsTruncated": 0
  }
}
```

Nothing is deleted: the restored row keeps its identity, its edges and its
`resolution_status`, and gains `metadata.unmerged_from` (history — resolution follows
`metadata.merged_into` only). A chain of merges comes apart at the reversed joint
only, so a sub-merge the detached entity itself contains travels with it and appears
in `restoredEntityIds`.

`unlinkedFindings` counts findings that carry no entity references and therefore
cannot be shown to depend on the merge — they are neither flagged nor hidden.
`runsTruncated` reports findings the bounded sweep did not read.

An entity that is not merged returns HTTP 200 with `outcome: "not_merged"`. An
unknown entity returns 404. Every unmerge emits an audit event.

#### POST /api/entities/{entityId}/unmerge-all
Purpose: split a merged entity completely — one reversal per constituent, each with
its own block and audit event.
Requires: authenticated Analyst or Owner.

Response: `{ "survivorEntityId": "e1", "detached": 2, "outcomes": [ <as above> ] }`

#### GET /api/entity-unmerges
Purpose: the org's unmerges, newest first — one entry per action, and the answer to
"why did this pair stop merging?".
Requires: authenticated Analyst or Owner.

Query: `status` (`blocked` | `released`, omit for both), `limit` (default 100).
Response: `{ "unmerges": [ { "unmergeId", "pairKey", "pairKeyKind", "status",
"survivorEntityId", "detachedEntityId", "entityType", "previousRule",
"restoredEntityIds", "flaggedFindingCount", "unlinkedFindingCount", "reason",
"actorId", "createdAt", "releasedBy", "releasedAt", "releaseReason" } ], "count": 1 }`

#### POST /api/entity-unmerges/{unmergeId}/release
Purpose: allow a previously-unmerged pair to be merged again.
Requires: authenticated **Owner** — this is the one action that re-permits AUTOMATIC
merging of a pair a person deliberately separated.

Request (optional): `{ "reason": "confirmed with the team" }`
Response: `{ "unmergeId": "unm_...", "releasedKeys": 2, "status": "released" }`

Does not itself merge anything — it removes the refusal. Nothing is deleted: the row
keeps its unmerge record and gains who released it and why. An unknown or
already-released id returns 404 (never 403, which would confirm the id exists).

#### GET /api/findings/reevaluation-flags
Purpose: findings awaiting re-evaluation because an entity they were built on
changed identity.
Requires: authenticated Analyst or Owner.

Query: `status` (`pending` (default) | `cleared` | `all`), `limit` (default 200).
Response: `{ "flags": [ { "opportunityIdentity", "status", "reason", "triggerKind",
"triggerRef", "entityIds", "flaggedRunId", "flaggedBy", "flaggedAt", "updatedAt",
"clearedRunId", "clearedAt" } ], "count": 1, "pending": 1 }`

Keyed on the stable `opportunityIdentity`, so a flag survives to the run that
re-evaluates it. A flag is cleared by the run that re-observed the finding and names
it in `clearedRunId` — a finding that stops appearing keeps its flag rather than being
treated as handled.

---

### F2) LLM Enrichment + Temporal/Entity Context (Screens 4 & 6)

#### GET /api/runs/{runId}/llm-enrichment
Purpose: enrichment status + executive summary for a run.
Response: `RunEnrichment` (`src/types/enrichment.ts`)
- Returns `{ ...defaults, available: false }` (HTTP 200, **not** 404) when
  enrichment has not been generated yet.

#### GET /api/runs/{runId}/opportunities/{oppId}/enrichment
Purpose: full enrichment for a single opportunity.
Response: `OppEnrichment` (`src/types/enrichment.ts`)
- Always returns a usable object — never 404 for *missing enrichment* (only for
  an unknown `runId`/`oppId`). Missing-LLM fallback returns the same shape with
  empty lists and the deterministic rationale surfaced as `aiSummary`.
- All list fields are always present (empty list when unavailable) so the UI
  never has to defensive-code around missing fields.

`OppEnrichment` shape (must match the TS type exactly):
```json
{
  "oppId": "opp_006",
  "aiSummary": "",
  "aiWhyBullets": [],
  "aiRisks": [],
  "aiSuggestedNextSteps": [],
  "llmGenerated": false,
  "llmModel": null,

  "baseline_context": null,
  "trend_direction": null,
  "anomaly_score": null,
  "is_anomalous": false,
  "first_deviation": false,
  "baseline_mean": null,
  "baseline_stddev": null,
  "baseline_window_days": null,
  "run_count": null,
  "current_value": null,
  "recent_values": [],
  "signal_key": null,
  "pack_id": null,

  "entities": [
    {
      "entity_id": "…",
      "entity_type": "person",
      "display_name": "…",
      "source_system": "jira",
      "resolution_confidence": 0.8,
      "resolution_status": "resolved"
    }
  ],
  "relationships": [],
  "causal_hypothesis": null,

  "llm_grounded": false,
  "graph_entity_count": 0,
  "graph_entity_count_shown": 0,
  "graph_truncated": false,
  "hallucination_removals": [],
  "hallucination_rewrites": 0,
  "hallucination_llm_rewrites": 0,
  "preliminary": true,
  "preliminary_reason": null,
  "corroboration_label": null
}
```

> ENT-3 / T3-S15-A fields (v1.3): `llm_grounded` is true when the first-pass
> prompt was grounded against the ENT-4 graph (>= 3 entities); the
> `graph_entity_count*` / `graph_truncated` fields reflect the 15-entity cap.
> `hallucination_*` report what the hallucination guard did to the why-bullets
> (`hallucination_removals` holds drop reason codes such as `dropped_timeout` /
> `dropped_generic`, never the dropped text). `preliminary` defaults to `true`
> ("analyst review required") until the three quality gates pass; when true,
> `preliminary_reason` carries the human-readable explanation rendered in the
> evidence trace. `corroboration_label` is carried through from ENT-2.

> ENT-6 / T3-S16-A field (v1.4): `causal_hypothesis` is the optional
> `CausalHypothesisSummary` for the opportunity, loaded live from the
> `causal_hypotheses` table (most-recent row, like `relationships` are read live
> from the graph). It is `null` when no causal hypothesis exists — absence is
> the normal state and distinct from an empty hypothesis. When present it always
> carries all six fields: `cause_chain` (ordered steps), `falsifiability_condition`,
> `confidence` (composite, 0.5–1.0), `inferred` (true when any step rests on an
> inferred relationship), `preliminary`, and `preliminary_reason`. The frontend
> branches on it: `null` → omit the section; `preliminary=true` → amber "analyst
> review required" banner with `preliminary_reason`; `preliminary=false` → full
> confirmed cause-chain rendering. Note this nested `preliminary`/
> `preliminary_reason` is the causal-gate status (ENT-6), distinct from the
> top-level `preliminary` (the ENT-3 enrichment gate).

> Casing note: the temporal/entity fields use `snake_case` (e.g.
> `baseline_stddev`, `recent_values`) — an intentional, documented exception to
> the camelCase frontend convention so the backend JSON maps directly to the TS
> type. `entities` items follow the `EntitySummary` shape and omit
> `canonical_name` (internal normalisation artifact, never exposed).

---

### G) Pilot Roadmap (Screen 9)

#### GET /api/runs/{runId}/roadmap
Response: `PilotRoadmapModel` (`src/types/pilotRoadmap.ts`)

---

### H) Executive Report (Screen 10)

#### GET /api/runs/{runId}/executive-report
Response:
```json
{
  "confidence": "High",
  "sourcesAnalyzed": {
    "recommendedConnected": 2,
    "totalConnected": 5,
    "uploadedFiles": 0,
    "sampleWorkspaceEnabled": false
  },
  "topQuickWins": [],
  "snapshotBubbles": [{ "x": 90, "y": 55, "r": 18 }],
  "roadmapHighlights": {
    "next30Count": 3,
    "next60Count": 2,
    "next90Count": 1,
    "blockerCount": 4
  },
  "aiExecutiveSummary": "",
  "outcomeSection": {
    "schemaVersion": "1.0.0",
    "runId": "run_123",
    "generatedFrom": "stored_movement_records",
    "summary": "Stored movement measurements are compared against baseline following recorded actions.",
    "aggregates": {
      "actionedOpportunityCount": 2,
      "measuredOpportunityCount": 2,
      "measurementCount": 2,
      "caveatedMeasurementCount": 1,
      "materialCaveatMeasurementCount": 0,
      "byDirection": { "improved": 1, "worsened": 1 },
      "byComparability": { "comparable": 1, "weakly_comparable": 1 },
      "byProjectionValidation": { "within_band": 1, "too_early": 1 },
      "numberRefs": []
    },
    "highlights": [],
    "numberRefs": []
  }
}
```

---

### I) Outcomes (2.0-A2 T6)

Outcome data is cross-run because each movement compares a frozen baseline run
with a current run. These routes are org-scoped and keyed by
`opportunity_identity`; they do not use a latest-run fallback and do not
recompute measurements on read. Requires authentication and Analyst role.
Unactioned and reopened opportunities are not outcome resources: the per-
opportunity outcome route returns 404 when the lifecycle has no current recorded
action, and portfolio/report aggregates exclude any movement rows invalidated by
action reversal.

#### GET /api/outcomes

Query filters:
`comparabilityVerdict[]`, `projectionVerdict[]`, `pack[]`, `detector[]`,
`confidence[]`, `limit`.

Response: `OutcomePortfolioView` (`frontend/src/types/outcome.ts`)
```json
{
  "schemaVersion": "1.0.0",
  "orgId": "org_123",
  "filters": {
    "comparabilityVerdict": [],
    "projectionVerdict": [],
    "pack": [],
    "detector": [],
    "confidence": []
  },
  "aggregates": {
    "actionedOpportunityCount": 4,
    "measuredOpportunityCount": 3,
    "measurementCount": 5,
    "caveatedMeasurementCount": 2,
    "materialCaveatMeasurementCount": 1,
    "byDirection": { "improved": 3, "worsened": 1, "unchanged": 1 },
    "byComparability": { "comparable": 3, "weakly_comparable": 2 },
    "byProjectionValidation": { "within_band": 2, "above_band": 1, "too_early": 2 },
    "numberRefs": [
      {
        "id": "aggregate:caveatedMeasurementCount",
        "label": "Measurements carrying caveats",
        "value": 2,
        "unit": "count",
        "evidence": {
          "measurementCount": 2,
          "runIds": ["run_baseline", "run_current"],
          "runPairs": [
            {
              "opportunityIdentity": "opp_stable_identity",
              "baselineRunId": "run_baseline",
              "currentRunId": "run_current"
            }
          ],
          "lifecycles": [
            {
              "opportunityIdentity": "opp_stable_identity",
              "state": "measured",
              "actionDate": "2026-06-15",
              "lastRunId": "run_current"
            }
          ]
        }
      }
    ]
  },
  "count": 4,
  "items": []
}
```

`caveatedMeasurementCount` is required on every aggregate response and is
computed at aggregation time. A missing field is a schema violation.

#### GET /api/outcomes/{opportunityIdentity}

Response: `OpportunityOutcomeView` (`frontend/src/types/outcome.ts`)
```json
{
  "schemaVersion": "1.0.0",
  "orgId": "org_123",
  "opportunityIdentity": "opp_stable_identity",
  "lifecycle": { "state": "measured", "actionDate": "2026-06-15" },
  "measurementCount": 1,
  "caveatedMeasurementCount": 0,
  "latestMeasurement": {
    "opportunityIdentity": "opp_stable_identity",
    "detectorId": "HANDOFF_FRICTION",
    "actionDate": "2026-06-15",
    "measuredAt": "2026-07-31T00:00:00+00:00",
    "baselineRunId": "run_baseline",
    "currentRunId": "run_current",
    "primaryMovement": {
      "signalName": "owner_changes_90d",
      "baselineValue": 240,
      "currentValue": 150,
      "delta": -90,
      "deltaPct": -37.5,
      "direction": "improved"
    },
    "comparability": { "verdict": "comparable", "reasons": [] },
    "projectionValidation": { "verdict": "within_band" },
    "confounderSummary": { "count": 0, "materialCount": 0 },
    "confounders": [],
    "numberRefs": [
      {
        "id": "opp_stable_identity:run_current:owner_changes_90d:delta",
        "label": "Movement against baseline",
        "value": -90,
        "unit": null,
        "signalName": "owner_changes_90d",
        "field": "delta",
        "evidence": {
          "opportunityIdentity": "opp_stable_identity",
          "signalName": "owner_changes_90d",
          "baselineRunId": "run_baseline",
          "currentRunId": "run_current",
          "postActionRunIds": ["run_current"],
          "baseline": { "runId": "run_baseline", "value": 240, "window": {} },
          "current": { "runId": "run_current", "value": 150, "window": {} },
          "confounderSummary": { "count": 0, "materialCount": 0 }
        }
      }
    ]
  },
  "measurements": [],
  "numberRefs": [],
  "emptyState": null
}
```

Outcome vocabulary across API, UI, report and export must be
movement-and-comparison shaped. The contract forbids outcome text that claims
causation, credit or financial return.

## DoD (Contract Freeze)
- Every `src/data/mock*.json` file is listed in `mock_to_endpoint_map.json` and mapped to an endpoint above.
- Every run-scoped endpoint includes `runId` in the URL (no latest-run fallback).


## Sign-off mechanism
Backend lead adds comment "Contract v1.1 approved — [name] [date]" to the contract PR before merge. Merge commit hash is the version anchor.
