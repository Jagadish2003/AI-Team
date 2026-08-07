import logging
from fastapi import BackgroundTasks, Depends

from . import db
from .materialize_t2 import get_status, run_trackb_and_persist, set_status
from .middleware.audit import log_event
from .middleware.tenancy import get_current_org_id
from .models_t2 import StartRunRequest, StartRunResponse, StatusResponse
from .rbac import require_role, _get_user_id_from_token
from .replay import seed_events
from .security import require_auth

ALL_SYSTEMS = ["salesforce", "servicenow", "jira"]


def register_sprint4_t2_routes(app):

    @app.post(
        "/api/runs/start",
        response_model=StartRunResponse,
        # require_role already depends on require_auth, and the handler resolves
        # the token via Depends(require_auth) below — listing it here too is
        # redundant, so it is omitted (auth still runs, exactly once).
        dependencies=[Depends(require_role("analyst"))],
    )
    async def start_run(
        body: StartRunRequest,
        background_tasks: BackgroundTasks,
        token: str = Depends(require_auth),
    ):
        """
        Creates a new run record, marks it RUNNING, and schedules Track B materialization
        in the background. Returns immediately with runId.
        """
        run_id = db.next_run_id()

        # Capture the caller's org so the background materializer can resolve
        # this org's authenticated connectors / OAuth tokens (CS-2 live ingest).
        org_id = get_current_org_id()

        run = {
            "id": run_id,
            "status": "running",
            "startedAt": db.now_iso(),
            "updatedAt": db.now_iso(),
            "orgId": org_id,
            "inputs": {
                "connectedSources": body.connectedSources,
                "uploadedFiles": body.uploadedFiles,
                "sampleWorkspaceEnabled": body.sampleWorkspaceEnabled,
                "orgId": org_id,
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

        log_event(
            "run_started",
            run_id=run_id,
            user_id=_get_user_id_from_token(token),
            pack_id=body.mode,
            system_ids=body.systems,
        )

        return StartRunResponse(
            runId=run_id, status="running", startedAt=run["startedAt"]
        )

    @app.get(
        "/api/runs/{run_id}/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def run_status(run_id: str):
        run = db.run_get(
            run_id
        )  # ← capture the run record (still raises 404 if missing)
        s = get_status(run_id)
        s["runId"] = run_id
        s["isReplay"] = run.get("isReplay", False)  # ← add isReplay from run record
        s["current_step"] = run.get("current_step")
        # CS-4 / AT-313: steps whose ingest failed, so the progress UI can render
        # them as failed instead of as completed (green-check) stages.
        s["failed_steps"] = run.get("failed_steps", [])
        # 2.0-D4 T5 (AC6): a run that finished is not necessarily a run that
        # delivered everything. Without this, a run whose ServiceNow died polls
        # as "complete" and every consumer downstream believes it.
        try:
            from .run_completeness import build_run_completeness

            s["completeness"] = build_run_completeness(
                run, include_environment=False
            ).to_dict()
        except Exception:  # noqa: BLE001 - status must always answer
            logging.getLogger(__name__).warning(
                "Could not attach run completeness", exc_info=True
            )
        return s
