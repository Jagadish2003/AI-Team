"""
Sprint 4 T6 — LLM Enrichment Routes  v1.1

Changes from v1.0:
  Fix 4: Removed claim about GET /executive-report/summary endpoint which
          was not implemented. Executive summary is served via the existing
          GET /executive-report endpoint's aiExecutiveSummary field.
  Fix 5: Registration order documented correctly — after T4.
  Fix 6: Fallback OppEnrichment now returns the full model shape with all
          list fields as empty lists, matching the LLM-generated shape.

Adds two endpoints:
  GET /api/runs/{runId}/llm-enrichment
      Returns enrichment status and executive summary for a run.
      Returns available: false (not 404) if enrichment not yet generated.

  GET /api/runs/{runId}/opportunities/{oppId}/enrichment
      Returns LLM fields for a single opportunity.
      Never returns 404 for missing enrichment — always returns usable
      fallback (aiRationale as aiSummary, empty lists for bullet fields).

Wire-in (main.py):
  from .routes_sprint4_t6 import register_sprint4_t6_routes
  register_sprint4_t6_routes(app)

  Add to the registration block after register_sprint4_t4_routes(app).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .security import require_auth
from .rbac import require_role
from . import db
from .llm_enrichment import KV_LLM_ENRICHMENT

_MIN_ENTITY_RUN_COUNT = 3  # service-account filter threshold (Section 8)


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

class EntitySummary(BaseModel):
    """
    Lightweight entity representation surfaced in the evidence trace.
    display_name is the original source name — canonical_name is never exposed.
    resolution_status='ambiguous' signals the UI to render with muted styling.
    """
    entity_id:             str
    entity_type:           str
    display_name:          str
    source_system:         str
    resolution_confidence: float
    resolution_status:     str  # 'resolved' | 'ambiguous'


class OppEnrichment(BaseModel):
    """
    Full enrichment shape for a single opportunity.
    All list fields are always present — empty lists when LLM not available.
    This consistent shape prevents UI defensive-coding against missing fields.
    """
    oppId:                str
    aiSummary:            str = ""
    aiWhyBullets:         List[str] = Field(default_factory=list)
    aiRisks:              List[str] = Field(default_factory=list)
    aiSuggestedNextSteps: List[str] = Field(default_factory=list)
    llmGenerated:         bool = False
    llmModel:             Optional[str] = None
    # Track 3 Stage 1 — T3-S11-A temporal fields
    baseline_context:     Optional[str] = None
    trend_direction:      Optional[str] = None
    anomaly_score:        Optional[float] = None
    is_anomalous:         bool = False
    first_deviation:      bool = False
    baseline_mean:        Optional[float] = None
    run_count:            Optional[int] = None
    # Track 3 Stage 2 — T3-S12-A entity summaries
    # default_factory=list ensures backward compat for code that constructs
    # OppEnrichment without an entity list (existing tests, fallback paths).
    entities:             List[EntitySummary] = Field(default_factory=list)


class RunEnrichment(BaseModel):
    runId:                  str
    executiveSummary:       str = ""
    opportunitiesEnriched:  int = 0
    opportunitiesFailed:    int = 0
    generatedAt:            Optional[str] = None
    llmModel:               Optional[str] = None
    available:              bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_run(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _full_fallback(
    opp_id: str,
    ai_rationale: str,
    entities: Optional[List[EntitySummary]] = None,
) -> OppEnrichment:
    """
    Fix 6: Return the full OppEnrichment shape on fallback.
    All list fields are empty lists — consistent with LLM-generated shape.
    """
    return OppEnrichment(
        oppId=opp_id,
        aiSummary=ai_rationale,
        aiWhyBullets=[],
        aiRisks=[],
        aiSuggestedNextSteps=[],
        llmGenerated=False,
        llmModel=None,
        entities=entities or [],
    )


def _load_entity_summaries(run_id: str) -> List[EntitySummary]:
    """Load entity summaries from run KV and apply the service-account filter.

    The KV store holds entities pre-populated by entity_extractor after each run.
    Service-account filter (run_count < 3) is applied here per Section 8 spec —
    low-count entities remain in the DB for graph completeness but are hidden
    from the evidence trace.
    """
    raw: List[Dict[str, Any]] = db.run_kv_get("entities", run_id, []) or []
    summaries: List[EntitySummary] = []
    for e in raw:
        if e.get("run_count", 0) < _MIN_ENTITY_RUN_COUNT:
            continue
        try:
            summaries.append(EntitySummary(
                entity_id=e["entity_id"],
                entity_type=e["entity_type"],
                display_name=e["display_name"],
                source_system=e["source_system"],
                resolution_confidence=float(e["resolution_confidence"]),
                resolution_status=e["resolution_status"],
            ))
        except (KeyError, ValueError, TypeError):
            pass  # malformed entry — skip silently
    return summaries


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_sprint4_t6_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/enrichment",
        response_model=OppEnrichment,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_opp_enrichment(run_id: str, opp_id: str) -> OppEnrichment:
        """
        Get LLM enrichment for a single opportunity.

        Always returns a usable OppEnrichment object:
        - If enrichment exists: returns LLM-generated fields
        - If enrichment missing: returns aiRationale as aiSummary, empty lists
        - Never returns 404 for missing enrichment (only for unknown runId/oppId)
        - entities field is always present — populated from run KV when available
        """
        _require_run(run_id)

        # Load entity summaries once for this run (shared across all opportunities).
        # Service-account filter (run_count < 3) is applied here per spec Section 8.
        entity_summaries = _load_entity_summaries(run_id)

        enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, None)

        # Enrichment not yet generated — serve fallback from stored opps
        if enrichment is None:
            opps = db.run_kv_get("opps", run_id, [])
            opp  = next((o for o in opps if o.get("id") == opp_id), None)
            if opp is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Opportunity '{opp_id}' not found in run '{run_id}'"
                )
            return _full_fallback(opp_id, opp.get("aiRationale", ""), entity_summaries)

        per_opp  = enrichment.get("perOpportunity", {})
        opp_data = per_opp.get(opp_id)

        if opp_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in enrichment for run '{run_id}'"
            )

        return OppEnrichment(
            oppId=opp_id,
            aiSummary=opp_data.get("aiSummary", ""),
            aiWhyBullets=opp_data.get("aiWhyBullets", []),
            aiRisks=opp_data.get("aiRisks", []),
            aiSuggestedNextSteps=opp_data.get("aiSuggestedNextSteps", []),
            llmGenerated=opp_data.get("llmGenerated", False),
            llmModel=opp_data.get("llmModel"),
            baseline_context=opp_data.get("baseline_context"),
            trend_direction=opp_data.get("trend_direction"),
            anomaly_score=opp_data.get("anomaly_score"),
            is_anomalous=opp_data.get("is_anomalous", False),
            first_deviation=opp_data.get("first_deviation", False),
            baseline_mean=opp_data.get("baseline_mean"),
            run_count=opp_data.get("run_count"),
            entities=entity_summaries,
        )

    @app.get(
        "/api/runs/{run_id}/llm-enrichment",
        response_model=RunEnrichment,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_run_enrichment(run_id: str) -> RunEnrichment:
        """
        Get LLM enrichment status and executive summary for a run.
        Returns available: false if enrichment not yet generated — not 404.
        """
        _require_run(run_id)
        enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, None)

        if enrichment is None:
            return RunEnrichment(runId=run_id, available=False)

        return RunEnrichment(
            runId=run_id,
            executiveSummary=enrichment.get("executiveSummary", ""),
            opportunitiesEnriched=enrichment.get("opportunitiesEnriched", 0),
            opportunitiesFailed=enrichment.get("opportunitiesFailed", 0),
            generatedAt=enrichment.get("generatedAt"),
            llmModel=enrichment.get("llmModel"),
            available=True,
        )
