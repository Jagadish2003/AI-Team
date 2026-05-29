"""Temporal signal history API routes."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .security import require_auth, require_role
from .temporal import get_baseline, get_run_signals, get_signal_history

TEMPORAL_HISTORY_PATH = "/api/temporal/{detector_id}/history"
TEMPORAL_BASELINE_PATH = "/api/temporal/{detector_id}/baseline"
RUN_SIGNALS_PATH = "/api/runs/{run_id}/signals"
TEMPORAL_ROUTE_PATHS = {
    TEMPORAL_HISTORY_PATH,
    TEMPORAL_BASELINE_PATH,
    RUN_SIGNALS_PATH,
}

analyst_dependencies = [Depends(require_auth), Depends(require_role("analyst"))]

router = APIRouter(tags=["temporal"])


@router.get(TEMPORAL_HISTORY_PATH, dependencies=analyst_dependencies)
def temporal_signal_history(
    detector_id: str,
    org_id: str = Query("org_default"),
    signal_key: str = Query("metric_value"),
    limit: int = Query(10, ge=1, le=50),
) -> List[Dict[str, Any]]:
    rows = get_signal_history(org_id, detector_id, signal_key, limit)
    if not rows:
        raise HTTPException(status_code=404, detail="signal history not found")
    return rows


@router.get(TEMPORAL_BASELINE_PATH, dependencies=analyst_dependencies)
def temporal_baseline(
    detector_id: str,
    org_id: str = Query("org_default"),
) -> Dict[str, Any]:
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
    org_id: str = Query("org_default"),
) -> List[Dict[str, Any]]:
    return get_run_signals(org_id, run_id)


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
