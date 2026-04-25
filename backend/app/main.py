import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .db import get_all, get_one, upsert, kv_get, kv_set, run_get
from .security import require_auth
from .run_store import start_run_, read_run, read_run_events
from .roadmap_engine import build_roadmap
from .replay import replay_run as replay_run_

from .routes_sprint4_t1 import register_sprint4_t1_routes
from .routes_sprint4_t2 import register_sprint4_t2_routes
from .routes_sprint4_t3 import register_sprint4_t3_routes
from .routes_sprint4_t4 import register_sprint4_t4_routes

app = FastAPI(title="AgentIQ Layer 1 API Skeleton", version="0.1.0")

register_sprint4_t4_routes(app)
register_sprint4_t3_routes(app)
register_sprint4_t2_routes(app)
register_sprint4_t1_routes(app)

origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:5176").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def run_kv_get(key: str, run_id: str, default: Any = None) -> Any:
    value = kv_get(f"{key}:{run_id}")
    return value if value is not None else default

def run_kv_set(key: str, run_id: str, value: Any) -> None:
    kv_set(f"{key}:{run_id}", value)

def default_audit() -> List[Dict[str, Any]]:
    return [
        {
            "id": "audit_seed_001",
            "tsLabel": "18 Mar 2026, 10:12",
            "tsEpoch": 1773828720,
            "action": "DISCOVERY_STARTED",
            "by": "System",
        },
        {
            "id": "audit_seed_002",
            "tsLabel": "18 Mar 2026, 10:15",
            "tsEpoch": 1773828900,
            "action": "ANALYSIS_COMPLETE",
            "by": "System",
        },
    ]

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "ts": now_iso()}

@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    return {"ok": True, "ts": now_iso()}

@app.get("/api/connectors", dependencies=[Depends(require_auth)])
def list_connectors() -> List[Dict[str, Any]]:
    return get_all("connectors")

@app.post("/api/connectors/{connector_id}/connect", dependencies=[Depends(require_auth)])
def connect_connector(connector_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    c = get_one("connectors", connector_id)
    if not c:
        raise HTTPException(404, "connector not found")
    status = body.get("status", "connected")
    c["status"] = status
    c["lastSynced"] = "Just now" if status == "connected" else c.get("lastSynced", "Not connected")
    upsert("connectors", connector_id, c)
    return c

@app.get("/api/confidence/explanation", dependencies=[Depends(require_auth)])
def confidence_explanation() -> Dict[str, Any]:
    return {
        "level": "MEDIUM",
        "why": ["Missing Microsoft 365 signals"],
        "nextAction": "Connect Microsoft 365 to reach High confidence",
        "recommendedNextSourceId": "m365"
    }

@app.get("/api/permissions", dependencies=[Depends(require_auth)])
def list_permissions() -> List[Dict[str, Any]]:
    return get_all("permissions")

@app.get("/api/uploads", dependencies=[Depends(require_auth)])
def list_uploads() -> List[Dict[str, Any]]:
    return get_all("uploads")

@app.post("/api/uploads", dependencies=[Depends(require_auth)])
def add_upload(body: Dict[str, Any]) -> Dict[str, Any]:
    name = body.get("name")
    size_label = body.get("sizeLabel", "—")
    if not name:
        raise HTTPException(400, "name required")
    item = {"id": f"up_{uuid.uuid4().hex[:6]}", "name": name, "sizeLabel": size_label, "uploadedLabel": "Just now"}
    upsert("uploads", item["id"], item)
    return item

# @app.post("/api/runs/start", dependencies=[Depends(require_auth)])
# def start_run(body: Dict[str, Any]) -> Dict[str, Any]:
#     return start_run_(body)

@app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth)])
def get_run(run_id: str) -> Dict[str, Any]:
    try:
        return read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

