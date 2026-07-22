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
from pydantic import BaseModel, Field, model_validator

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
    pack: Optional[str] = Field(
        default=None,
        description=(
            "Primary pack ID (backward-compatible singular alias for pack_ids). "
            "e.g. service_cloud or ncino."
        ),
    )
    pack_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "R191-P1 T1: multi-pack selection (order-preserving, de-duplicated). "
            "Supersedes the singular pack; a single-element list behaves exactly "
            "as pack. Reconciled with pack in the validator — both stay in sync, "
            "pack being the primary (first) pack."
        ),
    )

    @model_validator(mode="after")
    def _reconcile_packs(self) -> "ComputeRequest":
        # R191-P1 T1: fold the singular pack and the pack_ids list into ONE
        # order-preserving, de-duplicated selection via the shared primitive, then
        # re-derive pack as the primary (first). Single-pack callers — the current
        # frontend sends only `pack` — are unaffected: [pack] normalises to pack.
        from discovery.packs.pack_config import normalize_pack_ids

        combined = normalize_pack_ids(
            list(self.pack_ids or []) + ([self.pack] if self.pack else [])
        )
        self.pack_ids = combined
        self.pack = combined[0] if combined else None
        return self


class ComputeResponse(BaseModel):
    ok: bool = True
    runId: str
    modeUsed: str
    systemsUsed: List[str]
    # counts are 0 for immediate response; caller should poll /status
    counts: Dict[str, int] = Field(default_factory=dict)


# NOTE (CS-4 / AT-313): the run status model + GET /api/runs/{run_id}/status
# endpoint live in routes_sprint4_t2.py (StatusResponse / run_status), which is
# registered before this module in main.py and therefore owns that path. The
# duplicate RunStatus model and get_status route that used to live here were
# removed so there is exactly one status implementation (it already returns
# current_step and failed_steps). This module keeps only the /compute and
# /connector-health routes.


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
    _ingest_summary_from_payload,
    _org_id_for_run,
    _pack_id_for_run,
    _pack_ids_for_run,
    _selected_system_ids_for_report,
    resolve_effective_pack,
)


