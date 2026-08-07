"""
routes_trace_graph.py — Release 2.0-B1 T1/T2: the trace graph API.

Exposes trace_graph.py's engine as a single, read-only, org-scoped endpoint:

  GET /api/runs/{run_id}/opportunities/{opp_id}/trace-graph

Mirrors routes_sprint4_t6.py's evidence-trace route contract exactly:
  - 404 only for an unknown run or an opportunity absent from the run.
  - Org-scoped: a run belonging to another org yields an empty
    (available: false) trace, never another tenant's provenance.
  - Never 404 for a merely empty/thin trace.

T2 (AC3) adds ``retrieval_candidates``: every retrieval candidate context
assembly considered for this finding, used and unused alike — "retrieval
proposes, assembly decides", and both sides of that decision are visible.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .security import require_auth
from .rbac import require_role
from . import db

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

class TraceHopSummary(BaseModel):
    hop_id: str
    hop_type: str
    label: str
    origin: str
    connector: Optional[str] = None
    run_id: str
    timestamp: Optional[str] = None
    from_hop_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class JoinTraceSummary(BaseModel):
    join_type: str
    window_seconds: Optional[int] = None
    delta_seconds: Optional[float] = None
    within_window: bool
    a_at: Optional[str] = None
    b_at: Optional[str] = None
    hop_id: Optional[str] = None


class RetrievalCandidateSummary(BaseModel):
    """2.0-B1 T2 (AC3) — one retrieval candidate context assembly considered
    for this finding: proposed by retrieval, decided by assembly."""
    chunk_id: str
    used: bool
    decision: str
    reason: Optional[str] = None
    confidence: Optional[float] = None
    origin: Optional[str] = None
    source_system: Optional[str] = None
    source_artifact: Optional[str] = None
    content_snippet: Optional[str] = None
    is_stale: Optional[bool] = None


class TraceGraphResponse(BaseModel):
    """The full provenance chain for one opportunity (2.0-B1 AC1/AC2/AC3).

    ``available`` is False (never 404) when the run belongs to another org or
    the opportunity has no derivable chain — mirroring the evidence-trace and
    enrichment routes' contract.
    """
    runId: str
    oppId: str
    hops: List[TraceHopSummary] = Field(default_factory=list)
    joins: List[JoinTraceSummary] = Field(default_factory=list)
    # True only when the chain TERMINATES IN SOURCE RECORDS (AC1's wording).
    complete: bool = False
    # Why it does not, when it does not: 'no_source_record' (evidence is attached
    # but nothing resolves to an originating record) or 'no_chain' (nothing below
    # the finding at all). Null when complete.
    incompleteReason: Optional[str] = None
    truncated: bool = False
    retrieval_candidates: List[RetrievalCandidateSummary] = Field(default_factory=list)
    retrieval_candidates_used_count: int = 0
    retrieval_candidates_unused_count: int = 0
    # "Is there a chain to render?" — deliberately NOT `complete`. An incomplete
    # chain is the one a reviewer most needs to see, so it must not be hidden.
    available: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_run(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _find_stored_opp(run_id: str, opp_id: str) -> Optional[Dict[str, Any]]:
    opps = db.run_kv_get("opps", run_id, []) or []
    return next((o for o in opps if isinstance(o, dict) and o.get("id") == opp_id), None)


def _run_org_id(run: Dict[str, Any]) -> Optional[str]:
    inputs = run.get("inputs") or {}
    candidates = [
        run.get("org_id"),
        run.get("orgId"),
        inputs.get("org_id") if isinstance(inputs, dict) else None,
        inputs.get("orgId") if isinstance(inputs, dict) else None,
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _load_trace(run: Dict[str, Any], run_id: str, opp_id: str):
    """Org-scoped trace load. Returns None on any tenancy mismatch or failure —
    never another tenant's provenance (same guard as _load_evidence_pointers)."""
    try:
        from app.middleware.tenancy import get_current_org_id
        org_id = get_current_org_id()
    except Exception as exc:
        logger.debug("trace-graph skipped — org context unavailable: %s", exc)
        return None

    run_org_id = _run_org_id(run)
    if run_org_id is not None and run_org_id != org_id:
        logger.debug(
            "trace-graph skipped — run %s belongs to org %s, request org %s",
            run_id, run_org_id, org_id,
        )
        return None

    try:
        from .trace_graph import load_finding_trace
        return load_finding_trace(run_id, opp_id)
    except Exception as exc:
        logger.debug("trace-graph load failed for run %s opp %s: %s", run_id, opp_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_trace_graph_routes(app) -> None:
    if getattr(app.state, "trace_graph_routes_registered", False):
        return
    if any(getattr(r, "path", None) == "/api/runs/{run_id}/opportunities/{opp_id}/trace-graph" for r in app.routes):
        app.state.trace_graph_routes_registered = True
        return

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/trace-graph",
        response_model=TraceGraphResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        tags=["runs"],
    )
    def get_trace_graph(run_id: str, opp_id: str) -> TraceGraphResponse:
        """2.0-B1 (T1/T2) — the complete provenance chain for one finding.

        Walks finding -> contributing evidence -> source records, with every
        hop carrying origin, connector, run id, and timestamp (AC1); where a
        claim was corroborated by an MSP-B7 time-windowed join, the join type
        and correlation window used are surfaced too, and a join outside its
        window can never appear (AC2). ``retrieval_candidates`` lists every
        candidate context assembly considered for this finding — used and
        unused alike (AC3).
        """
        run = _require_run(run_id)

        # Org ownership is checked BEFORE any run-scoped opportunity data is read.
        # _load_trace re-checks (defence in depth), but reading the opps KV blob
        # first would pull another tenant's full opportunity list into this
        # request's context before the org gate fired. A cross-org request gets the
        # same available:false answer, now without touching the other org's data.
        try:
            from app.middleware.tenancy import get_current_org_id
            request_org_id = get_current_org_id()
        except Exception:  # noqa: BLE001 — no tenancy context => defer to _load_trace
            request_org_id = None
        run_org_id = _run_org_id(run)
        if run_org_id is not None and request_org_id is not None and run_org_id != request_org_id:
            return TraceGraphResponse(runId=run_id, oppId=opp_id, available=False)

        stored_opp = _find_stored_opp(run_id, opp_id)
        if stored_opp is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in run '{run_id}'",
            )

        trace = _load_trace(run, run_id, opp_id)
        if trace is None:
            return TraceGraphResponse(runId=run_id, oppId=opp_id, available=False)

        used_count = sum(1 for c in trace.retrieval_candidates if c.used)
        return TraceGraphResponse(
            runId=run_id,
            oppId=opp_id,
            hops=[TraceHopSummary(**hop.to_dict()) for hop in trace.hops],
            joins=[JoinTraceSummary(**join.to_dict()) for join in trace.joins],
            complete=trace.complete,
            incompleteReason=trace.incomplete_reason,
            truncated=trace.truncated,
            retrieval_candidates=[
                RetrievalCandidateSummary(**c.to_dict()) for c in trace.retrieval_candidates
            ],
            retrieval_candidates_used_count=used_count,
            retrieval_candidates_unused_count=len(trace.retrieval_candidates) - used_count,
            available=trace.has_chain,
        )
