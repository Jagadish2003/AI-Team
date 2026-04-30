import time
from typing import Any, Dict, List, Tuple

from . import db


def _now_epoch() -> int:
    return int(time.time())


def set_status(run_id: str, payload: Dict[str, Any]) -> None:
    db.run_kv_set("status", run_id, payload)


def get_status(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    started_at = run.get("startedAt", db.now_iso()) if run else db.now_iso()

    return db.run_kv_get(
        "status",
        run_id,
        {
            "runId": run_id,
            "status": "running",
            "startedAt": started_at,
            "updatedAt": started_at,
        },
    )


def _audit_prepend(run_id: str, event: Dict[str, Any]) -> None:
    audit = db.run_kv_get("audit", run_id, [])
    db.run_kv_set("audit", run_id, [event] + audit)


def _audit_event(action: str, by: str = "System") -> Dict[str, Any]:
    return {
        "id": f"aud_{_now_epoch()}",
        "tsLabel": db.now_iso(),
        "tsEpoch": _now_epoch(),
        "action": action,
        "by": by,
    }


def _probe_systems(
    systems: List[str], mode: str
) -> Tuple[Dict[str, str], List[str], Dict[str, str]]:
    import os

    os.environ["INGEST_MODE"] = mode

    from discovery.ingest import jira as _jira
    from discovery.ingest import salesforce as _sf
    from discovery.ingest import servicenow as _sn
    from discovery.ingest.jira import JiraIngestError
    from discovery.ingest.salesforce import IngestError as SFError
    from discovery.ingest.servicenow import ServiceNowIngestError as SNError

    _ingest_map = {
        "salesforce": (_sf.ingest, (SFError,)),
        "servicenow": (_sn.ingest, (SNError,)),
        "jira": (_jira.ingest, (JiraIngestError,)),
    }

    per_system: Dict[str, str] = {
        s: "skipped" for s in ["salesforce", "servicenow", "jira"]
    }
    errors: Dict[str, str] = {}
    succeeded: List[str] = []

    for system in systems:
        if system not in _ingest_map:
            continue
        ingest_fn, _ = _ingest_map[system]
        try:
            data = ingest_fn()
            if data:
                per_system[system] = "ok"
                succeeded.append(system)
            else:
                per_system[system] = "failed"
                errors[system] = "ingest returned no data"
        except Exception as e:
            per_system[system] = "failed"
            errors[system] = str(e)

    return per_system, succeeded, errors


def _finalise(
    run_id: str,
    run: Dict[str, Any],
    status: str,
    mode: str,
    systems: List[str],
    per_system: Dict[str, str],
    counts: Dict[str, int],
    errors: Dict[str, str],
    audit_action: str,
) -> None:
    set_status(
        run_id,
        {
            "runId": run_id,
            "status": status,
            "modeUsed": mode,
            "systemsUsed": systems,
            "perSystem": per_system,
            "counts": counts,
            "errors": errors,
            "updatedAt": db.now_iso(),
        },
    )
    _audit_prepend(run_id, _audit_event(audit_action))
    run["status"] = status
    run["updatedAt"] = db.now_iso()
    db.run_set(run_id, run)


def _emit_event(run_id: str, stage: str, message: str, level: str = "INFO") -> None:
    event = {
        "id": f"ev_{int(time.time() * 1000)}_{stage}",
        "tsLabel": db.now_iso(),
        "stage": stage,
        "message": message,
        "level": level,
    }
    events = db.kv_get(f"events:{run_id}") or []
    db.kv_set(f"events:{run_id}", events + [event])


def run_trackb_and_persist(
    run_id: str, mode: str, systems: List[str], run_inputs: Dict[str, Any]
) -> None:
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
        payload = trackb_run(mode=mode, systems=succeeded, run_id=run_id)
        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev = seed.get("evidence", [])

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)

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
        try:
            _emit_event(run_id, "REPORT", "Building executive summary report...")
            from .executive_report_engine import build_executive_report

            roadmap = db.run_kv_get("roadmap", run_id, {})
            er = build_executive_report(run_id=run_id, opps=opps, roadmap=roadmap)

            sa = er.get("sourcesAnalyzed", {})
            sa["totalConnected"] = len(run_inputs.get("connectedSources", []))
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
                        "totalConnected": len(run_inputs.get("connectedSources", [])),
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
