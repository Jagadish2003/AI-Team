"""Temporal signal history API routes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from .temporal import get_baseline, get_run_signals, get_signal_history

TEMPORAL_HISTORY_PATH = "/api/temporal/{detector_id}/history"
TEMPORAL_BASELINE_PATH = "/api/temporal/{detector_id}/baseline"
RUN_SIGNALS_PATH = "/api/runs/{run_id}/signals"
TEMPORAL_TREND_PATH = "/api/temporal/{detector_id}/trend"
RUN_TEMPORAL_CONTEXT_PATH = "/api/runs/{run_id}/temporal-context"
TEMPORAL_ROUTE_PATHS = {
    TEMPORAL_HISTORY_PATH,
    TEMPORAL_BASELINE_PATH,
    RUN_SIGNALS_PATH,
    TEMPORAL_TREND_PATH,
    RUN_TEMPORAL_CONTEXT_PATH,
}

analyst_dependencies = [Depends(require_auth), Depends(require_role("analyst"))]

router = APIRouter(tags=["temporal"])


@router.get(TEMPORAL_HISTORY_PATH, dependencies=analyst_dependencies)
def temporal_signal_history(
    detector_id: str,
    signal_key: str = Query("metric_value"),
    limit: int = Query(10, ge=1, le=50),
) -> List[Dict[str, Any]]:
    org_id = get_current_org_id()
    rows = get_signal_history(org_id, detector_id, signal_key, limit)
    if not rows:
        raise HTTPException(status_code=404, detail="signal history not found")
    return rows


@router.get(TEMPORAL_BASELINE_PATH, dependencies=analyst_dependencies)
def temporal_baseline(
    detector_id: str,
) -> Dict[str, Any]:
    org_id = get_current_org_id()
    baseline = get_baseline(org_id, detector_id)
    if baseline is None:
        raise HTTPException(status_code=404, detail="baseline not found")
    return {
        "baseline_mean": baseline.get("baseline_mean"),
        "baseline_stddev": baseline.get("baseline_stddev"),
        "baseline_window_days": baseline.get("baseline_window_days"),
        "calculated_at": baseline.get("calculated_at"),
        "run_count": baseline.get("run_count"),
        "insufficient_data": baseline.get("insufficient_data"),
    }


@router.get(RUN_SIGNALS_PATH, dependencies=analyst_dependencies)
def run_signals(
    run_id: str,
) -> List[Dict[str, Any]]:
    org_id = get_current_org_id()
    return get_run_signals(org_id, run_id)


@router.get(TEMPORAL_TREND_PATH, dependencies=analyst_dependencies)
def temporal_trend(
    detector_id: str,
    pack_id: str = Query("pack"),
) -> Dict[str, Any]:
    """Return trend result for the detector's primary signal.

    org_id is derived from tenancy context — not accepted as a parameter.
    """
    from .middleware.tenancy import get_current_org_id
    from .trend_engine import calculate_trend

    org_id = get_current_org_id()
    signal_key = f"{pack_id}::{detector_id}::metric_value"
    result = calculate_trend(org_id, signal_key)
    return {
        "trend_direction": result.trend_direction,
        "slope": result.slope,
        "slope_pct": result.slope_pct,
        "r_squared": result.r_squared,
        "run_count": result.run_count,
        "signal_key": result.signal_key,
    }


@router.get(RUN_TEMPORAL_CONTEXT_PATH, dependencies=analyst_dependencies)
def run_temporal_context(run_id: str) -> List[Dict[str, Any]]:
    """Return temporal context for all opportunities in the run.

    org_id is derived from tenancy context — not accepted as a parameter.
    Returns 404 if the run belongs to a different org.
    """
    from . import db
    from .llm_enrichment import KV_LLM_ENRICHMENT
    from .middleware.tenancy import get_current_org_id

    org_id = get_current_org_id()

    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    run_org = run.get("org_id") or run.get("orgId")
    if run_org and run_org != org_id:
        raise HTTPException(status_code=404, detail="run not found")

    enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, {})
    per_opp: Dict[str, Any] = enrichment.get("perOpportunity", {})

    temporal_keys = (
        "baseline_context", "trend_direction", "anomaly_score",
        "is_anomalous", "first_deviation", "baseline_mean", "run_count",
        "baseline_stddev", "baseline_window_days", "current_value",
        "recent_values", "signal_key", "pack_id",
    )
    result: List[Dict[str, Any]] = []
    for opp_id, opp_data in per_opp.items():
        entry: Dict[str, Any] = {"opp_id": opp_id}
        for k in temporal_keys:
            entry[k] = opp_data.get(k)
        result.append(entry)

    return result


def register_temporal_routes(app: FastAPI) -> None:
    """Register temporal routes once for the provided FastAPI app."""

    if getattr(app.state, "temporal_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if TEMPORAL_ROUTE_PATHS.issubset(existing_paths):
        app.state.temporal_routes_registered = True
        return

    app.include_router(router)
    app.state.temporal_routes_registered = True
