import time
from fastapi import Depends, BackgroundTasks, Body
from .security import require_auth
from . import db
from .models_t2 import StartRunRequest, StartRunResponse, StatusResponse
from .materialize_t2 import set_status, get_status, run_trackb_and_persist
from .replay import seed_events

ALL_SYSTEMS = ["salesforce", "servicenow", "jira"]
def _epoch() -> int:
    return int(time.time())

def register_sprint4_t2_routes(app):

    @app.post("/api/runs/start", response_model=StartRunResponse, dependencies=[Depends(require_auth)])
    async def start_run(body: StartRunRequest, background_tasks: BackgroundTasks):
        """
        Creates a new run record, marks it RUNNING, and schedules Track B materialization
        in the background. Returns immediately with runId.
        """
        run_id = f"run_{_epoch()}"

        run = {
        "id": run_id,
        "status": "running",
        "startedAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "inputs": {
            "connectedSources": body.connectedSources,
            "uploadedFiles": body.uploadedFiles,
            "sampleWorkspaceEnabled": body.sampleWorkspaceEnabled,
        },
        }
        db.run_set(run_id, run)
        # Seed event stream so replay determinism holds: replay resets to these same events.
        db.kv_set(f"events:{run_id}", seed_events())

        # Status document is separate from the run record to keep status polling cheap.
        set_status(
        run_id,
        {
            "runId": run_id,
            "status": "running",
            "modeUsed": body.mode,
            "systemsUsed": body.systems,
            "perSystem": {s: "skipped" for s in ALL_SYSTEMS},
            "counts": {"opportunities": 0, "evidence": 0},
            "errors": {},
            "updatedAt": db.now_iso(),
        },
        )

        background_tasks.add_task(
        run_trackb_and_persist,
        run_id,
        body.mode,
        body.systems,
        run["inputs"],
        )

        return StartRunResponse(runId=run_id, status="running", startedAt=run["startedAt"])

    @app.get("/api/runs/{run_id}/status", response_model=StatusResponse, dependencies=[Depends(require_auth)])
    def run_status(run_id: str):
        db.run_get(run_id)
        s = get_status(run_id)
        s["runId"] = run_id
        return s
