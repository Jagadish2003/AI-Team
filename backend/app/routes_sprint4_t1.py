"""Sprint 4 T1: Wire Track B computation into Track A run lifecycle.

Adds:
  - POST /api/runs/{run_id}/compute  (returns immediately; runs Track B in background)
  - GET  /api/runs/{run_id}/status   (polling endpoint for UI + smoke)
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, HTTPException, FastAPI
from pydantic import BaseModel, Field

from .security import require_auth
from .rbac import require_role
from . import db
from .connector_metrics import update_connector_metrics_from_run
from .trackb_runner import run_trackb
from discovery.track_a_adapter import export_track_a_seed

logger = logging.getLogger(__name__)


class ComputeRequest(BaseModel):
    mode: str = Field(default="offline", pattern="^(offline|live)$")
    systems: List[str] = Field(default_factory=lambda: ["salesforce", "servicenow", "jira"])
    pack: Optional[str] = Field(default=None, description="Pack ID: service_cloud or ncino")


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
        try:
            run = db.run_get(run_id)
            if run:
                run["status"] = status
                run["updatedAt"] = payload["updatedAt"]
                db.run_set(run_id, run)
        except Exception:
            logger.warning("Could not mirror status onto run record for %s", run_id, exc_info=True)
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


def _append_event(run_id: str, stage: str, message: str, level: str = "INFO") -> None:
    event = {
        "id": f"ev_{int(time.time() * 1000)}_{stage}",
        "tsLabel": _now_iso(),
        "stage": stage,
        "message": message,
        "level": level,
    }
    events = db.kv_get(f"events:{run_id}") or []
    db.kv_set(f"events:{run_id}", [*events, event])

from .materialize_t2 import (
    _finalise,
    _emit_event,
    _probe_systems,
    _selected_system_ids_for_report,
)

def _run_trackb_and_persist(run_id: str, mode: str, systems: List[str], pack: Optional[str] = None) -> None:
    """Background task: execute Track B and persist Track A-shaped artifacts."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Starting trackb materialization for run {run_id} in mode: {mode}")

    _emit_event(run_id, "CONNECT", f"Connected sources: {', '.join(systems)}")

    run = db.run_get(run_id)
    if run is None:
        raise RuntimeError(f"Run '{run_id}' not found — cannot materialise")

    per_system: Dict[str, str] = {
        s: "skipped" for s in ["salesforce", "servicenow", "jira"]
    }
    errors: Dict[str, str] = {}

    try:
        _emit_event(run_id, "INGEST", "Ingesting data from enterprise systems...")
        per_system, succeeded, probe_errors = _probe_systems(systems, mode)
        errors.update(probe_errors)

        if not succeeded:
            _emit_event(
                run_id,
                "ERROR",
                "No data could be ingested from any system",
                level="ERROR",
            )
            _finalise(
                run_id,
                run,
                "failed",
                mode,
                systems,
                per_system,
                {"opportunities": 0, "evidence": 0},
                errors,
                "DISCOVERY_FAILED",
            )
            return

        _emit_event(
            run_id, "NORMALIZE", f"Successfully ingested from: {', '.join(succeeded)}"
        )

        from discovery.runner import run as trackb_run
        from discovery.track_a_adapter import export_track_a_seed

        _emit_event(
            run_id, "EXTRACT", "Extracting entities and identifying patterns..."
        )
        payload = trackb_run(
            mode=mode, systems=succeeded, run_id=run_id, pack=pack
        )
        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev = seed.get("evidence", [])

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)

        # Keep Integration Hub connector cards in sync with the actual run data.
        from .connector_metrics import update_connector_metrics_from_run

        update_connector_metrics_from_run(payload, succeeded)

        _emit_event(
            run_id,
            "SCORE",
            f"Found {len(opps)} opportunities and {len(ev)} evidence items",
        )

        # T3 — compute and store cross-system linked clusters
        try:
            _emit_event(run_id, "ANALYZE", "Clustering cross-system references...")
            from .materialize_t3_hook import compute_and_store_clusters

            compute_and_store_clusters(run_id, ev)
        except Exception as e:
            errors["clusters"] = str(e)

        # roadmap
        try:
            _emit_event(run_id, "PLAN", "Generating implementation roadmap...")
            from .roadmap_engine import build_roadmap

            db.run_kv_set("roadmap", run_id, build_roadmap(opps))
        except Exception as e:
            errors["roadmap"] = str(e)

        # executive report
        run_inputs = run.get("inputs", {})
        try:
            _emit_event(run_id, "REPORT", "Building executive summary report...")
            from .executive_report_engine import build_executive_report

            roadmap = db.run_kv_get("roadmap", run_id, {})
            selected_system_ids = _selected_system_ids_for_report(
                run_id, run, run_inputs, systems
            )
            er = build_executive_report(
                run_id=run_id,
                opps=opps,
                roadmap=roadmap,
                selected_system_ids=selected_system_ids,
            )

            sa = er.get("sourcesAnalyzed", {})
            sa["totalConnected"] = len(selected_system_ids)
            sa["uploadedFiles"] = len(run_inputs.get("uploadedFiles", []))
            sa["sampleWorkspaceEnabled"] = bool(
                run_inputs.get("sampleWorkspaceEnabled", False)
            )
            er["sourcesAnalyzed"] = sa

            db.run_kv_set("executive_report", run_id, er)
        except Exception as e:
            errors["exec_report"] = str(e)
            db.run_kv_set(
                "executive_report",
                run_id,
                {
                    "confidence": "Moderate",
                    "sourcesAnalyzed": {
                        "recommendedConnected": 0,
                        "totalConnected": len(
                            _selected_system_ids_for_report(
                                run_id, run, run_inputs, systems
                            )
                        ),
                        "uploadedFiles": len(run_inputs.get("uploadedFiles", [])),
                        "sampleWorkspaceEnabled": bool(
                            run_inputs.get("sampleWorkspaceEnabled", False)
                        ),
                    },
                    "topQuickWins": [],
                    "snapshotBubbles": [],
                    "roadmapHighlights": [],
                },
            )

        # T6 — LLM enrichment (post-processing, non-blocking)
        try:
            _emit_event(
                run_id, "AI_ANALYZE", "Starting AI-driven analysis and enrichment..."
            )
            from .llm_enrichment import KV_LLM_ENRICHMENT, run_llm_enrichment

            exec_report = db.run_kv_get("executive_report", run_id, {})
            sources_analyzed = exec_report.get("sourcesAnalyzed", {})
            
            enrichment = run_llm_enrichment(
                run_id=run_id,
                opps=opps,
                evidence=ev,
                sources_analyzed=sources_analyzed,
                pack_id=pack,
            )
            db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)
            if enrichment.get("executiveSummary"):
                exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
                db.run_kv_set("executive_report", run_id, exec_report)
            _emit_event(run_id, "COMPLETE", "AI analysis and enrichment completed")
        except Exception as e:
            errors["llm_enrichment"] = str(e)
            _emit_event(run_id, "AI_ERROR", f"AI analysis failed: {e}", level="WARNING")

        status = "complete" if len(succeeded) == len(systems) else "partial"
        audit_action = (
            "DISCOVERY_MATERIALIZED" if status == "complete" else "DISCOVERY_PARTIAL"
        )
        _finalise(
            run_id,
            run,
            status,
            mode,
            systems,
            per_system,
            {"opportunities": len(opps), "evidence": len(ev)},
            errors,
            audit_action,
        )
        _emit_event(run_id, "DONE", "Discovery run complete (100%)")

    except Exception as e:
        errors["exception"] = str(e)
        _finalise(
            run_id,
            run,
            "failed",
            mode,
            systems,
            per_system,
            {"opportunities": 0, "evidence": 0},
            errors,
            "DISCOVERY_FAILED",
        )