@app.get("/api/runs/{run_id}/events", dependencies=[Depends(require_auth)])
def get_events(run_id: str) -> List[Dict[str, Any]]:
    try:
        return read_run_events(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

# Replay is now handled by routes_sprint4_t4 (registered above)
# @app.post("/api/runs/{run_id}/replay", dependencies=[Depends(require_auth)])
# def replay_run(run_id: str) -> Dict[str, Any]:
#     try:
#         return replay_run_(run_id)
#     except KeyError:
#         raise HTTPException(404, "run not found")

@app.get("/api/runs/{run_id}/evidence", dependencies=[Depends(require_auth)])
def list_evidence(run_id: str) -> List[Dict[str, Any]]:
    run_get(run_id)  # raises 404 if not found
    run_ev = run_kv_get("evidence", run_id, None)
    if run_ev is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No evidence found for run '{run_id}'. "
                "Ensure PATCH_materialize_t2.py has been applied "
                "and the run has completed successfully."
            ),
        )
    return run_ev

@app.post("/api/runs/{run_id}/evidence/{evidence_id}/decision", dependencies=[Depends(require_auth)])
def set_evidence_decision(run_id: str, evidence_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    run_get(run_id)  # raises 404 if run not found

    # Read from run-scoped KV (not global seed table)
    run_ev = run_kv_get("evidence", run_id, None)
    if run_ev is None:
        raise HTTPException(404, "evidence not found for this run")

    idx = next((i for i, e in enumerate(run_ev) if e["id"] == evidence_id), None)
    if idx is None:
        raise HTTPException(404, "evidence not found")

    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")

    run_ev[idx] = {**run_ev[idx], "decision": decision}
    run_kv_set("evidence", run_id, run_ev)      # write back to run KV
    e = run_ev[idx]

    audit_event = {
        "id": f"audit_{uuid4().hex[:8]}",
        "tsLabel": now_iso(),
        "tsEpoch": int(time.time()),
        "action": f"EVIDENCE_{decision}",
        "by": "Architect",
        "evidenceId": evidence_id,
    }
    audit = run_kv_get("audit", run_id, default_audit())
    run_kv_set("audit", run_id, [audit_event, *audit])
    return e

@app.get("/api/runs/{run_id}/entities", dependencies=[Depends(require_auth)])
def list_entities(run_id: str) -> List[Dict[str, Any]]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    return get_all("entities")

@app.get("/api/runs/{run_id}/mappings", dependencies=[Depends(require_auth)])
def list_mappings(run_id: str) -> List[Dict[str, Any]]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    return get_all("mappings")

@app.get("/api/runs/{run_id}/opportunities", dependencies=[Depends(require_auth)])
def list_opportunities(run_id: str) -> List[Dict[str, Any]]:
    # 1. Validate the run exists and handle if not found
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    # 2. Get opportunities from run-scoped storage (no fallback to seed data)
    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=f"No opportunities for run '{run_id}'. T2 materialisation may not have completed."
        )

    # 3. S7 MAP FIX: Add Deterministic Jitter
    # This prevents items with identical integer scores from overlapping perfectly on the UI.
    for opp in opps:
        if "impact" in opp and "effort" in opp:
            # Use the opportunity ID to generate a stable, deterministic offset.
            # This ensures the layout is the same every time and passes replay tests.
            _id = str(opp.get("id", "0"))
            stable_offset = (sum(ord(c) for c in _id) % 5) * 0.15

            # Apply the small offset to separate the bubbles
            opp["impact"] = float(opp["impact"]) + stable_offset
            opp["effort"] = float(opp["effort"]) + stable_offset

    # 4. Return the modified list of opportunities
    return opps

