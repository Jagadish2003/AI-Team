"""Sprint 4 T1: Wire Track B computation into Track A run lifecycle.

Adds:
  - POST /api/runs/{run_id}/compute  (returns immediately; runs Track B in background)
  - GET  /api/runs/{run_id}/status   (polling endpoint for UI + smoke)
"""

from __future__ import annotations

import itertools
import time
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, HTTPException, FastAPI
from pydantic import BaseModel, Field

from .security import require_auth
from . import db
from .trackb_runner import run_trackb
from discovery.track_a_adapter import export_track_a_seed


class ComputeRequest(BaseModel):
    mode: str = Field(default="offline", pattern="^(offline|live)$")
    systems: List[str] = Field(default_factory=lambda: ["salesforce", "servicenow", "jira"])


class ComputeResponse(BaseModel):
    ok: bool = True
    runId: str
    modeUsed: str
    systemsUsed: List[str]
    # counts are 0 for immediate response; caller should poll /status
    counts: Dict[str, int] = Field(default_factory=dict)


class RunStatus(BaseModel):
    runId: str
    status: str  # running|complete|failed
    startedAt: str
    updatedAt: str
    error: Optional[str] = None
    counts: Dict[str, int] = Field(default_factory=dict)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _status_key(run_id: str) -> str:
    return f"status:{run_id}"


def _set_status(run_id: str, status: str, counts: Optional[Dict[str, int]] = None, error: Optional[str] = None) -> None:
    payload: Dict[str, Any] = db.run_kv_get("status", run_id, {}) if hasattr(db, "run_kv_get") else {}
    started_at = payload.get("startedAt") or _now_iso()
    payload = {
        "runId": run_id,
        "status": status,
        "startedAt": started_at,
        "updatedAt": _now_iso(),
        "error": error,
        "counts": counts or payload.get("counts") or {},
    }
    # Preferred: run_kv_set (run-scoped KV)
    if hasattr(db, "run_kv_set"):
        db.run_kv_set("status", run_id, payload)
    else:
        # Fallback: store on run record if only run_set exists
        run = db.run_get(run_id)
        run["status"] = payload
        db.run_set(run_id, run)


def _get_status(run_id: str) -> Dict[str, Any]:
    if hasattr(db, "run_kv_get"):
        return db.run_kv_get("status", run_id, {})
    run = db.run_get(run_id)
    return run.get("status") or {}


def _run_trackb_and_persist(run_id: str, mode: str, systems: List[str]) -> None:
    """Background task: execute Track B and persist Track A-shaped artifacts."""
    try:
        # Build run_context for Track B runner (primarily runId)
        run = db.run_get(run_id)  # may raise HTTPException(404) in Track A backend
        run_context = {"runId": run_id, "inputs": run.get("inputs") or {}}

        payload = run_trackb(mode=mode, systems=systems, run_context=run_context)

        # Convert Track B payload -> Track A seed shapes (opportunities + flattened evidence)
        seed = export_track_a_seed(payload, id_counter=itertools.count(1))
        opps = seed.get("opportunities") or []
        evidence = seed.get("evidence") or []

        # Persist under run-scoped KV
        if hasattr(db, "run_kv_set"):
            db.run_kv_set("opps", run_id, opps)
            db.run_kv_set("evidence", run_id, evidence)
        else:
            # Fallback: attach to run record
            run["opps"] = opps
            run["evidence"] = evidence
            db.run_set(run_id, run)

        _set_status(run_id, "complete", counts={"opportunities": len(opps), "evidence": len(evidence)})

    except Exception as e:  # noqa: BLE001
        _set_status(run_id, "failed", error=str(e))


def register_sprint4_t1_routes(app: FastAPI) -> None:
    """Register Sprint 4 T1 endpoints on the existing FastAPI app."""

    @app.post(
        "/api/runs/{run_id}/compute",
        response_model=ComputeResponse,
        dependencies=[Depends(require_auth)],
    )
    async def compute_run(run_id: str, body: ComputeRequest, background_tasks: BackgroundTasks) -> ComputeResponse:
        # Ensure run exists. db.run_get should raise 404; keep defensive for alternate impls.
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        # Mark status running and return immediately.
        _set_status(run_id, "running", counts={"opportunities": 0, "evidence": 0})
        background_tasks.add_task(_run_trackb_and_persist, run_id, body.mode, body.systems)

        return ComputeResponse(
            ok=True,
            runId=run_id,
            modeUsed=body.mode,
            systemsUsed=body.systems,
            counts={"opportunities": 0, "evidence": 0},
        )

    @app.get(
        "/api/runs/{run_id}/status",
        response_model=RunStatus,
        dependencies=[Depends(require_auth)],
    )
    async def get_status(run_id: str) -> RunStatus:
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        st = _get_status(run_id) or {}
        if not st:
            # If status not written yet, treat as running (run exists)
            st = {"runId": run_id, "status": run.get("status") or "running", "startedAt": run.get("startedAt") or _now_iso(), "updatedAt": _now_iso(), "counts": {}}

        return RunStatus(**st)
