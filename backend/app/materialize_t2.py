import time
from typing import Dict, Any, List, Tuple
from . import db


def _now_epoch() -> int:
    return int(time.time())


def set_status(run_id: str, payload: Dict[str, Any]) -> None:
    db.run_kv_set("status", run_id, payload)


def get_status(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    started_at = run.get("startedAt", db.now_iso()) if run else db.now_iso()

    return db.run_kv_get("status", run_id, {
        "runId": run_id,
        "status": "running",
        "startedAt": started_at,
        "updatedAt": started_at
    })


def _audit_prepend(run_id: str, event: Dict[str, Any]) -> None:
    audit = db.run_kv_get("audit", run_id, [])
    db.run_kv_set("audit", run_id, [event] + audit)


def _audit_event(action: str, by: str = "System") -> Dict[str, Any]:
    return {"id": f"aud_{_now_epoch()}", "tsLabel": db.now_iso(), "tsEpoch": _now_epoch(), "action": action, "by": by}


def _probe_systems(
    systems: List[str], mode: str
) -> Tuple[Dict[str, str], List[str], Dict[str, str]]:
    import os
    os.environ["INGEST_MODE"] = mode

    from discovery.ingest import salesforce as _sf
    from discovery.ingest import servicenow as _sn
    from discovery.ingest import jira as _jira
    from discovery.ingest.salesforce import IngestError as SFError
    from discovery.ingest.servicenow import ServiceNowIngestError as SNError
    from discovery.ingest.jira import JiraIngestError

    _ingest_map = {
        "salesforce": (_sf.ingest, (SFError,)),
        "servicenow": (_sn.ingest, (SNError,)),
        "jira":       (_jira.ingest, (JiraIngestError,)),
    }

    per_system: Dict[str, str] = {s: "skipped" for s in ["salesforce", "servicenow", "jira"]}
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
    set_status(run_id, {
        "runId": run_id,
        "status": status,
        "modeUsed": mode,
        "systemsUsed": systems,
        "perSystem": per_system,
        "counts": counts,
        "errors": errors,
        "updatedAt": db.now_iso(),
    })
    _audit_prepend(run_id, _audit_event(audit_action))
    run["status"] = status
    run["updatedAt"] = db.now_iso()
    db.run_set(run_id, run)


def run_trackb_and_persist(run_id: str, mode: str, systems: List[str], run_inputs: Dict[str, Any]) -> None:
    run = db.run_get(run_id)
    if run is None:
        raise RuntimeError(f"Run '{run_id}' not found — cannot materialise")

    per_system: Dict[str, str] = {s: "skipped" for s in ["salesforce", "servicenow", "jira"]}
    errors: Dict[str, str] = {}

    try:
        per_system, succeeded, probe_errors = _probe_systems(systems, mode)
        errors.update(probe_errors)

        if not succeeded:
            _finalise(run_id, run, "failed", mode, systems, per_system,
                      {"opportunities": 0, "evidence": 0}, errors, "DISCOVERY_FAILED")
            return

        from discovery.runner import run as trackb_run
        from discovery.track_a_adapter import export_track_a_seed

        payload = trackb_run(mode=mode, systems=succeeded, run_id=run_id)
        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev   = seed.get("evidence", [])

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)

        # T3 — compute and store cross-system linked clusters
        try:
            from .materialize_t3_hook import compute_and_store_clusters
            compute_and_store_clusters(run_id, ev)
        except Exception as e:
            errors["clusters"] = str(e)

        # roadmap
        try:
            from .roadmap_engine import build_roadmap
            db.run_kv_set("roadmap", run_id, build_roadmap(opps))
        except Exception as e:
            errors["roadmap"] = str(e)

        # executive report
        try:
            from .executive_report_engine import build_executive_report
            roadmap = db.run_kv_get("roadmap", run_id, {})
            er = build_executive_report(run_id=run_id, opps=opps, roadmap=roadmap)

            sa = er.get("sourcesAnalyzed", {})
            sa["totalConnected"] = len(run_inputs.get("connectedSources", []))
            sa["uploadedFiles"] = len(run_inputs.get("uploadedFiles", []))
            sa["sampleWorkspaceEnabled"] = bool(run_inputs.get("sampleWorkspaceEnabled", False))
            er["sourcesAnalyzed"] = sa

            db.run_kv_set("executive_report", run_id, er)
        except Exception as e:
            errors["exec_report"] = str(e)
            db.run_kv_set("executive_report", run_id, {
                "confidence": "Moderate",
                "sourcesAnalyzed": {
                    "recommendedConnected": 0,
                    "totalConnected": len(run_inputs.get("connectedSources", [])),
                    "uploadedFiles": len(run_inputs.get("uploadedFiles", [])),
                    "sampleWorkspaceEnabled": bool(run_inputs.get("sampleWorkspaceEnabled", False))
                },
                "topQuickWins": [],
                "snapshotBubbles": [],
                "roadmapHighlights": []
            })

        # T6 — LLM enrichment (post-processing, non-blocking)
        try:
            from .llm_enrichment import run_llm_enrichment, KV_LLM_ENRICHMENT
            exec_report = db.run_kv_get("executive_report", run_id, {})
            sources_analyzed = exec_report.get("sourcesAnalyzed", {})
            enrichment = run_llm_enrichment(
                run_id=run_id, opps=opps, evidence=ev,
                sources_analyzed=sources_analyzed,
            )
            db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)
            if enrichment.get("executiveSummary"):
                exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
                db.run_kv_set("executive_report", run_id, exec_report)
        except Exception as e:
            errors["llm_enrichment"] = str(e)

        status = "complete" if len(succeeded) == len(systems) else "partial"
        audit_action = "DISCOVERY_MATERIALIZED" if status == "complete" else "DISCOVERY_PARTIAL"
        _finalise(run_id, run, status, mode, systems, per_system,
                  {"opportunities": len(opps), "evidence": len(ev)}, errors, audit_action)

    except Exception as e:
        errors["exception"] = str(e)
        _finalise(run_id, run, "failed", mode, systems, per_system,
                  {"opportunities": 0, "evidence": 0}, errors, "DISCOVERY_FAILED")