"""R16-A1 / AT-383 (T7) — admin ingestion-checkpoint reset route.

Exposes an explicit, admin-only action to clear a source's ingestion checkpoint
(R16-A1 §3): the next discovery run for that ``(org_id, connector_id)`` then does
a full re-read instead of an incremental one. Used deliberately — e.g. after a
connector schema change — and NEVER triggered automatically.

  POST /api/ingestion/checkpoints/reset   { "connector_id": "<id>" }   (Owner only)

The org is taken from the authenticated tenancy context (never the request body),
so an admin can only reset their own org's checkpoints. Every reset is recorded
twice for traceability: an ``audit_log`` entry (the admin-action trail) and an
``ingestion.checkpoint_reset`` telemetry event (observability) — satisfying AC7.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field

from discovery.ingest import checkpoint_repository as checkpoints

from .middleware import audit
from .middleware.tenancy import get_current_org_id
from .rbac import require_role, _get_user_id_from_token
from .security import require_auth
from .telemetry import record_event

logger = logging.getLogger(__name__)

INGESTION_CHECKPOINT_RESET_PATH = "/api/ingestion/checkpoints/reset"

router = APIRouter(tags=["ingestion"])


class CheckpointResetRequest(BaseModel):
    connector_id: str = Field(..., min_length=1, description="Source connector whose checkpoint to clear.")


class CheckpointResetResponse(BaseModel):
    ok: bool = True
    org_id: str
    connector_id: str
    # True if a checkpoint existed and was cleared; False if there was nothing to
    # clear (the source was already at first-run / no checkpoint).
    cleared: bool


@router.post(
    INGESTION_CHECKPOINT_RESET_PATH,
    response_model=CheckpointResetResponse,
    # require_role("owner") already depends on require_auth internally (see
    # rbac.py), and the token parameter below resolves require_auth to read the
    # acting user for the audit trail. Listing require_auth again here would be a
    # third reference to the same dependency, so the RBAC guard alone is enough.
    dependencies=[Depends(require_role("owner"))],
)
def reset_ingestion_checkpoint(
    body: CheckpointResetRequest,
    token: str = Depends(require_auth),
) -> CheckpointResetResponse:
    """Owner-only: clear a source's ingestion checkpoint (forces a full re-read).

    Explicit admin action — gated to the Owner role and scoped to the caller's
    org. Records the reset in the audit trail and telemetry (AC7).
    """
    org_id = get_current_org_id()
    cleared = checkpoints.reset_checkpoint(org_id, body.connector_id)

    # Audit trail (admin action) — fail-silent by contract; org_id from context.
    audit.log_event(
        audit.INGESTION_CHECKPOINT_RESET,
        connector_id=body.connector_id,
        user_id=_get_user_id_from_token(token),
        had_checkpoint=cleared,
    )
    # Telemetry (observability) — fire-and-forget.
    record_event(
        "ingestion.checkpoint_reset",
        {"org_id": org_id, "connector_id": body.connector_id, "had_checkpoint": cleared},
    )

    return CheckpointResetResponse(org_id=org_id, connector_id=body.connector_id, cleared=cleared)


def register_ingestion_routes(app: FastAPI) -> None:
    """Register the ingestion admin routes once for the provided FastAPI app."""
    if getattr(app.state, "ingestion_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if INGESTION_CHECKPOINT_RESET_PATH in existing_paths:
        app.state.ingestion_routes_registered = True
        return

    app.include_router(router)
    app.state.ingestion_routes_registered = True
