from __future__ import annotations
from typing import Any, Dict, List
import json
import os

from . import db

SEED_DIR = os.getenv("SEED_DIR", os.path.join(os.path.dirname(__file__), "..", "seed"))

# Deterministic fallback timestamps (only used when seed/events.json is missing).
# Keep these fixed so replay determinism holds even on a fresh clone.
_FALLBACK_TS = "18 Mar 2026, 10:12"

def _load_seed_json(filename: str, default: Any) -> Any:
    path = os.path.join(SEED_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def seed_events() -> List[Dict[str, Any]]:
    fallback = [
        {"id":"ev_run_001","tsLabel": _FALLBACK_TS, "stage":"CONNECT",   "message":"Connected sources loaded", "level":"INFO"},
        {"id":"ev_run_002","tsLabel": _FALLBACK_TS, "stage":"INGEST",    "message":"Ingest completed",        "level":"INFO"},
        {"id":"ev_run_003","tsLabel": _FALLBACK_TS, "stage":"NORMALIZE", "message":"Normalization completed", "level":"INFO"},
        {"id":"ev_run_004","tsLabel": _FALLBACK_TS, "stage":"EXTRACT",   "message":"Entities extracted",      "level":"INFO"},
        {"id":"ev_run_005","tsLabel": _FALLBACK_TS, "stage":"SCORE",     "message":"Opportunities scored",    "level":"INFO"},
    ]
    data = _load_seed_json("events.json", fallback)
    return data if isinstance(data, list) and data else fallback

def replay_run(run_id: str) -> Dict[str, Any]:
    """Backend-only replay reset.
    Aligns with A-Task-3 v1.1 db.py API:
      - run_get(run_id) raises HTTPException(404) automatically if missing
      - run_set(run_id, run) persists run metadata
      - kv_set stores run-scoped artifacts under keys like events:{runId}
    """
    run = db.run_get(run_id)  # raises 404 automatically if not found

    # Update run metadata (keep startedAt stable; bump updatedAt + status)
    started_at = run.get("startedAt") or run.get("started_at") or _FALLBACK_TS
    run.update({
        "id": run_id,
        "status": "running",
        "startedAt": started_at,
        "updatedAt": _FALLBACK_TS,  # deterministic label for demo; backend can move to ISO later
    })
    db.run_set(run_id, run)

    # Reset deterministic event stream
    db.kv_set(f"events:{run_id}", seed_events())

    # Optional: reset review decisions + audit under a feature flag
    if os.getenv("REPLAY_RESETS_DECISIONS", "false").lower() == "true":
        opps = db.kv_get(f"opps:{run_id}") or []
        for o in opps:
            o["decision"] = "UNREVIEWED"
            o["override"] = {
                "isLocked": False,
                "rationaleOverride": "",
                "overrideReason": "",
                "updatedAt": None,
            }
        db.kv_set(f"opps:{run_id}", opps)

        evidence = db.kv_get(f"evidence:{run_id}") or []
        for e in evidence:
            e["decision"] = "UNREVIEWED"
        db.kv_set(f"evidence:{run_id}", evidence)

        db.kv_set(f"audit:{run_id}", [])

    return {"ok": True, "runId": run_id}
