"""
SHARED-2 — Sprint 5 — nCino Lending Entity Extension
GET /api/runs/{runId}/normalization

Returns normalization mapping rows for a specific run.

Data source priority:
  1. Run KV key "normalization" — written by normalization_enrichment.py
     when enrich_ambiguous_mappings() is wired into the runner.
  2. Derived from run evidence and opportunity data — always available
     as a fallback, producing real rows from actual ingested data.

This endpoint replaces the frontend mockMappings.json. The context
(NormalizationContext.tsx) calls this endpoint using the current runId.

Wire-in (main.py):
    from .routes_normalization import register_normalization_routes
    register_normalization_routes(app)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from .security import require_auth
from . import db

from .normalization_enrichment import KV_NORMALIZATION  # shared key — Issue 1 fix
KV_EVIDENCE = "evidence"

# ── Response models ───────────────────────────────────────────────────────────

class MappingRowOut(BaseModel):
    id: str
    sourceSystem: str
    sourceType: str
    sourceField: str
    commonEntity: str
    commonField: str
    status: str        # MAPPED | UNMAPPED | AMBIGUOUS
    confidence: str    # HIGH | MEDIUM | LOW
    sampleValues: List[str]
    notes: str = ""


class NormalizationResponse(BaseModel):
    runId: str
    rows: List[MappingRowOut]
    counts: Dict[str, int]
    source: str   # "stored" | "derived" — tells frontend how data was produced


# ── Entity type derivation ────────────────────────────────────────────────────
# SHARED-2 Issue 3 fix: derive lending entity type from detector_id, not from
# fragile source string conventions. The detector_id is set explicitly by each
# nCino detector and is always present on evidence items.

_DETECTOR_ENTITY_MAP: dict = {
    # nCino lending detectors → lending canonical entity types
    "LOAN_ORIGINATION_ROUTING_FRICTION": "Loan",
    "STAGE_DURATION_OVERRUN":            "Loan",
    "COVENANT_TRACKING_GAP":             "Covenant",
    "CHECKLIST_BOTTLENECK":              "Checklist",
    "SPREADING_BOTTLENECK":              "SpreadPeriod",
    "APPROVAL_BOTTLENECK":               "LendingApproval",
    # Service Cloud detectors → Service Cloud entity types (original)
    "CASE_ROUTING_FRICTION":             "Workflow",
    "REPEATED_ESCALATIONS":              "Workflow",
    "PERMISSION_BOTTLENECK":             "Application",
    "REPETITIVE_AUTOMATION":             "Service",
}

_SOURCE_ENTITY_MAP: dict = {
    # Fallback: source-level entity type when no detector_id match
    "Salesforce":  "Workflow",
    "ServiceNow":  "Application",
    "Jira":        "Workflow",
    "Databricks":  "Service",
}


def _entity_from_detector(detector_id_or_ev_type: str, source: str) -> str:
    """
    Derive canonical entity type from detector_id (preferred) or source (fallback).

    Issue 3 fix: detector_id is explicit and not fragile. Source-string
    conventions (nCino:Loan etc.) are no longer required.
    """
    # Try detector_id first (explicit, reliable)
    entity = _DETECTOR_ENTITY_MAP.get(detector_id_or_ev_type)
    if entity:
        return entity
    # Fall back to source-level map (Service Cloud behaviour preserved)
    return _SOURCE_ENTITY_MAP.get(source, "DataObject")


# ── Derivation from evidence ──────────────────────────────────────────────────

def _derive_from_evidence(run_id: str) -> List[Dict[str, Any]]:
    """
    Derive normalization rows from the run's evidence items.
    Evidence items have source, evidenceType, title, snippet — enough
    to produce representative mapping rows showing what each source
    contributed to the run.

    This is the fallback when no stored normalization data exists.
    Rows are MAPPED with confidence derived from the evidence confidence.
    """
    evidence: List[Dict[str, Any]] = db.run_kv_get(KV_EVIDENCE, run_id, [])
    if not evidence:
        return []

    rows: List[Dict[str, Any]] = []
    seen: set = set()

    for ev in evidence:
        source = ev.get("source", "")
        # Issue 2 fix: evidenceType is always "Metric"/"Log" — not a detector ID.
        # Read detectorId if present (set by nCino evidence enrichment path),
        # fall back to evidenceType for backward compat with Service Cloud evidence.
        ev_type = ev.get("detectorId") or ev.get("evidenceType", "Metric")
        title = ev.get("title", "")
        confidence = ev.get("confidence", "MEDIUM")
        ev_id = ev.get("id", "")

        # Derive a representative field name from the evidence title
        # Limit to one row per source+type combination to avoid flooding
        key = (source, ev_type)
        if key in seen:
            continue
        seen.add(key)

        # Map evidenceType to a sourceType label
        source_type_map = {
            "Metric":  "CRM" if source == "Salesforce" else "ITSM" if source == "ServiceNow" else "Tickets",
            "Log":     "Events",
            "Event":   "Events",
            "Email":   "Email",
            "Ticket":  "Tickets",
            "Chat":    "Chat",
            "Doc":     "Documentation",
        }
        source_type = source_type_map.get(ev_type, ev_type)

        # Derive entity type.
        # SHARED-2 Issue 3 fix: use detector_id (from evidenceType field) to
        # derive lending entity type. This is explicit and not fragile — the
        # detector_id is set by the nCino detector and does not depend on
        # source string conventions.
        entity = _entity_from_detector(ev_type, source)

        rows.append({
            "id":           f"norm_{ev_id}",
            "sourceSystem": source,
            "sourceType":   source_type,
            "sourceField":  f"{source.lower()}.{ev_type.lower()}_signal",
            "commonEntity": entity,
            "commonField":  f"{entity}.{ev_type.lower()}",
            "status":       "MAPPED",
            "confidence":   confidence,
            "sampleValues": [],
            "notes":        f"Derived from evidence: {title[:60]}",
        })

    return rows


# ── Route registration ────────────────────────────────────────────────────────

def register_normalization_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/normalization",
        response_model=NormalizationResponse,
        dependencies=[Depends(require_auth)],
        tags=["normalization"],
    )
    def get_run_normalization(run_id: str) -> NormalizationResponse:
        """
        Return normalization mapping rows for a run.

        Priority:
          1. Stored normalization data (from enrich_ambiguous_mappings)
          2. Derived from evidence items in the run

        Returns 404 if the run does not exist.
        """
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )

        # Priority 1: stored normalization data
        stored = db.run_kv_get(KV_NORMALIZATION, run_id, None)
        # Issue 1 fix: stored may be a dict {"rows": [...], ...metadata}
        # or a legacy list. Handle both.
        stored_rows = None
        if stored:
            if isinstance(stored, dict) and stored.get("rows"):
                stored_rows = stored["rows"]
            elif isinstance(stored, list) and len(stored) > 0:
                stored_rows = stored  # legacy list shape
        if stored_rows:
            rows = stored_rows
            data_source = "stored"
        else:
            # Priority 2: derive from evidence
            rows = _derive_from_evidence(run_id)
            data_source = "derived"

        # Compute counts
        counts: Dict[str, int] = {"MAPPED": 0, "UNMAPPED": 0, "AMBIGUOUS": 0}
        for row in rows:
            status = row.get("status", "UNMAPPED")
            if status in counts:
                counts[status] += 1

        return NormalizationResponse(
            runId=run_id,
            rows=[MappingRowOut(**r) for r in rows],
            counts=counts,
            source=data_source,
        )
