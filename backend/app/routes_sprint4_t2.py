from fastapi import BackgroundTasks, Depends

from . import db
from .materialize_t2 import get_status, run_trackb_and_persist, set_status
from .models_t2 import StartRunRequest, StartRunResponse, StatusResponse
from .replay import seed_events
from .security import require_auth

ALL_SYSTEMS = ["salesforce", "servicenow", "jira"]


def register_sprint4_t2_routes(app):

    @app.post(
        "/api/runs/start",
        response_model=StartRunResponse,
        dependencies=[Depends(require_auth)],
    )
    async def start_run(body: StartRunRequest, background_tasks: BackgroundTasks):
        """
        Creates a new run record, marks it RUNNING, and schedules Track B materialization
        in the background. Returns immediately with runId.
        """
        run_id = db.next_run_id()

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
        # Start with an empty event stream for real-time updates.
        db.kv_set(f"events:{run_id}", [])

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

        return StartRunResponse(
            runId=run_id, status="running", startedAt=run["startedAt"]
        )

    @app.get(
        "/api/runs/{run_id}/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_auth)],
    )
    def run_status(run_id: str):
        run = db.run_get(
            run_id
        )  # ← capture the run record (still raises 404 if missing)
        s = get_status(run_id)
        s["runId"] = run_id
        s["isReplay"] = run.get("isReplay", False)  # ← add isReplay from run record
        return s
