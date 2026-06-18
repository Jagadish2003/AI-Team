"""Workspace member management — T1-S11 Task 2 / Section 3 (AT-154).

Three Owner-only endpoints for managing workspace_members:

    GET    /api/workspace/members            → list members for the caller's org
    POST   /api/workspace/members            → invite a member (analyst|viewer)
    DELETE /api/workspace/members/{user_id}  → remove a member

Security rules (locked):
  * Every route requires both require_auth AND require_role("owner").
  * org_id comes EXCLUSIVELY from the tenancy context (JWT) — never from the
    request body, path, or headers. See middleware/tenancy.get_current_org_id.

Identity model note:
  The dev auth layer treats the bearer token as the user identity, and the
  workspace_members table keys on (org_id, user_id) with no separate email
  column. An invited member is therefore stored with user_id == email: the
  email IS the identifier a real user authenticates with later. GET echoes the
  stored user_id back as the `email` field so the contract shape
  [{user_id, email, role, created_at}] is satisfied without a schema change.

Wire-in (main.py):
    from .routes_workspace import register_workspace_routes
    register_workspace_routes(app)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Response, status
from pydantic import BaseModel

from . import db
from .middleware.audit import log_event
from .middleware.tenancy import get_current_org_id
from .rbac import (
    _ensure_members_table,
    _get_user_id_from_token,
    get_user_role,
    require_role,
)
from .security import require_auth

# Roles an owner may assign when inviting a member. Owners are seeded at
# workspace creation, not invited — so 'owner' is intentionally excluded.
_ASSIGNABLE_ROLES = {"analyst", "viewer"}


# ── Models ────────────────────────────────────────────────────────────────────

class MemberOut(BaseModel):
    user_id: str
    email: str
    role: str
    created_at: str


class MemberInviteRequest(BaseModel):
    email: str
    role: str


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_member(
    user_id: str,
    role: str,
    created_at: str,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a workspace_members row to the contract shape.

    The real email is sourced from the users table (joined on users.id =
    workspace_members.user_id). When no matching users row exists — e.g. a
    member invited in the dev identity model where user_id IS the email, or a
    seeded owner without a users record — we fall back to user_id, which has
    historically doubled as the email (see module docstring).
    """
    return {
        "user_id": user_id,
        "email": email or user_id,
        "role": role,
        "created_at": created_at,
    }


# ── Route registration ────────────────────────────────────────────────────────

def register_workspace_routes(app) -> None:

    @app.get(
        "/api/workspace/members",
        response_model=List[MemberOut],
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
        tags=["workspace"],
    )
    def list_workspace_members() -> List[MemberOut]:
        """List workspace members for the caller's org (owner only)."""
        _ensure_members_table()
        org_id = get_current_org_id()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT wm.user_id, wm.role, wm.created_at, u.email "
                "FROM workspace_members wm "
                "LEFT JOIN users u ON u.id = wm.user_id "
                "WHERE wm.org_id = %s ORDER BY wm.created_at ASC",
                (org_id,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [MemberOut(**_row_to_member(r[0], r[1], r[2], r[3])) for r in rows]

    @app.post(
        "/api/workspace/members",
        response_model=MemberOut,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
        tags=["workspace"],
    )
    def invite_workspace_member(body: MemberInviteRequest) -> MemberOut:
        """Invite a member to the caller's org (owner only).

        Returns 201 with the created member. Returns 409 if the email is
        already a member. Writes a member_invited audit event.
        """
        if body.role not in _ASSIGNABLE_ROLES:
            raise HTTPException(
                status_code=400,
                detail="role must be 'analyst' or 'viewer'",
            )

        email = body.email.strip()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="a valid email is required")

        _ensure_members_table()
        org_id = get_current_org_id()
        user_id = email

        # Reject duplicates — (org_id, user_id) is the primary key.
        if get_user_role(org_id, user_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"'{user_id}' is already a member of this workspace",
            )

        created_at = _now_iso()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (org_id, user_id, body.role, created_at),
            )
            con.commit()
        finally:
            con.close()

        log_event(
            "member_invited",
            org_id=org_id,
            user_id=user_id,
            role=body.role,
        )

        return MemberOut(**_row_to_member(user_id, body.role, created_at))

    @app.delete(
        "/api/workspace/members/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
        tags=["workspace"],
    )
    def remove_workspace_member(
        user_id: str,
        token: str = Depends(require_auth),
    ) -> Response:
        """Remove a member from the caller's org (owner only).

        Returns 204 on success, 404 if the member does not exist, and 400 if
        the owner attempts to remove themselves. Writes a member_removed
        audit event.
        """
        _ensure_members_table()
        org_id = get_current_org_id()
        current_user_id = _get_user_id_from_token(token)

        # An owner must not lock themselves out of their own workspace.
        if user_id == current_user_id:
            raise HTTPException(
                status_code=400,
                detail="Owner cannot remove themselves",
            )

        if get_user_role(org_id, user_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{user_id}' is not a member of this workspace",
            )

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "DELETE FROM workspace_members WHERE org_id = %s AND user_id = %s",
                (org_id, user_id),
            )
            con.commit()
        finally:
            con.close()

        log_event(
            "member_removed",
            org_id=org_id,
            user_id=user_id,
        )

        return Response(status_code=status.HTTP_204_NO_CONTENT)
