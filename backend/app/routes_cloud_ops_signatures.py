"""Read-only access to a run's assembled cloud-event signature rows.

``GET /api/runs/{runId}/cloud-ops/event-signatures``

Why this exists
---------------
A discovery run's cloud ``event_signature`` values had no read surface. They are
built by ``discovery/cloud_ops_runtime.build_cloud_ops_runtime`` into
``sn_data["cloud_ops"]["event_signatures"]``, handed to the Cloud Operations
detectors, and then discarded with ``sn_data``: ``runner.build_org_context`` does
not carry the block, the run payload keeps only the ``cloudOpsRuntime`` health
COUNT, and the native cloud connectors set ``produces_retrieval_content = False``
so no per-event telemetry is emitted. The only way a signature VALUE used to
survive a run was inside a FIRED finding's evidence — and no event-consuming
cloud_ops detector can fire until a ServiceNow incident already carries that
signature, so the values were unreachable exactly when they were needed to create
those incidents.

``discovery/runner._persist_cloud_ops_event_signatures`` now records the rows
verbatim under the run-scoped KV key ``cloud_ops_event_signatures``; this route
serves them back.

Posture
-------
* **Read-only.** No POST/PATCH/PUT/DELETE. The route never recomputes, re-polls,
  or re-derives anything — it returns exactly what the run stored, so a value read
  here is provably the value the detectors saw.
* **Analyst+**, matching the other run-scoped diagnostic surfaces.
* **Org-scoped** against the run record, so one tenant cannot read another's rows.
* A run that predates this write, or one where no cloud_ops pack was selected,
  returns ``count: 0`` with ``available: false`` — an honest "not recorded",
  never a fabricated or zero-filled row set.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth

#: Must match ``discovery/runner.KV_CLOUD_OPS_EVENT_SIGNATURES``.
KV_CLOUD_OPS_EVENT_SIGNATURES = "cloud_ops_event_signatures"


class EventSignaturesResponse(BaseModel):
    """The run's recorded cloud-event signature rows.

    ``rows`` are the untouched detector-input rows (``signature``, ``event_count``,
    ``recurring``, ``first_seen``/``last_seen``, ``resource_id``, ``resource_type``,
    ``event_class``, ``source_systems``, ``window_overlap``, ``incident_ids``,
    ``correlation_windows``, ``evidence_pointers``, …), so no field a caller needs
    to author a matching ServiceNow incident is filtered out here.
    """

    runId: str
    available: bool
    capturedAt: Optional[str] = None
    count: int
    signatures: List[str]
    rows: List[Dict[str, Any]]


def _run_org_id(run: Dict[str, Any]) -> Optional[str]:
    for key in ("orgId", "org_id"):
        value = run.get(key)
        if value:
            return str(value)
    return None


def register_cloud_ops_signature_routes(app: FastAPI) -> None:
    """Attach the route exactly once (idempotent, mirroring the other modules)."""
    path = "/api/runs/{run_id}/cloud-ops/event-signatures"
    if getattr(app.state, "cloud_ops_signature_routes_registered", False):
        return
    if path in {getattr(route, "path", None) for route in app.routes}:
        app.state.cloud_ops_signature_routes_registered = True
        return

    @app.get(
        path,
        response_model=EventSignaturesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        tags=["cloud-ops"],
    )
    def get_run_cloud_ops_event_signatures(run_id: str) -> EventSignaturesResponse:
        """Return the cloud-event signature rows this run assembled.

        404 when the run does not exist or belongs to another org. A run with no
        recorded rows answers ``available: false`` rather than inventing any.
        """
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        run_org = _run_org_id(run)
        # Same posture as the graph/retrieval routes: the org comes from the
        # tenancy context, never from the request, and a cross-org run is simply
        # not found rather than confirming its existence.
        if run_org and run_org != get_current_org_id():
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        stored = db.run_kv_get(KV_CLOUD_OPS_EVENT_SIGNATURES, run_id, None)
        if not isinstance(stored, dict):
            return EventSignaturesResponse(
                runId=run_id, available=False, count=0, signatures=[], rows=[]
            )

        rows = [row for row in (stored.get("rows") or []) if isinstance(row, dict)]
        return EventSignaturesResponse(
            runId=run_id,
            available=True,
            capturedAt=stored.get("capturedAt"),
            # Derived from the stored rows, never a carried-forward number, so the
            # count can never disagree with what is returned.
            count=len(rows),
            signatures=[
                str(row["signature"]) for row in rows if row.get("signature")
            ],
            rows=rows,
        )

    app.state.cloud_ops_signature_routes_registered = True
