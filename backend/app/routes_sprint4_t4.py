"""
Sprint 4 T4 — Deterministic Replay  v1.2

Replay semantics (locked):
  - POST /api/runs/{runId}/replay re-serves persisted artifacts from
    run-scoped KV storage (opps, evidence, clusters, roadmap, executive_report).
  - No live ingestion. No Track B runner call. No external API calls.
  - Artifacts are READ ONLY — no KV writes except run record + status flags.
  - Response includes isReplay: true and replayedAt timestamp.
  - Replay is idempotent: same artifacts returned every time.
  - 404 if runId not found, or if opps were never persisted.

Timestamp write policy on replay:
  - replayedAt:  written (new field, set to now)
  - updatedAt:   written (reflects when replay was called)
  - startedAt:   NOT written (must remain identical to original run)
  - completedAt: NOT written (not rewritten, stays as original)
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .security import require_auth
from . import db


# ─────────────────────────────────────────────────────────────────────────────
# Response model
# ─────────────────────────────────────────────────────────────────────────────

class ReplayResponse(BaseModel):
    ok: bool = True
    runId: str
    isReplay: bool = True
    replayedAt: str
    status: str
    counts: Dict[str, int] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# KV key constants — must match what T1/T2/T3 persist under
# ─────────────────────────────────────────────────────────────────────────────

KV_OPPS        = "opps"
KV_EVIDENCE    = "evidence"
KV_CLUSTERS    = "clusters"
KV_ROADMAP     = "roadmap"
KV_EXEC_REPORT = "executive_report"
KV_STATUS      = "status"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_run(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _require_artifacts(run_id: str) -> Dict[str, Any]:
    """
    Load all persisted artifacts for a run. Strictly read-only.
    Raises 404 if required artifact keys are missing — means the run
    never completed materialisation and cannot be replayed.
    """
    opps = db.run_kv_get(KV_OPPS, run_id, None)

    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Run '{run_id}' has no persisted opportunities. "
                f"The run may have failed before materialisation completed "
                f"and cannot be replayed."
            ),
        )

    return {
        KV_OPPS:        opps,
        KV_EVIDENCE:    db.run_kv_get(KV_EVIDENCE,    run_id, []),
        KV_CLUSTERS:    db.run_kv_get(KV_CLUSTERS,    run_id, []),
        KV_ROADMAP:     db.run_kv_get(KV_ROADMAP,     run_id, None),
        KV_EXEC_REPORT: db.run_kv_get(KV_EXEC_REPORT, run_id, None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_sprint4_t4_routes(app) -> None:

    @app.post(
        "/api/runs/{run_id}/replay",
        response_model=ReplayResponse,
        dependencies=[Depends(require_auth)],
        tags=["runs"],
    )
    async def replay_run(run_id: str) -> ReplayResponse:
        """
        Re-serve persisted artifacts for a completed run.
        Artifacts are read-only — no KV re-writes for opps/evidence/clusters/roadmap/exec_report.
        Only run record and status KV are updated with isReplay and replayedAt flags.
        """
        run       = _require_run(run_id)
        artifacts = _require_artifacts(run_id)

        replayed_at = _now_iso()

        # Update run record — only replay flags and updatedAt
        # startedAt and completedAt intentionally NOT touched
        run["isReplay"]   = True
        run["replayedAt"] = replayed_at
        run["updatedAt"]  = replayed_at
        run["status"]     = "complete"
        db.run_set(run_id, run)

        # Update status KV — only replay flags, not artifact timestamps
        existing_status = db.run_kv_get(KV_STATUS, run_id, {})
        existing_status.update({
            "status":     "complete",
            "isReplay":   True,
            "replayedAt": replayed_at,
            "updatedAt":  replayed_at,
        })
        db.run_kv_set(KV_STATUS, run_id, existing_status)

        opps     = artifacts[KV_OPPS]
        evidence = artifacts[KV_EVIDENCE]
        clusters = artifacts[KV_CLUSTERS]

        return ReplayResponse(
            runId=run_id,
            replayedAt=replayed_at,
            status="complete",
            counts={
                "opportunities": len(opps)     if isinstance(opps,     list) else 0,
                "evidence":      len(evidence)  if isinstance(evidence, list) else 0,
                "clusters":      len(clusters)  if isinstance(clusters, list) else 0,
            },
        )
