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

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .security import require_auth
from .rbac import require_role
from . import db
from .llm_enrichment import KV_LLM_ENRICHMENT
from .temporal import get_baseline, get_signal_history
from .graph_query import RelationshipSummary, select_relationships

logger = logging.getLogger(__name__)

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
    baseline_stddev:      Optional[float] = None
    baseline_window_days: Optional[int] = None
    run_count:            Optional[int] = None
    current_value:        Optional[float] = None
    recent_values:        List[float] = Field(default_factory=list)
    signal_key:           Optional[str] = None
    pack_id:              Optional[str] = None
    # Track 3 Stage 2 — T3-S12-A entity summaries
    # default_factory=list ensures backward compat for code that constructs
    # OppEnrichment without an entity list (existing tests, fallback paths).
    entities:             List[EntitySummary] = Field(default_factory=list)
    # Track 3 Stage 2 — T3-S13-A relationship edges.
    # Flag-gated (INFERRED_RELATIONSHIPS_ENABLED): observed-only by default,
    # observed + inferred when the flag is on. default_factory=list keeps the
    # field present (empty) for fallback paths and runs without a graph.
    relationships:        List[RelationshipSummary] = Field(default_factory=list)


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


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float_list(value: Any) -> List[float]:
    if not isinstance(value, list):
        return []
    result: List[float] = []
    for item in value:
        number = _safe_float(item)
        if number is not None:
            result.append(number)
    return result


def _pack_id_for_run(run: Dict[str, Any]) -> Optional[str]:
    inputs = run.get("inputs") or {}
    input_pack_id = inputs.get("packId") if isinstance(inputs, dict) else None
    return input_pack_id or run.get("packId") or run.get("pack_id")


