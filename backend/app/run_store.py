import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .db import init_tables, upsert_run, require_run_exists, delete_run_events, insert_run_events, get_run_events

SEED_DIR = Path(os.getenv("SEED_DIR", "backend/seed"))
SEED_EVENTS_FILE = SEED_DIR / "run_events.json"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_seed_events() -> List[Dict[str, Any]]:
    if SEED_EVENTS_FILE.exists():
        return json.loads(SEED_EVENTS_FILE.read_text(encoding="utf-8"))
    raise RuntimeError(f"Seed events file missing: {SEED_EVENTS_FILE}. Create backend/seed/run_events.json")
def start_run_(inputs: Dict[str, Any]) -> Dict[str, Any]:
    init_tables()
    run_id = f"run_{uuid.uuid4().hex[:6]}"
    started = now_iso()
    run = {"id": run_id, "status": "running", "startedAt": started, "updatedAt": started, "inputs": inputs}
    upsert_run(run_id, run)
    # Deterministic per-run events
    events = load_seed_events()
    delete_run_events(run_id)
    insert_run_events(run_id, events)
    return {"runId": run_id, "status": "running", "startedAt": started}

def replay_run(run_id: str) -> Dict[str, Any]:
    init_tables()
    run = require_run_exists(run_id)

    # Replay reset definition (backend-only, H-min):
    # - status -> running
    # - updatedAt refreshed
    # - events reset to seed ordering
    # - decisions/overrides are NOT reset in H-min
    run["status"] = "running"
    run["updatedAt"] = now_iso()
    upsert_run(run_id, run)

    events = load_seed_events()
    delete_run_events(run_id)
    insert_run_events(run_id, events)
    return {"ok": True, "runId": run_id}

def read_run(run_id: str) -> Dict[str, Any]:
    init_tables()
    return require_run_exists(run_id)

def read_run_events(run_id: str) -> List[Dict[str, Any]]:
    init_tables()
    require_run_exists(run_id)
    return get_run_events(run_id)
