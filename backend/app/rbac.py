"""RBAC — AT-82 / T1-S10-B T7.

require_role(minimum_role) is a FastAPI dependency factory.
Role hierarchy: owner > analyst > viewer.

Usage (always pair with require_auth):
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))]
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException

from app import db
from app.middleware.tenancy import get_current_org_id
from app.security import require_auth
from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

logger = logging.getLogger(__name__)

_ROLE_RANK: dict[str, int] = {"owner": 3, "analyst": 2, "viewer": 1}

_MEMBERS_INITIALISED = False


def _ensure_members_table() -> None:
    """Create the workspace_members table if it does not exist (idempotent).

    Fail-soft: a failure here (e.g. a concurrent creator winning the race, or a
    read-only replica) is rolled back and swallowed so require_role still reaches
    its own query and returns a clean 403 rather than surfacing a 500.
    """
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def seed_owner(org_id: str, user_id: str) -> None:
    """Insert the workspace creator as owner (idempotent).

    Reactivates on conflict: if the owner row was ever soft-deleted (is_deleted),
    restore it to an active owner rather than leaving the seed user locked out.
    """
    _ensure_members_table()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (%s, %s, 'owner', %s)
            ON CONFLICT (org_id, user_id)
            DO UPDATE SET role = 'owner', is_deleted = FALSE
            """,
            (org_id, user_id, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def get_user_role(org_id: str, user_id: str) -> str | None:
    """Return the user's role in org, or None if not a member."""
    _ensure_members_table()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT role FROM workspace_members "
            "WHERE org_id = %s AND user_id = %s AND is_deleted = FALSE",
            (org_id, user_id),
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _get_user_id_from_token(token: str) -> str:
    """Derive a stable user_id from the bearer token.

    AUTH-1 JWTs carry the user_id in their `sub` claim (workspace_members rows
    are keyed on that UUID). Static dev/test tokens are not valid JWTs, so the
    decode returns None and the token string itself is the identifier — a row is
    seeded with user_id = token for those (unchanged dev behaviour).

    Fail-closed: the signature (and expiry) are verified via decode_signed before
    the `sub` claim is trusted. A forged or tampered token yields None here, so
    even a caller that reaches this without an upstream require_auth cannot be
    tricked into attributing an action to a spoofed `sub` (privilege-escalation /
    audit-spoofing guard). This mirrors security._jwt_role's fail-closed decode.
    """
    try:
        from app.auth.user_auth import decode_signed

        payload = decode_signed(token)
        if payload:
            sub = payload.get("sub")
            if sub:
                return sub
    except Exception:
        pass
    return token


def require_role(minimum_role: str) -> Callable:
    """Return a FastAPI dependency that enforces a minimum role.

    Must be used together with require_auth:
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))]
    """
    if minimum_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role: {minimum_role!r}")

    def _dependency(token: str = Depends(require_auth)) -> None:
        org_id = get_current_org_id()
        user_id = _get_user_id_from_token(token)
        role = get_user_role(org_id, user_id)

        if role is None:
            logger.warning("RBAC 403: user %s has no role in org %s", user_id, org_id)
            raise HTTPException(status_code=403, detail="Insufficient role")

        if _ROLE_RANK.get(role, 0) < _ROLE_RANK[minimum_role]:
            logger.warning(
                "RBAC 403: user %s role=%s below minimum=%s in org %s",
                user_id,
                role,
                minimum_role,
                org_id,
            )
            raise HTTPException(status_code=403, detail="Insufficient role")

    return _dependency
