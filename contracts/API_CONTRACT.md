# AgentIQ — API_CONTRACT.md (EPIC E0)
Version: v1.4
Date: 2026-06-10

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
    "action": "OVERRIDE_SAVED",
    "by": "Architect Name"
  }
]
```

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
