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

app = FastAPI(title="AgentIQ Layer 1 API Skeleton", version="0.1.0")

origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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

@app.post("/api/runs/start", dependencies=[Depends(require_auth)])
def start_run(body: Dict[str, Any]) -> Dict[str, Any]:
    return start_run_(body)

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

@app.post("/api/runs/{run_id}/replay", dependencies=[Depends(require_auth)])
def replay_run(run_id: str) -> Dict[str, Any]:
    try:
        return replay_run_(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

@app.get("/api/runs/{run_id}/evidence", dependencies=[Depends(require_auth)])
def list_evidence(run_id: str) -> List[Dict[str, Any]]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    return get_all("evidence")

@app.post("/api/runs/{run_id}/evidence/{evidence_id}/decision", dependencies=[Depends(require_auth)])
def set_evidence_decision(run_id: str, evidence_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    e = get_one("evidence", evidence_id)
    if not e:
        raise HTTPException(404, "evidence not found")
    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")
    e["decision"] = decision
    upsert("evidence", evidence_id, e)
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
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    return get_all("opportunities")

@app.post("/api/runs/{run_id}/opportunities/{opp_id}/decision", dependencies=[Depends(require_auth)])
def set_opp_decision(run_id: str, opp_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    o = get_one("opportunities", opp_id)
    if not o:
        raise HTTPException(404, "opportunity not found")
    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")
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
    o = get_one("opportunities", opp_id)
    if not o:
        raise HTTPException(404, "opportunity not found")
    override = o.get("override") or {}
    override["rationaleOverride"] = body.get("rationaleOverride", override.get("rationaleOverride",""))
    override["overrideReason"] = body.get("overrideReason", override.get("overrideReason",""))
    override["isLocked"] = bool(body.get("isLocked", override.get("isLocked", False)))
    override["updatedAt"] = now_iso()
    o["override"] = override
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
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    return build_roadmap(get_all("opportunities"))

@app.get("/api/runs/{run_id}/executive-report", dependencies=[Depends(require_auth)])
def get_exec_report(run_id: str) -> Dict[str, Any]:
    try:
        run = read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

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

    rep = get_one("executive_reports", "exec_001")
    if rep:
        rep["sourcesAnalyzed"] = sources_analyzed
        return rep

    return {
        "confidence": "MODERATE",
        "sourcesAnalyzed": sources_analyzed,
        "topQuickWins": [],
        "snapshotBubbles": [],
        "roadmapHighlights": {
            "next30Count": 3,
            "next60Count": 2,
            "next90Count": 1,
            "blockerCount": 0
        }
    }