@app.post("/api/runs/{run_id}/opportunities/{opp_id}/decision", dependencies=[Depends(require_auth)])
def set_opp_decision(run_id: str, opp_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")
    run_opps = run_kv_get("opps", run_id)
    if run_opps is not None:
        idx = next((i for i, o in enumerate(run_opps) if o["id"] == opp_id), None)
        if idx is None:
            raise HTTPException(404, "opportunity not found")
        run_opps[idx] = {**run_opps[idx], "decision": decision}
        run_kv_set("opps", run_id, run_opps)
        o = run_opps[idx]
    else:
        o = get_one("opportunities", opp_id)
        if not o:
            raise HTTPException(404, "opportunity not found")
        o["decision"] = decision
        upsert("opportunities", opp_id, o)
    event = {
        "id": f"audit_{uuid4().hex[:8]}",
        "tsLabel": now_iso(),
        "tsEpoch": int(time.time()),
        "action": decision,
        "by": "Architect",
        "opportunityId": opp_id,
    }
    audit = run_kv_get("audit", run_id, default_audit())
    run_kv_set("audit", run_id, [event, *audit])
    return o

@app.post("/api/runs/{run_id}/opportunities/{opp_id}/override", dependencies=[Depends(require_auth)])
def set_opp_override(run_id: str, opp_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    run_opps = run_kv_get("opps", run_id)
    if run_opps is not None:
        idx = next((i for i, o in enumerate(run_opps) if o["id"] == opp_id), None)
        if idx is None:
            raise HTTPException(404, "opportunity not found")
        o = dict(run_opps[idx])
    else:
        o = get_one("opportunities", opp_id)
        if not o:
            raise HTTPException(404, "opportunity not found")
    override = o.get("override") or {}
    override["rationaleOverride"] = body.get("rationaleOverride", override.get("rationaleOverride", ""))
    override["overrideReason"] = body.get("overrideReason", override.get("overrideReason", ""))
    override["isLocked"] = bool(body.get("isLocked", override.get("isLocked", False)))
    override["updatedAt"] = now_iso()
    o["override"] = override
    if run_opps is not None:
        run_opps[idx] = o
        run_kv_set("opps", run_id, run_opps)
    else:
        upsert("opportunities", opp_id, o)
    event = {
        "id": f"audit_{uuid4().hex[:8]}",
        "tsLabel": now_iso(),
        "tsEpoch": int(time.time()),
        "action": "OVERRIDE_SAVED",
        "by": "Architect",
        "opportunityId": opp_id,
    }
    audit = run_kv_get("audit", run_id, default_audit())
    run_kv_set("audit", run_id, [event, *audit])
    return o

@app.get("/api/runs/{run_id}/audit", dependencies=[Depends(require_auth)])
def list_audit(run_id: str) -> List[Dict[str, Any]]:
    run_get(run_id)  # raises 404 if missing
    audit = run_kv_get("audit", run_id, default_audit())
    return sorted(audit, key=lambda e: int(e.get("tsEpoch", 0)), reverse=True)

@app.get("/api/runs/{run_id}/roadmap", dependencies=[Depends(require_auth)])
def get_roadmap(run_id: str) -> Dict[str, Any]:
    run_get(run_id)  # raises 404 if not found
    run_roadmap = run_kv_get("roadmap", run_id, None)
    if run_roadmap is not None:
        return run_roadmap
    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No opportunities for run '{run_id}'. "
                "T2 materialisation has not completed for this run."
            ),
        )
    return build_roadmap(opps)

@app.get("/api/runs/{run_id}/executive-report", dependencies=[Depends(require_auth)])
def get_exec_report(run_id: str) -> Dict[str, Any]:
    try:
        run = read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    # --- REMOVED THE run_exec CACHE CHECK FROM HERE ---

    # sourcesAnalyzed MUST derive from run inputs (not live connector state)
    inputs = run.get("inputs") or {}
    connected_sources = inputs.get("connectedSources") or []
    uploaded_files = inputs.get("uploadedFiles") or []
    sample_enabled = bool(inputs.get("sampleWorkspaceEnabled", False))

    # Determine recommended count from connector catalog
    connectors = get_all("connectors")
    name_to_tier = {c.get("name"): c.get("tier") for c in connectors}
    recommended_connected = sum(1 for n in connected_sources if name_to_tier.get(n) == "recommended")

    sources_analyzed = {
        "recommendedConnected": recommended_connected,
        "totalConnected": len(connected_sources),
        "uploadedFiles": len(uploaded_files),
        "sampleWorkspaceEnabled": sample_enabled,
    }

    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No opportunities for run '{run_id}'. "
                "T2 materialisation has not completed."
            ),
        )

    quick_wins = [o for o in opps if o.get("tier") == "Quick Win"]

    # This return block ensures roadmapHighlights is a DICTIONARY, fixing the test error.
    return {
        "confidence":      "MODERATE",
        "sourcesAnalyzed": sources_analyzed,
        "topQuickWins":    quick_wins,
        "snapshotBubbles": [],
        "roadmapHighlights": {
            "next30Count":  sum(1 for o in opps if o.get("tier") == "Quick Win"),
            "next60Count":  sum(1 for o in opps if o.get("tier") == "Strategic"),
            "next90Count":  sum(1 for o in opps if o.get("tier") == "Complex"),
            "blockerCount": 0,
        },
    }
