"""Org-config pack migration API — 2.0-C4 T3 (AT-844) migration assist.

Four endpoints on the pack-migration resource:

    GET  /api/packs/{pack_id}/migration/preview       — what would change (analyst+)
    POST /api/packs/{pack_id}/migration/apply         — apply it            (owner)
    GET  /api/packs/migrations                        — the ledger          (analyst+)
    POST /api/packs/migrations/{migration_id}/revert  — undo one            (owner)

Role rationale
--------------
PREVIEW is ``analyst`` rather than ``viewer``: unlike the deprecation notice itself
(viewer+, on ``GET /api/packs/state``), a preview quotes the org's saved run
configuration back — the same reason ``GET /api/packs/installed/{id}/validation`` is
analyst+. Anyone who needs only to know that a pack is going away already has that
from the notice.

APPLY and REVERT are ``owner``: they rewrite the configuration every future run for
the whole organisation is built from. That is the same bar as disabling a pack or
connecting a connector, and deliberately a step above the analyst who reviews
findings.

Every read and write is org-scoped through ``get_current_org_id()``. A request body
never carries an org id.

Confirmation
------------
``apply`` requires ``confirm: true`` in the body, and accepts the ``fingerprint`` the
caller previewed. A UI that renders the change set and posts the fingerprint back
gets the parent story's "previewed before applying" as an enforced property rather
than a convention: if the configuration or the declaration moved in between, the
apply is refused with a 409 instead of applying a change set nobody saw.

Status codes
------------
* **404** — unknown pack id, or unknown migration id.
* **409** — nothing to migrate (not deprecated / no replacement named), a stale
  fingerprint, an already-reverted migration, or a revert whose target fields have
  since been edited.
* **200** — including an apply that had nothing to change, which reports
  ``changed: false`` (idempotent, mirroring ``PUT /api/packs/{id}/state``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .middleware.audit import (
    PACK_MIGRATION_APPLIED,
    PACK_MIGRATION_REVERTED,
    log_event,
)
from .middleware.tenancy import get_current_org_id
from .pack_migration import (
    MigrationRecord,
    PackMigrationConflict,
    PackMigrationNotFound,
    PackMigrationUnavailable,
    apply_migration,
    migration_history,
    preview_migration,
    revert_migration,
)
from .pack_state import PackNotFound
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["packs"])


class MigrationApplyRequest(BaseModel):
    """Confirmation of a previewed migration."""

    confirm: bool = Field(
        default=False,
        description=(
            "Must be true. An explicit confirmation, so a migration can never be the "
            "side effect of a request that merely looked at the plan."
        ),
    )
    fingerprint: Optional[str] = Field(
        default=None,
        max_length=128,
        description=(
            "The fingerprint returned by the preview being confirmed. When supplied "
            "and no longer current, the apply is refused with 409 rather than "
            "applying a change set the caller did not see."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional operator note recorded on the migration ledger and the audit "
            "trail (e.g. 'moving to cloud_ops ahead of the grace period')."
        ),
    )


class MigrationRevertRequest(BaseModel):
    """Undo an applied migration, restoring the configuration it replaced."""

    force: bool = Field(
        default=False,
        description=(
            "Restore the pre-migration values even though the configuration has been "
            "edited since. Without it such a revert is refused with 409, because it "
            "would silently discard that edit."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional operator note recorded on the ledger and audit trail.",
    )


def _pack_not_found(pack_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown pack '{pack_id}'")


@router.get(
    "/migrations",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    summary="Append-only history of this org's pack migrations",
)
def list_pack_migrations() -> Dict[str, Any]:
    """Newest-first migration ledger (the repo convention for audit lists).

    Reverting does not erase the migration it undoes: both rows are here, and each
    applied migration reports whether a later revert has undone it.
    """
    org_id = get_current_org_id()
    return {
        "orgId": org_id,
        "migrations": [record.to_dict() for record in migration_history(org_id)],
    }


@router.get(
    "/{pack_id}/migration/preview",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    summary="Preview the org-config migration from a deprecated pack to its replacement",
)
def preview_pack_migration(pack_id: str) -> Dict[str, Any]:
    """The exact change set a migration would make. Writes nothing.

    A pack that is not deprecated, or that names no registered replacement, is a
    **200** with ``available: false`` and a reason — those are states a surface has to
    explain to the customer, not errors. ``applicable`` is the narrower "and there is
    actually something in this org's configuration to change".
    """
    org_id = get_current_org_id()
    try:
        return preview_migration(org_id, pack_id).to_dict()
    except PackNotFound:
        raise _pack_not_found(pack_id)


@router.post(
    "/{pack_id}/migration/apply",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="Apply the previewed migration to this org's saved run configuration",
)
def apply_pack_migration(
    pack_id: str,
    body: MigrationApplyRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Rewrite this org's pack and template selections onto the replacement pack.

    Affects FUTURE runs only. Historical runs, findings, and evidence keep the pack
    they were produced with — a migration is a forward-looking configuration change,
    never a rewrite of what already happened.
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm must be true — preview the migration and confirm it",
        )

    try:
        record = apply_migration(
            org_id,
            pack_id,
            actor_id=actor_id,
            reason=body.reason,
            expected_fingerprint=body.fingerprint,
        )
    except PackNotFound:
        raise _pack_not_found(pack_id)
    except PackMigrationUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PackMigrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # A no-op apply is not an audit event — only a real configuration change is
    # (the same rule as a no-op pack state transition).
    if record.changed:
        _audit(PACK_MIGRATION_APPLIED, org_id, actor_id, record)
        _record_telemetry("pack.migration_applied", record)
    return record.to_dict()


@router.post(
    "/migrations/{migration_id}/revert",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="Revert an applied pack migration, restoring the previous configuration",
)
def revert_pack_migration(
    migration_id: str,
    body: MigrationRevertRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Restore the configuration the migration replaced (parent-story AC2).

    Appends a revert row rather than removing the applied one, so the trail still
    shows that the migration happened and was undone.
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)

    try:
        record = revert_migration(
            org_id,
            migration_id,
            actor_id=actor_id,
            reason=body.reason,
            force=body.force,
        )
    except PackMigrationNotFound:
        raise HTTPException(
            status_code=404, detail=f"unknown migration '{migration_id}'"
        )
    except PackMigrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    _audit(PACK_MIGRATION_REVERTED, org_id, actor_id, record, forced=body.force)
    _record_telemetry("pack.migration_reverted", record, forced=body.force)
    return record.to_dict()


def _changed_fields(record: MigrationRecord) -> List[str]:
    return [change.field for change in record.changes]


def _audit(
    event_type: str,
    org_id: str,
    actor_id: str,
    record: MigrationRecord,
    *,
    forced: bool = False,
) -> None:
    """Place the transition in the org-wide audit stream (parent-story AC4).

    Field NAMES and counts only — the configuration values themselves live on the
    migration ledger, which is the domain record.
    """
    log_event(
        event_type,
        org_id=org_id,
        user_id=actor_id,
        migration_id=record.id,
        pack_id=record.pack_id,
        replacement_pack_id=record.replacement_pack_id,
        fields=_changed_fields(record),
        change_count=len(record.changes),
        unmapped_count=len(record.unmapped),
        reverts_migration_id=record.reverts_migration_id,
        forced=forced,
    )


def _record_telemetry(
    event_type: str, record: MigrationRecord, *, forced: bool = False
) -> None:
    """Mirror the transition into telemetry. Never fails the request.

    Observability only: the configuration change is already persisted and audited by
    the time this runs, so a telemetry failure must not turn a successful migration
    into an error.
    """
    from .telemetry import record_event

    try:
        record_event(
            event_type,
            {
                "org_id": record.org_id,
                "migration_id": record.id,
                "pack_id": record.pack_id,
                "replacement_pack_id": record.replacement_pack_id,
                "fields": _changed_fields(record),
                "change_count": len(record.changes),
                "unmapped_count": len(record.unmapped),
                "warnings": [warning.code for warning in record.warnings],
                "forced": forced,
                "reverts_migration_id": record.reverts_migration_id,
                "actor_id": record.actor_id,
                "at": record.at,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("%s telemetry failed (non-blocking)", event_type, exc_info=True)


def register_pack_migration_routes(app: FastAPI) -> None:
    """Attach the pack-migration routes exactly once (idempotent)."""
    existing = {getattr(route, "path", None) for route in app.routes}
    if "/api/packs/migrations" in existing:
        return
    app.include_router(router)


__all__ = [
    "MigrationApplyRequest",
    "MigrationRevertRequest",
    "register_pack_migration_routes",
    "router",
]
