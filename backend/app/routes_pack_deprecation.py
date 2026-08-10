"""Deprecation lifecycle audit API — 2.0-C4 T5 (AT-846).

One endpoint:

    GET /api/packs/deprecation/audit   — the three transitions, newest first (owner)

Why this exists when ``GET /api/audit-log`` already serves the same rows
------------------------------------------------------------------------
Reachable is not the same as usable. ``/api/audit-log`` is an unfiltered, paginated
firehose across every event type in the product; answering "what has happened to this
pack's deprecation lifecycle" by scrolling it is archaeology, not audit. This endpoint
selects exactly the three transitions, stamps each row with WHICH transition it is,
and optionally narrows to one pack.

Role
----
**Owner**, deliberately the same bar as ``/api/audit-log`` itself. These are the same
audit rows viewed through a narrower lens, so serving them to a lower role would be a
privilege bypass dressed as a convenience — the row is either audit-sensitive or it is
not, and the shape of the query does not change that.

Read posture
------------
A read failure raises rather than degrading to an empty list. Every other read in the
pack-lifecycle surface is fail-soft because a missing LABEL is better than a blocked
page — but an audit surface reporting "nothing happened" when it simply could not read
is worse than one reporting an error, and it is the one place that distinction can
mislead a reviewer.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Query

from .middleware.tenancy import get_current_org_id
from .pack_deprecation_audit import (
    DEPRECATION_AUDIT_EVENTS,
    DEPRECATION_AUDIT_EVENT_TYPES,
    deprecation_audit_trail,
)
from .rbac import require_role
from .security import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["packs"])


@router.get(
    "/deprecation/audit",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="The deprecation, migration, and post-grace disable trail for this org",
)
def get_deprecation_audit(
    packId: Optional[str] = Query(
        default=None,
        description="Narrow the trail to one pack. Omit for the whole org.",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
) -> Dict[str, Any]:
    """Newest-first, the repo convention for audit lists.

    ``transitions`` names the three the parent story requires and the audit event
    types that record each, so a consumer can render the trail without hard-coding
    that ``pack_migration_applied`` and ``pack_migration_reverted`` are two halves of
    one transition.
    """
    org_id = get_current_org_id()
    entries: List[Dict[str, Any]] = deprecation_audit_trail(
        org_id, pack_id=packId, limit=limit
    )
    return {
        "orgId": org_id,
        "packId": packId,
        "eventTypes": list(DEPRECATION_AUDIT_EVENT_TYPES),
        "transitions": {
            transition: list(events)
            for transition, events in DEPRECATION_AUDIT_EVENTS.items()
        },
        "entries": entries,
    }


def register_pack_deprecation_routes(app: FastAPI) -> None:
    """Attach the deprecation-audit route exactly once (idempotent)."""
    existing = {getattr(route, "path", None) for route in app.routes}
    if "/api/packs/deprecation/audit" in existing:
        return
    app.include_router(router)


__all__ = ["register_pack_deprecation_routes", "router"]