def _org_candidates_for_run(run: Dict[str, Any]) -> List[str]:
    inputs = run.get("inputs") or {}
    candidates: List[Optional[str]] = [
        run.get("orgId"),
        run.get("org_id"),
        inputs.get("orgId") if isinstance(inputs, dict) else None,
        inputs.get("org_id") if isinstance(inputs, dict) else None,
        "demo-org",
        "default",
    ]
    result: List[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _find_stored_opp(run_id: str, opp_id: str) -> Optional[Dict[str, Any]]:
    opps = db.run_kv_get("opps", run_id, []) or []
    return next((o for o in opps if isinstance(o, dict) and o.get("id") == opp_id), None)


def _temporal_payload(
    run: Dict[str, Any],
    run_id: str,
    opp_id: str,
    opp_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    opp_data = opp_data or {}
    stored_opp = _find_stored_opp(run_id, opp_id) or {}
    debug = stored_opp.get("_debug", {}) if isinstance(stored_opp, dict) else {}
    if not isinstance(debug, dict):
        debug = {}

    pack_id = opp_data.get("pack_id") or stored_opp.get("packId") or _pack_id_for_run(run)
    detector_id = debug.get("detector_id")
    current_value = _safe_float(
        opp_data.get("current_value")
        if opp_data.get("current_value") is not None
        else stored_opp.get("metric_value", debug.get("metric_value"))
    )
    signal_key = opp_data.get("signal_key")
    if not signal_key and pack_id and detector_id:
        signal_key = f"{pack_id}::{detector_id}::metric_value"

    recent_values = _safe_float_list(opp_data.get("recent_values"))
    baseline_mean = _safe_float(opp_data.get("baseline_mean"))
    baseline_stddev = _safe_float(opp_data.get("baseline_stddev"))
    baseline_window_days = _safe_int(opp_data.get("baseline_window_days"))
    run_count = _safe_int(opp_data.get("run_count"))

    if detector_id:
        for org_id in _org_candidates_for_run(run):
            if signal_key and not recent_values:
                try:
                    rows = get_signal_history(org_id, detector_id, signal_key, limit=5)
                    recent_values = [
                        value
                        for value in (
                            _safe_float(row.get("metric_value", row.get("value")))
                            for row in reversed(rows)
                            if isinstance(row, dict)
                        )
                        if value is not None
                    ]
                except Exception:
                    recent_values = []

            if (
                baseline_mean is None
                or baseline_stddev is None
                or baseline_window_days is None
                or run_count is None
            ):
                try:
                    baseline = get_baseline(org_id, detector_id)
                except Exception:
                    baseline = None
                if isinstance(baseline, dict):
                    baseline_mean = baseline_mean if baseline_mean is not None else _safe_float(baseline.get("baseline_mean"))
                    baseline_stddev = baseline_stddev if baseline_stddev is not None else _safe_float(baseline.get("baseline_stddev"))
                    baseline_window_days = baseline_window_days if baseline_window_days is not None else _safe_int(baseline.get("baseline_window_days"))
                    run_count = run_count if run_count is not None else _safe_int(baseline.get("run_count"))

            if recent_values or baseline_window_days is not None:
                break

    if current_value is not None and not recent_values:
        recent_values = [current_value]

    return {
        "baseline_mean": baseline_mean,
        "baseline_stddev": baseline_stddev,
        "baseline_window_days": baseline_window_days,
        "run_count": run_count,
        "current_value": current_value,
        "recent_values": recent_values,
        "signal_key": signal_key,
        "pack_id": pack_id,
    }


def _full_fallback(
    opp_id: str,
    ai_rationale: str,
    entities: Optional[List[EntitySummary]] = None,
    temporal: Optional[Dict[str, Any]] = None,
    relationships: Optional[List[RelationshipSummary]] = None,
) -> OppEnrichment:
    """
    Fix 6: Return the full OppEnrichment shape on fallback.
    All list fields are empty lists — consistent with LLM-generated shape.
    """
    temporal = temporal or {}
    return OppEnrichment(
        oppId=opp_id,
        aiSummary=ai_rationale,
        aiWhyBullets=[],
        aiRisks=[],
        aiSuggestedNextSteps=[],
        llmGenerated=False,
        llmModel=None,
        baseline_mean=temporal.get("baseline_mean"),
        baseline_stddev=temporal.get("baseline_stddev"),
        baseline_window_days=temporal.get("baseline_window_days"),
        run_count=temporal.get("run_count"),
        current_value=temporal.get("current_value"),
        recent_values=temporal.get("recent_values", []),
        signal_key=temporal.get("signal_key"),
        pack_id=temporal.get("pack_id"),
        entities=entities or [],
        relationships=relationships or [],
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


def _load_relationship_summaries(run_id: str) -> List[RelationshipSummary]:
    """Load relationship edges for the run's graph, flag-gated (T3-S13-A T5).

    select_relationships() applies INFERRED_RELATIONSHIPS_ENABLED at population
    time: observed-only by default, observed + inferred when the flag is on.
    org_id is taken from the request tenancy context so queries stay org-scoped.

    Never raises — relationship surfacing is advisory and must not break the
    enrichment response. Returns an empty list when the graph is empty, the
    org context is missing, or any query error occurs.
    """
    try:
        from app.middleware.tenancy import get_current_org_id
        org_id = get_current_org_id()
    except Exception as exc:
        logger.debug("relationship load skipped — org context unavailable: %s", exc)
        return []
    try:
        return select_relationships(org_id, run_id)
    except Exception as exc:
        logger.debug("relationship load failed for run %s: %s", run_id, exc)
        return []


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
        run = _require_run(run_id)

        # Load entity summaries once for this run (shared across all opportunities).
        # Service-account filter (run_count < 3) is applied here per spec Section 8.
        entity_summaries = _load_entity_summaries(run_id)

        # Load relationship edges once for this run. Flag-gated: observed-only by
        # default, observed + inferred when INFERRED_RELATIONSHIPS_ENABLED is on.
        relationship_summaries = _load_relationship_summaries(run_id)

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
            temporal = _temporal_payload(run, run_id, opp_id)
            return _full_fallback(
                opp_id,
                opp.get("aiRationale", ""),
                entity_summaries,
                temporal,
                relationship_summaries,
            )

        per_opp  = enrichment.get("perOpportunity", {})
        opp_data = per_opp.get(opp_id)

        if opp_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in enrichment for run '{run_id}'"
            )

        temporal = _temporal_payload(run, run_id, opp_id, opp_data)

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
            baseline_mean=temporal.get("baseline_mean"),
            baseline_stddev=temporal.get("baseline_stddev"),
            baseline_window_days=temporal.get("baseline_window_days"),
            run_count=temporal.get("run_count"),
            current_value=temporal.get("current_value"),
            recent_values=temporal.get("recent_values", []),
            signal_key=temporal.get("signal_key"),
            pack_id=temporal.get("pack_id"),
            entities=entity_summaries,
            relationships=relationship_summaries,
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
