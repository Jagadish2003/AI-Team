"""2.0-A2 T3 — the read-only movement API.

Read-only: the pipeline measures when a run lands, and a movement record is a
stored artifact rather than something a client asks to have recomputed. Exposing a
write verb would let a caller re-derive a measurement outside the run that
produced it, which is exactly what storing the record prevents.

Standard spine: org from the tenancy middleware only, ``require_role("analyst")``
on every route, cross-org identities answer 404 identically to missing ones.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .opportunity_movement import (
    ensure_opportunity_movement_table,
    get_movement_history,
    get_movements_for_run,
    list_movements,
)
from .opportunity_movement_record import (
    VERDICT_COMPARABLE,
    VERDICT_NOT_COMPARABLE,
    VERDICT_WEAK,
)
from .projection_validation import PROJECTION_VALIDATION_VERDICTS
from .rbac import require_role
from .security import require_auth

router = APIRouter(prefix="/api/opportunity-movement", tags=["opportunity-movement"])

_VERDICTS = (VERDICT_COMPARABLE, VERDICT_WEAK, VERDICT_NOT_COMPARABLE)
_CONFIDENCES = ("LOW", "MEDIUM", "HIGH")


@router.get("", dependencies=[Depends(require_role("analyst"))])
def list_opportunity_movements(
    verdict: Optional[List[str]] = Query(default=None),
    projectionVerdict: Optional[List[str]] = Query(default=None),
    pack: Optional[List[str]] = Query(default=None),
    detector: Optional[List[str]] = Query(default=None),
    confidence: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    """Every stored measurement in the caller's org, newest first.

    Filterable by comparability verdict, projection-validation verdict, pack,
    detector and confidence. Those latter filters are T5's calibration surface:
    A1 and A3 can read aggregate projection results without scraping each
    per-opportunity record.
    """
    if verdict:
        unknown = sorted({v for v in verdict if v not in _VERDICTS})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown comparability verdict(s): {', '.join(unknown)}. "
                    f"Valid: {', '.join(_VERDICTS)}"
                ),
            )
    if projectionVerdict:
        unknown_projection = sorted(
            {v for v in projectionVerdict if v not in PROJECTION_VALIDATION_VERDICTS}
        )
        if unknown_projection:
            raise HTTPException(
                status_code=400,
                detail=(
                    "unknown projection validation verdict(s): "
                    f"{', '.join(unknown_projection)}. "
                    f"Valid: {', '.join(PROJECTION_VALIDATION_VERDICTS)}"
                ),
            )
    if confidence:
        unknown_confidence = sorted({c.upper() for c in confidence if c.upper() not in _CONFIDENCES})
        if unknown_confidence:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown confidence filter(s): {', '.join(unknown_confidence)}. "
                    f"Valid: {', '.join(_CONFIDENCES)}"
                ),
            )
    org_id = get_current_org_id()
    items = list_movements(
        org_id,
        verdicts=verdict,
        projection_verdicts=projectionVerdict,
        pack_ids=pack,
        detector_ids=detector,
        confidences=confidence,
        limit=limit,
    )
    caveated = sum(
        1
        for item in items
        if (item.get("comparability") or {}).get("verdict") != VERDICT_COMPARABLE
    )
    projection_counts = {}
    for item in items:
        pv = ((item.get("projectionValidation") or {}).get("verdict")
              or "unknown")
        projection_counts[pv] = projection_counts.get(pv, 0) + 1
    return {
        "orgId": org_id,
        "count": len(items),
        # Surfaced alongside the count so an aggregate can never be read without
        # knowing how many of its inputs carried a caveat.
        "caveatedCount": caveated,
        "projectionValidationCounts": projection_counts,
        "filters": {
            "comparabilityVerdict": verdict or [],
            "projectionVerdict": projectionVerdict or [],
            "pack": pack or [],
            "detector": detector or [],
            "confidence": [c.upper() for c in (confidence or [])],
        },
        "items": items,
    }


@router.get("/run/{run_id}", dependencies=[Depends(require_role("analyst"))])
def get_run_opportunity_movements(
    run_id: str,
    _token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    items = get_movements_for_run(org_id, run_id)
    return {"orgId": org_id, "runId": run_id, "count": len(items), "items": items}


@router.get("/{opportunity_identity}", dependencies=[Depends(require_role("analyst"))])
def get_opportunity_movement_history(
    opportunity_identity: str,
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    """One opportunity's measurement series, oldest first.

    A 404 when there is none, with the reason: absence of a measurement is a
    distinct fact from a measurement of no change, and the API must not blur them.
    """
    org_id = get_current_org_id()
    items = get_movement_history(org_id, opportunity_identity, limit=limit)
    if not items:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no movement measurement for opportunity {opportunity_identity!r}. "
                "An opportunity is measured only once a recorded action exists, a "
                "frozen baseline exists, and a run has landed after the action "
                "date — absence of a measurement is not a measurement of no change."
            ),
        )
    return {
        "orgId": org_id,
        "opportunityIdentity": opportunity_identity,
        "count": len(items),
        "measurements": items,
    }


def register_opportunity_movement_routes(app: FastAPI) -> None:
    if getattr(app.state, "opportunity_movement_routes_registered", False):
        return
    ensure_opportunity_movement_table()
    path = "/api/opportunity-movement/{opportunity_identity}"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.opportunity_movement_routes_registered = True


__all__ = ["register_opportunity_movement_routes", "router"]