def register_sprint4_t1_routes(app: FastAPI) -> None:
    """Register Sprint 4 T1 endpoints on the existing FastAPI app."""

    @app.post(
        "/api/runs/{run_id}/compute",
        response_model=ComputeResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    )
    async def compute_run(run_id: str, body: ComputeRequest, background_tasks: BackgroundTasks) -> ComputeResponse:
        # Ensure run exists. db.run_get should raise 404; keep defensive for alternate impls.
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        # SN-CONNECT-1 + JIRA-CONNECT-1: run connector health checks at run start.
        # Results stored in KV — S1 reads these to show Live/Fixture badges.
        # Health checks are non-blocking — a failed check never prevents a run.
        try:
            from discovery.ingest.connector_health import check_all_connectors
            connector_health = check_all_connectors()
            if hasattr(db, "run_kv_set"):
                db.run_kv_set("connector_health", run_id, connector_health)
        except Exception as _e:
            logger.warning("Connector health check failed (non-blocking): %s", _e)

        # Mark status running and return immediately.
        _set_status(run_id, "running", counts={"opportunities": 0, "evidence": 0})
        _append_event(run_id, "QUEUED", "Discovery run queued.")
        background_tasks.add_task(_run_trackb_and_persist, run_id, body.mode, body.systems, body.pack)

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
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
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

    @app.get(
        "/api/runs/{run_id}/connector-health",
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    async def get_connector_health(run_id: str) -> Dict[str, Any]:
        """
        SN-CONNECT-1 + JIRA-CONNECT-1: Return connector health for S1 Live badges.

        Returns the health check results stored at run start.
        If not yet available, runs checks on demand.

        Response shape:
          {
            "ServiceNow": {"system": "ServiceNow", "status": "live"|"fixture"|"error",
                           "message": "...", "latencyMs": 42, "isLive": true},
            "Jira":        {"system": "Jira", "status": "live"|"fixture"|"error",
                           "message": "...", "latencyMs": 38, "isLive": true},
            "nCino":       {"system": "nCino", "status": "live"|"fixture"|"error",
                           "message": "...", "latencyMs": 40, "isLive": true},
          }
        """
        # Try stored health from run start first
        if hasattr(db, "run_kv_get"):
            stored = db.run_kv_get("connector_health", run_id, None)
            if stored:
                return stored

        # Not stored yet — run on demand
        try:
            from discovery.ingest.connector_health import check_all_connectors
            health = check_all_connectors()
            if hasattr(db, "run_kv_set"):
                db.run_kv_set("connector_health", run_id, health)
            return health
        except Exception as e:
            return {
                "ServiceNow": {"system": "ServiceNow", "status": "error",
                               "message": str(e), "isLive": False},
                "Jira":        {"system": "Jira", "status": "error",
                               "message": str(e), "isLive": False},
                "nCino":       {"system": "nCino", "status": "error",
                               "message": str(e), "isLive": False},
            }
