# AgentIQ — API_CONTRACT.md (EPIC E0)
Version: v1.16
Date: 2026-07-30

> v1.16 — 2.0-C1 (Pack Compatibility, Safe Disable & Rollback): documents the pack
> LIFECYCLE surface. All fields are additive and optional; every pre-v1.16 consumer
> is unaffected, and each field is absent on responses served before it existed.
>
> **New routes** (`app/routes_pack_state.py`):
> - `GET /api/packs/state` (viewer+) — `{ orgId, packs: PackStateItem[] }`, where
>   `PackStateItem` is `{ packId, packName, packVersion (string | null),
>   state ("active" | "disabled"), revision (number), reason (string | null),
>   updatedBy (string | null), updatedAt (string | null),
>   pinnedVersion (string | null), effectiveVersion (string | null),
>   availableVersions (string[]), registered (boolean) }`. `packVersion` is what the
>   registry currently ships; `effectiveVersion` is what a run started NOW would
>   execute and stamp; `availableVersions` are the rollback targets (empty ⇒ the pack
>   cannot be rolled back). `registered: false` marks an ORPHANED row — lifecycle
>   state for a pack no longer in the registry, retained so its history stays
>   reachable (AT-829); its version fields are `null`.
> - `PUT /api/packs/{packId}/state` (**owner**) — body `{ state: "active" |
>   "disabled", reason?: string }`. Idempotent (target state, not a verb). 404 for an
>   unknown pack.
> - `PUT /api/packs/{packId}/version` (**owner**) — body `{ version: string | null,
>   reason?: string }`; `null` clears the pin. **409** when the version has no
>   archived artifact, naming the versions that are available.
> - `GET /api/packs/{packId}/state/history` (analyst+) — `{ orgId, packId,
>   registered, transitions[] }`, newest-first, each transition
>   `{ id, revision, transition ("disable" | "enable" | "rollback" | "restore"),
>   previous_state, resulting_state, previous_version, resulting_version, reason,
>   actor_id, changed_at }`. Append-only: re-enabling does not erase the disable and
>   restoring does not erase the rollback. Serves a REMOVED pack's retained history
>   rather than 404-ing (AT-829); a genuinely unknown id is still 404.
>
> **Extended responses:**
> - `GET /api/runs/{runId}/opportunities` — `OpportunityCandidate` gains
>   `packState` (`"active" | "disabled"`) and `packStateLabel` (`string`), stamped at
>   serve time when the producing pack is disabled TODAY. The existing
>   `packVersion` still reports the version that produced the finding, so provenance
>   is intact; a finding is never removed or rewritten when its pack is disabled
>   (AT-827). Applies to every opportunity serve site sharing the display funnel
>   (list, decision, override, roadmap, executive report, blueprint).
> - `GET /api/run-health/packs` — each pack row gains `pack_state`
>   (`"active" | "disabled"`, read LIVE, unlike the immutable execution fields),
>   `pinned_version` (`string | null`) and `rolled_back` (`boolean`) (AT-828). The
>   response gains `excluded_packs` (`{ packId, state, reason }[]`) — packs selected
>   for the run that did not execute because the org has them disabled — and
>   `pinned_pack_versions` (`Record<string, string>`), the pins THIS run used.
> - `POST /api/stack-builder/launch` — `LaunchResponse` gains `excludedPacks`
>   (`{ packId, state, reason }[]`). `packIds` already excludes them, so a caller
>   ignoring the field still sees the truthful pack set; this names what was dropped.
> - Both `POST /api/stack-builder/launch` and `POST /api/runs/{runId}/compute` may
>   now return **409** when a selected pack declares an unmet platform-capability
>   range (AT-826, the detail names the unmet requirement) or when EVERY selected
>   pack is disabled (AT-827).

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

### H) Executive Report Stub (Screen 10)

#### GET /api/runs/{runId}/executive-report
Response (v1 stub shape):
```json
{
  "confidence": "High",
  "sourcesAnalyzed": { "recommendedConnected": 2, "totalConnected": 5 },
  "topQuickWins": [],
  "snapshotBubbles": [{ "x": 90, "y": 55, "r": 18 }],
  "roadmapHighlights": { "next30Count": 3, "next60Count": 2, "next90Count": 1, "blockerCount": 4 }
}
```

---

## DoD (Contract Freeze)
- Every `src/data/mock*.json` file is listed in `mock_to_endpoint_map.json` and mapped to an endpoint above.
- Every run-scoped endpoint includes `runId` in the URL (no latest-run fallback).


## Sign-off mechanism
Backend lead adds comment "Contract v1.1 approved — [name] [date]" to the contract PR before merge. Merge commit hash is the version anchor.
