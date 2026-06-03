# AgentIQ — API_CONTRACT.md (EPIC E0)
Version: v1.2
Date: 2026-06-03

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

### G) Pilot Roadmap (Screen 9)

#### GET /api/runs/{runId}/roadmap
Response: `PilotRoadmapModel` (`src/types/pilotRoadmap.ts`)

---

### H) Executive Report Stub (Screen 10)

#### GET /api/runs/{runId}/executive-report
Response (v1 stub shape):
```json
{
  "confidence": "Moderate",
  "sourcesAnalyzed": { "recommendedConnected": 2, "totalConnected": 5 },
  "topQuickWins": [],
  "snapshotBubbles": [{ "x": 90, "y": 55, "r": 18 }],
  "roadmapHighlights": { "next30Count": 3, "next60Count": 2, "next90Count": 1, "blockerCount": 4 }
}
```

---

### I) Run Lifecycle & Status (added v1.2)

#### GET /api/runs/{runId}/status
Purpose: poll current status of a discovery run.
Response:
```json
{ "runId": "string", "status": "running|complete|error", "progress": 0.0 }
```

#### POST /api/runs/{runId}/compute
Purpose: trigger T2 materialization for a completed run.
Response: `{ "ok": true }`

#### GET /api/runs/{runId}/connector-health
Purpose: return connector health status scoped to a run.
Response: `ConnectorHealth[]` (see `src/types/connectorHealth.ts`)

---

### J) Clusters (added v1.2)

#### GET /api/runs/{runId}/clusters
Purpose: return opportunity clusters for a run.
Response: `OpportunityCluster[]` (see `src/types/clusters.ts`)

---

### K) LLM Enrichment (added v1.2)

#### GET /api/runs/{runId}/llm-enrichment
Purpose: return aggregate LLM enrichment summary for a run.
Response: `LLMEnrichmentSummary` (see `src/types/enrichment.ts`)

#### GET /api/runs/{runId}/opportunities/{oppId}/enrichment
Purpose: return per-opportunity LLM enrichment detail.
Response: `OppEnrichment` (see `src/types/enrichment.ts`)

---

### L) Blueprint (added v1.2)

#### GET /api/runs/{runId}/opportunities/{oppId}/blueprint
Purpose: return agent blueprint for a specific opportunity.
Response: `AgentBlueprint` (see `src/types/blueprint.ts`)

---

### M) Normalization (added v1.2)

#### GET /api/runs/{runId}/normalization
Purpose: return field normalization mapping results for a run.
Response:
```json
{
  "runId": "string",
  "rows": "MappingRow[]",
  "counts": { "MAPPED": 0, "UNMAPPED": 0, "AMBIGUOUS": 0 },
  "source": "stored|derived"
}
```
Note: response is a wrapper object, not a bare `MappingRow[]` array.

---

### N) Temporal Signals (added v1.2)

#### GET /api/runs/{runId}/signals
Purpose: return temporal signal data for a run.
Response: `TemporalSignal[]` (see `src/types/temporal.ts`)

---

## DoD (Contract Freeze)
- Every `src/data/mock*.json` file is listed in `mock_to_endpoint_map.json` and mapped to an endpoint above.
- Every run-scoped endpoint includes `runId` in the URL (no latest-run fallback).


## Sign-off mechanism
Backend lead adds comment "Contract v1.1 approved — [name] [date]" to the contract PR before merge. Merge commit hash is the version anchor.
