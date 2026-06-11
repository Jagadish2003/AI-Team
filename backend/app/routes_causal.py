"""Causal hypothesis retrieval routes — ENT-6 / T3-S16-A T8.

GET /api/causal/{opportunity_id}/hypothesis
  - Requires Analyst+ (Viewer → 403).
  - Scoped to authenticated org_id from tenancy context (never a query param).
  - Cross-org or non-existent opportunity_id → 404 {"detail": "not found"}.
    Never 403 — 403 would leak the opportunity's existence across tenants.
  - In-org opportunity with no hypothesis yet → 404 {"detail": "No causal
    hypothesis found for this opportunity"}.
  - Returns the most-recent row from causal_hypotheses, ordered by created_at
    DESC. Response shape matches CausalHypothesisSummary (identical to the
    inline causal_hypothesis field on OppEnrichment from T7).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from . import db
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from database.models.causal_hypotheses import ALL_CAUSAL_HYPOTHESES_DDL

CAUSAL_HYPOTHESIS_ROUTE_PATH = "/api/causal/{opportunity_id}/hypothesis"

router = APIRouter(tags=["causal"])
logger = logging.getLogger(__name__)


def ensure_causal_hypotheses_table() -> None:
    """Ensure the causal_hypotheses table and its indexes exist.

    Idempotent — all DDL statements use CREATE TABLE/INDEX IF NOT EXISTS.
    Called once at registration time, never per-request.
    """
    con = db.connect()
    try:
        for ddl in ALL_CAUSAL_HYPOTHESES_DDL:
            con.execute(ddl)
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("ensure_causal_hypotheses_table failed")
        raise
    finally:
        con.close()


@router.get(
    CAUSAL_HYPOTHESIS_ROUTE_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_causal_hypothesis(opportunity_id: str) -> dict:
    """Return the most-recent causal hypothesis for an opportunity.

    Scoped to the authenticated org. Cross-org opportunity_id yields 404 with a
    neutral body — the same status as "does not exist" — so an analyst in org-B
    cannot distinguish "no hypothesis" from "hypothesis exists in org-A". This
    closes the enumeration channel (tenancy isolation rule, same as
    routes_entities.py).

    Two 404 meanings, distinguished by body only (same HTTP status):
      - Cross-org / truly nonexistent: {"detail": "not found"}
      - In-org opportunity with no hypothesis stored yet:
        {"detail": "No causal hypothesis found for this opportunity"}
    """
    org_id = get_current_org_id()

    con = db.connect()
    try:
        con.row_factory = sqlite3.Row

        # Phase 1 — does any hypothesis row exist for this opportunity_id?
        # Deliberately NOT filtered by org_id to detect cross-org cases.
        any_row = con.execute(
            "SELECT org_id FROM causal_hypotheses WHERE opportunity_id = ? LIMIT 1",
            (opportunity_id,),
        ).fetchone()

        if any_row is not None and any_row["org_id"] != org_id:
            # Opportunity exists but belongs to a different org.
            # Return 404 (not 403) — never leak existence across tenants.
            raise HTTPException(status_code=404, detail="not found")

        # Phase 2 — fetch most-recent hypothesis for this org.
        row = con.execute(
            """
            SELECT id, org_id, opportunity_id, run_id,
                   cause_chain, evidence_links, temporal_support,
                   confidence, inferred, falsifiability_condition,
                   preliminary, preliminary_reason, gate_run_count,
                   generated_by, created_at
            FROM causal_hypotheses
            WHERE opportunity_id = ?
              AND org_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (opportunity_id, org_id),
        ).fetchone()

        if row is None:
            if any_row is None:
                # Truly nonexistent — neutral body, same as cross-org case.
                raise HTTPException(status_code=404, detail="not found")
            # In-org opportunity with no hypothesis generated yet.
            raise HTTPException(
                status_code=404,
                detail="No causal hypothesis found for this opportunity",
            )

        row_dict = dict(row)

        # Decode JSON TEXT columns.
        for col in ("cause_chain", "evidence_links"):
            val = row_dict.get(col)
            if isinstance(val, str):
                try:
                    row_dict[col] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "causal_hypotheses: unparseable %s for opportunity %s",
                        col,
                        opportunity_id,
                    )
                    row_dict[col] = []

        temporal = row_dict.get("temporal_support")
        if isinstance(temporal, str):
            try:
                row_dict["temporal_support"] = json.loads(temporal)
            except (json.JSONDecodeError, ValueError):
                row_dict["temporal_support"] = None

        # Normalise SQLite integer booleans to Python bools.
        row_dict["inferred"] = bool(row_dict["inferred"])
        row_dict["preliminary"] = bool(row_dict["preliminary"])

        # Return only CausalHypothesisSummary fields — identical shape to T7.
        return {
            "cause_chain": row_dict["cause_chain"],
            "falsifiability_condition": row_dict["falsifiability_condition"],
            "confidence": row_dict["confidence"],
            "inferred": row_dict["inferred"],
            "preliminary": row_dict["preliminary"],
            "preliminary_reason": row_dict.get("preliminary_reason"),
        }
    finally:
        con.close()


def register_causal_routes(app: FastAPI) -> None:
    """Register causal routes once for the provided FastAPI app.

    Idempotent: a second call with the same app is a no-op. Guards via both
    app.state flag and route-path existence check — same pattern as
    register_entities_routes() in routes_entities.py.
    """
    ensure_causal_hypotheses_table()

    if getattr(app.state, "causal_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if CAUSAL_HYPOTHESIS_ROUTE_PATH in existing_paths:
        app.state.causal_routes_registered = True
        return

    app.include_router(router)
    app.state.causal_routes_registered = True
