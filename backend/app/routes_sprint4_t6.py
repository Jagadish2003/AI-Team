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
from . import db
from .llm_enrichment import KV_LLM_ENRICHMENT


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

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


def _full_fallback(opp_id: str, ai_rationale: str) -> OppEnrichment:
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
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_sprint4_t6_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/enrichment",
        response_model=OppEnrichment,
        dependencies=[Depends(require_auth)],
        tags=["runs"],
    )
    def get_opp_enrichment(run_id: str, opp_id: str) -> OppEnrichment:
        """
        Get LLM enrichment for a single opportunity.

        Always returns a usable OppEnrichment object:
        - If enrichment exists: returns LLM-generated fields
        - If enrichment missing: returns aiRationale as aiSummary, empty lists
        - Never returns 404 for missing enrichment (only for unknown runId/oppId)
        """
        _require_run(run_id)

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
            return _full_fallback(opp_id, opp.get("aiRationale", ""))

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
        )

    @app.get(
        "/api/runs/{run_id}/llm-enrichment",
        response_model=RunEnrichment,
        dependencies=[Depends(require_auth)],
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