def _apply_temporal_enrichment(
    run_id: str,
    run: Dict[str, Any],
    pack: Optional[str],
    opps: List[Dict[str, Any]],
    org_id: str,
) -> List[Dict[str, Any]]:
    """Attach temporal context to the active compute path without blocking runs.

    org_id must be the same value the signal snapshots were written under (the
    org_id passed to the runner), otherwise the baseline read finds no history.
    """
    try:
        from .llm_enrichment import KV_LLM_ENRICHMENT
        from .jobs.baseline_calculator import calculate_baselines_for_org
        from .telemetry import record_event
        from .temporal_enrichment import enrich_opportunities_with_temporal_context

        calculate_baselines_for_org(org_id or "default")
        fallback_pack_id = _pack_id_for_run(run) or pack or ""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for opp in opps:
            opp_pack_id = str(
                opp.get("packId") or opp.get("pack_id") or fallback_pack_id
            )
            grouped.setdefault(opp_pack_id, []).append(opp)
        for pack_id, pack_opps in grouped.items():
            enrich_opportunities_with_temporal_context(
                run_id,
                org_id or "default",
                pack_id,
                pack_opps,
            )

        temporal_keys = (
            "baseline_context",
            "trend_direction",
            "anomaly_score",
            "is_anomalous",
            "first_deviation",
            "baseline_mean",
            "run_count",
            "baseline_stddev",
            "baseline_window_days",
            "current_value",
            "recent_values",
            "signal_key",
            "pack_id",
        )
        stored = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, {})
        per_opp = stored.get("perOpportunity", {})
        for opp in opps:
            opp_id = opp.get("id", "")
            if opp_id in per_opp:
                for key in temporal_keys:
                    if key in opp:
                        per_opp[opp_id][key] = opp[key]
        stored["perOpportunity"] = per_opp
        db.run_kv_set(KV_LLM_ENRICHMENT, run_id, stored)
        record_event(
            "temporal.enrichment_completed",
            {"run_id": run_id, "org_id": org_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("T7 temporal enrichment failed (non-blocking): %s", exc)
    return opps


def _run_trackb_and_persist(
    run_id: str,
    mode: str,
    systems: List[str],
    pack: Optional[str] = None,
    pack_ids: Optional[List[str]] = None,
) -> None:
    """Background task: execute Track B and persist Track A-shaped artifacts."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Starting trackb materialization for run {run_id} in mode: {mode}")

    run = db.run_get(run_id)
    if run is None:
        raise RuntimeError(f"Run '{run_id}' not found — cannot materialise")

    from discovery.packs.pack_config import normalize_pack_ids

    # The launch record is the source of truth. This is important for the current
    # frontend, which may still send only the primary pack to /compute after a
    # multi-template launch. Direct compute callers can supply pack_ids normally.
    selected_pack_ids = normalize_pack_ids(
        list(_pack_ids_for_run(run) or [])
        + list(pack_ids or [])
        + ([pack] if pack else [])
    )
    pack = selected_pack_ids[0] if selected_pack_ids else pack

    # Resolve the run's org once; reused for live-connector resolution and for
    # the runner's org_id below.
    from .middleware.tenancy import get_current_org_id_optional

    run_org_id = _org_id_for_run(run, get_current_org_id_optional() or "default")

    # CS-2: prefer the org's authenticated connectors. If the user connected
    # Salesforce / ServiceNow / Jira via the Integration Hub OAuth flow, ingest
    # LIVE from exactly those connectors using their stored OAuth tokens —
    # instead of the requested mode/systems (which would otherwise rely on
    # offline fixtures or .env credentials). Falls back to the requested
    # mode/systems when nothing is authenticated.
    try:
        from .live_ingest_credentials import resolve_live_systems

        live_systems = resolve_live_systems(run_org_id)
    except Exception:
        logger.exception("Live connector resolution failed; using requested mode/systems")
        live_systems = []

    if live_systems:
        mode = "live"
        systems = live_systems
        _emit_event(
            run_id,
            "CONNECT",
            f"Using authenticated connectors: {', '.join(live_systems)}",
        )
    else:
        _emit_event(run_id, "CONNECT", f"Connected sources: {', '.join(systems)}")

    # Default a GitHub-connected run to the github_engineering pack unless a pack
    # was explicitly selected. `pack` flows to the runner + enrichment + temporal
    # calls below, so setting it here covers them all. Mirrors materialize_t2.
    _effective_pack = resolve_effective_pack(pack, live_systems)
    if _effective_pack and _effective_pack != pack:
        pack = _effective_pack
        selected_pack_ids = normalize_pack_ids([pack] + selected_pack_ids)
        _emit_event(
            run_id, "CONNECT", f"GitHub connected — defaulting to the {_effective_pack} pack"
        )

    per_system: Dict[str, str] = {
        s: "skipped" for s in ["salesforce", "servicenow", "jira"]
    }
    errors: Dict[str, str] = {}

    try:
        # Single-ingest: the discovery runner is the SOLE place enterprise systems
        # are ingested. The removed _probe_systems pre-pass ingested Salesforce/
        # ServiceNow/Jira a second time (discarding the data) just to build the
        # per-system status + "any data?" gate. The runner now returns that
        # summary in its payload and receives ALL connected systems.
        _emit_event(run_id, "INGEST", "Ingesting data from enterprise systems...")

        from discovery.runner import run as trackb_run
        from discovery.track_a_adapter import export_track_a_seed

        _emit_event(
            run_id, "EXTRACT", "Extracting entities and identifying patterns..."
        )
        # run_org_id resolved above (shared with live-connector resolution). It is
        # passed to the runner so signal snapshots are written under the same org
        # the temporal read uses; without it the runner falls back to its own
        # "demo-org" default, which hides the Baseline Context panel.
        payload = trackb_run(
            mode=mode,
            systems=systems,
            run_id=run_id,
            org_id=run_org_id,
            pack=pack,
            pack_ids=selected_pack_ids or None,
        )

        # Preserve the exact immutable execution snapshot returned by the runner.
        executed_detectors = payload.get("detectorsExecuted")
        if isinstance(executed_detectors, list):
            run["packId"] = payload.get("packId") or pack
            run["packName"] = payload.get("packName") or run.get("packName")
            run["packVersion"] = payload.get("packVersion")
            run["executedDetectorIds"] = [
                str(detector_id)
                for detector_id in executed_detectors
                if str(detector_id).strip()
            ]
            run["packExecutedAt"] = payload.get("packExecutedAt")
        if isinstance(payload.get("packIds"), list):
            run["packIds"] = list(payload["packIds"])
        if isinstance(payload.get("packVersions"), dict):
            run["packVersions"] = dict(payload["packVersions"])
        if isinstance(payload.get("packs"), list):
            run["packs"] = list(payload["packs"])

        per_system, succeeded, ingest_errors = _ingest_summary_from_payload(
            payload, systems
        )
        errors.update(ingest_errors)

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

        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev = seed.get("evidence", [])

        # R16-B1 (§2): stamp the stable cross-run opportunity_identity onto each
        # stored opportunity BEFORE it is persisted, so the served record carries
        # the id used to group an opportunity with its own history. Idempotent and
        # additive (a value already stamped upstream is recomputed to the same id);
        # non-blocking — never breaks the run.
        try:
            from .opportunity_instances import stamp_opportunity_identities

            stamp_opportunity_identities(opps, run_id, org_id=run_org_id)
        except Exception as e:  # noqa: BLE001
            errors["opportunity_identity"] = str(e)
            logger.warning("Opportunity identity stamping failed (non-blocking): %s", e)

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)

        # R16-B1 (T4): persist one per-run opportunity_instance per opportunity,
        # carrying the stable opportunity_identity + run-specific score/confidence/
        # evidence/narrative (R16-B1 §2a). Built from the RAW runner opportunities
        # (payload["opportunities"]) because those keep orgId + detector_id +
        # signal_source at top level — the inputs identity is derived from. The
        # same identity recurring across runs is what later outcome tracking (2.0)
        # compares. Non-blocking: a storage failure never breaks the run.
        try:
            from .opportunity_instances import record_opportunity_instances

            n_instances = record_opportunity_instances(
                run_id, payload.get("opportunities", []), org_id=run_org_id
            )
            logger.info("Recorded %d opportunity instances for run %s", n_instances, run_id)
        except Exception as e:  # noqa: BLE001
            errors["opportunity_instances"] = str(e)
            logger.warning(
                "Opportunity instance recording failed (non-blocking): %s", e
            )

        # Keep Integration Hub connector cards in sync with the actual run data.
        from .connector_metrics import update_connector_metrics_from_run

        update_connector_metrics_from_run(payload, succeeded, run_org_id)

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
            
            from .pack_aware_enrichment import run_pack_aware_enrichment

            execution_pack_ids = list(payload.get("packIds") or selected_pack_ids)
            enrichment = run_pack_aware_enrichment(
                run_id=run_id,
                opportunities=opps,
                evidence=ev,
                sources_analyzed=sources_analyzed,
                pack_ids=execution_pack_ids or [pack],
                org_id=run_org_id,
                enrichment_fn=run_llm_enrichment,
            )
            db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)
            if enrichment.get("executiveSummary"):
                exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
                db.run_kv_set("executive_report", run_id, exec_report)
            _emit_event(run_id, "COMPLETE", "AI analysis and enrichment completed")
        except Exception as e:
            errors["llm_enrichment"] = str(e)
            _emit_event(run_id, "AI_ERROR", f"AI analysis failed: {e}", level="WARNING")

        # T7 - temporal enrichment (non-blocking, T3-S11-A)
        opps = _apply_temporal_enrichment(run_id, run, pack, opps, run_org_id)

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
        background_tasks.add_task(
            _run_trackb_and_persist,
            run_id,
            body.mode,
            body.systems,
            body.pack,
            body.pack_ids,
        )

        return ComputeResponse(
            ok=True,
            runId=run_id,
            modeUsed=body.mode,
            systemsUsed=body.systems,
            counts={"opportunities": 0, "evidence": 0},
        )

    # GET /api/runs/{run_id}/status is owned by routes_sprint4_t2.py (see the
    # module note above) — intentionally not registered here to avoid a
    # duplicate, divergent implementation of the same path.

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
        # Tenant isolation (R17-D3 / AT-448): verify the run belongs to the
        # authenticated org BEFORE reading or writing its run-scoped KV. run_kv_get
        # keys only by run_id, so without this guard an authenticated user in one
        # org could read (and lazily overwrite) another org's connector health by
        # run id. require_run_exists denies cross-org access as 404.
        db.require_run_exists(run_id)

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
