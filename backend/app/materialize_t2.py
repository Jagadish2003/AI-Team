import time
from typing import Dict, Any, List
from fastapi import HTTPException
from . import db

def _now_epoch() -> int:
    return int(time.time())

def set_status(run_id: str, payload: Dict[str, Any]) -> None:
    db.run_kv_set("status", run_id, payload)

def get_status(run_id: str) -> Dict[str, Any]:
    return db.run_kv_get("status", run_id, {"runId": run_id, "status": "running"})

def _audit_prepend(run_id: str, event: Dict[str, Any]) -> None:
    audit = db.run_kv_get("audit", run_id, [])
    db.run_kv_set("audit", run_id, [event] + audit)

def _audit_event(action: str, by: str = "System") -> Dict[str, Any]:
    return {"id": f"aud_{_now_epoch()}", "tsLabel": db.now_iso(), "tsEpoch": _now_epoch(), "action": action, "by": by}

def run_trackb_and_persist(run_id: str, mode: str, systems: List[str], run_inputs: Dict[str, Any]) -> None:
    run = db.run_get(run_id)
    if run is None:
        raise RuntimeError(f"Run '{run_id}' not found — cannot materialise")

    per_system = {s: "skipped" for s in ["salesforce","servicenow","jira"]}
    errors: Dict[str,str] = {}

    try:
        from discovery.runner import run as trackb_run
        from discovery.track_a_adapter import export_track_a_seed

        payload = trackb_run(mode=mode, systems=systems, run_id=run_id)
        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev   = seed.get("evidence", [])

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)

        # roadmap + exec report are best-effort here (don’t fail the whole run)
        try:
            from .roadmap_engine import build_roadmap
            db.run_kv_set("roadmap", run_id, build_roadmap(opps))
        except Exception as e:
            errors["roadmap"] = str(e)

        try:
            from .executive_report_engine import build_executive_report
            roadmap = db.run_kv_get("roadmap", run_id, {})
            db.run_kv_set("executive_report", run_id, build_executive_report(run_id=run_id, opps=opps, roadmap=roadmap))
        except Exception:
            db.run_kv_set("executive_report", run_id, {
                "confidence":"Moderate",
                "sourcesAnalyzed":{"recommendedConnected":0,"totalConnected":len(run_inputs.get("connectedSources",[]))},
                "topQuickWins":[],
                "snapshotBubbles":[],
                "roadmapHighlights":[]
            })

        # Track B does not currently emit per-system ingestion success flags.
        # For Sprint 4, treat materialization as all-or-nothing: if Track B completes
        # without raising, mark all requested systems as ok.
        per_system = {s: "ok" for s in systems}
        status = "complete"
        set_status(run_id, {
            "runId": run_id,
            "status": status,
            "modeUsed": mode,
            "systemsUsed": systems,
            "perSystem": per_system,
            "counts": {"opportunities": len(opps), "evidence": len(ev)},
            "errors": errors,
            "updatedAt": db.now_iso(),
        })

        _audit_prepend(run_id, _audit_event(
            "DISCOVERY_MATERIALIZED" if status=="complete" else ("DISCOVERY_PARTIAL" if status=="partial" else "DISCOVERY_FAILED")
        ))

        run["status"]=status
        run["updatedAt"]=db.now_iso()
        db.run_set(run_id, run)

    except Exception as e:
        set_status(run_id, {
            "runId": run_id,
            "status": "failed",
            "modeUsed": mode,
            "systemsUsed": systems,
            "perSystem": per_system,
            "counts": {"opportunities": 0, "evidence": 0},
            "errors": {"exception": str(e)},
            "updatedAt": db.now_iso(),
        })
        _audit_prepend(run_id, _audit_event("DISCOVERY_FAILED"))
        run["status"]="failed"
        run["updatedAt"]=db.now_iso()
        db.run_set(run_id, run)
