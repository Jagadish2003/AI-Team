"""Auth API routes — AUTH-1 / AT-234.

All 7 /api/auth/* endpoints. Auth dependency uses verify_jwt (not the legacy
static-token require_auth) so dynamic JWT tokens issued by register/login/
accept-invite are accepted.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, Field

from app import db
from app.auth.user_auth import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidTokenError,
    RateLimitError,
    RegistrationError,
    ensure_auth_tables,
    get_org_name,
    hash_password,
    issue_jwt,
    login,
    logout_token,
    mark_password_changed,
    register_org_and_owner,
    verify_jwt,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_INVITE_KV_PREFIX = "auth_invite"
INVITE_TTL_HOURS = 72

AUTH_ROUTE_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/invite",
    "/api/auth/accept-invite",
    "/api/auth/change-password",
}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    org_name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class InviteRequest(BaseModel):
    email: str
    role: str = "analyst"


class AcceptInviteRequest(BaseModel):
    # AUTH-1 Section 4 and the frontend (authApi.acceptInvite) both send
    # `invite_token`. Accept `token` too as a backward-compatible alias so older
    # callers / manual API tests using either name keep working.
    invite_token: str = Field(validation_alias=AliasChoices("invite_token", "token"))
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------------------------------------------------------------------------
# JWT-based auth dependency (replaces legacy static-token require_auth)
# ---------------------------------------------------------------------------


def _require_jwt(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = verify_jwt(creds.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return payload


def _require_owner(payload: dict = Depends(_require_jwt)) -> dict:
    if payload.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return payload


def _get_raw_token(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return creds.credentials


# ---------------------------------------------------------------------------
# Invite token helpers (stored in KV store; keyed by SHA-256 of raw token)
# ---------------------------------------------------------------------------


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _store_invite(raw_token: str, user_id: str, org_id: str, role: str) -> None:
    key = f"{_INVITE_KV_PREFIX}:{_token_hash(raw_token)}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS)
    ).isoformat()
    db.kv_set(key, {
        "user_id": user_id,
        "org_id": org_id,
        "role": role,
        "expires_at": expires_at,
        "used": False,
    })


def _load_invite(raw_token: str) -> dict | None:
    key = f"{_INVITE_KV_PREFIX}:{_token_hash(raw_token)}"
    return db.kv_get(key)


def _mark_invite_used(raw_token: str) -> None:
    key = f"{_INVITE_KV_PREFIX}:{_token_hash(raw_token)}"
    entry = db.kv_get(key)
    if entry:
        entry["used"] = True
        db.kv_set(key, entry)


def _validate_invite(raw_token: str) -> dict:
    """Load an invite and reject it if unknown, used, malformed, or expired.

    Returns the stored entry on success. Shared by GET /invite-info (which only
    reads) and POST /accept-invite (which then consumes it), so both apply the
    exact same acceptance rules and 400 messages. Never mutates the entry.
    """
    entry = _load_invite(raw_token)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid or unknown invite token")
    if entry.get("used"):
        raise HTTPException(status_code=400, detail="Invite token has already been used")
    expires_at_str = entry.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed invite token")
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Invite token has expired")
    return entry


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/register", status_code=201)
def register(body: RegisterRequest) -> Dict[str, Any]:
    """AC2: creates org + user + workspace_member in one transaction; returns JWT."""
    ensure_auth_tables()
    try:
        result = register_org_and_owner(
            org_name=body.org_name,
            email=body.email,
            password=body.password,
        )
    except EmailAlreadyExistsError:
        raise HTTPException(status_code=409, detail="Email already registered")
    except RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.post("/login")
def login_endpoint(body: LoginRequest, request: Request) -> Dict[str, Any]:
    """AC4/AC5: rate-limit → validate → read membership → return JWT."""
    ensure_auth_tables()
    ip = request.client.host if request.client else "unknown"
    try:
        result = login(email=body.email, password=body.password, ip_address=ip)
    except RateLimitError as exc:
        # retry_after is also carried in the body: the standard Retry-After
        # header is not a CORS-safelisted response header, so the cross-origin
        # SPA cannot read it without an Access-Control-Expose-Headers entry. The
        # body field lets the login form render a live "wait N minutes" message.
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many failed attempts. Try again later.",
                "retry_after": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return result


@router.post("/logout", status_code=204)
def logout_endpoint(
    raw_token: str = Depends(_get_raw_token),
    _payload: dict = Depends(_require_jwt),
) -> Response:
    """AC9: add jti to blocklist with TTL = remaining token lifetime."""
    try:
        logout_token(raw_token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return Response(status_code=204)


@router.get("/me")
def me(payload: dict = Depends(_require_jwt)) -> Dict[str, Any]:
    """Return {id, email, role, org_id, org_name, last_login_at}."""
    user_id = payload.get("sub")
    org_id = payload.get("org_id")
    last_login_at: str | None = None
    if user_id:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT last_login_at FROM users WHERE id = %s", (user_id,)
            )
            row = cur.fetchone()
            if row:
                last_login_at = row[0]
        finally:
            con.close()
    return {
        "id": user_id,
        "email": payload.get("email"),
        "role": payload.get("role"),
        "org_id": org_id,
        "org_name": get_org_name(org_id),
        "last_login_at": last_login_at,
    }


@router.post("/invite", status_code=201)
def invite(
    body: InviteRequest,
    owner_payload: dict = Depends(_require_owner),
) -> Dict[str, Any]:
    """AC10: non-production returns invite_token; production returns 501."""
    environment = os.getenv("ENVIRONMENT", "").strip().lower()
    if environment == "production":
        raise HTTPException(
            status_code=501,
            detail="Email delivery not configured. Invite tokens cannot be issued in production.",
        )

    if body.role not in ("owner", "analyst", "viewer"):
        raise HTTPException(status_code=400, detail="role must be owner, analyst, or viewer")

    ensure_auth_tables()
    org_id = owner_payload.get("org_id")
    email = body.email.strip().lower()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT id, is_active FROM users WHERE email = %s", (email,))
        existing_user = cur.fetchone()
    finally:
        con.close()

    if existing_user and bool(existing_user[1]):
        raise HTTPException(status_code=409, detail="Email already has an active account")

    if existing_user:
        user_id = existing_user[0]
    else:
        user_id = str(uuid4())
        now = db.now_iso()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO users (id, email, password_hash, is_active, created_at) "
                "VALUES (%s, %s, '', FALSE, %s)",
                (user_id, email, now),
            )
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, body.role, now),
            )
            con.commit()
        finally:
            con.close()

    raw_token = str(uuid4())
    _store_invite(raw_token, user_id=user_id, org_id=org_id, role=body.role)

    return {"invite_token": raw_token}


@router.get("/invite-info")
def invite_info(token: str) -> Dict[str, Any]:
    """Resolve an invite token to its org/email WITHOUT consuming it.

    Lets the accept-invite page greet the invitee by org name and show an
    'invalid / expired / already used' state on page load — before any submit, so
    reopening a spent link no longer renders the empty password form. Applies the
    same rejection rules (400) as accept-invite; the token is never marked used.
    """
    ensure_auth_tables()
    entry = _validate_invite(token)

    email: str | None = None
    user_id = entry.get("user_id")
    if user_id:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                email = row[0]
        finally:
            con.close()

    return {
        "org_name": get_org_name(entry.get("org_id")),
        "email": email,
        "role": entry.get("role"),
    }


@router.post("/accept-invite", status_code=200)
def accept_invite(body: AcceptInviteRequest) -> Dict[str, Any]:
    """AC11/AC12: validate token → set password + is_active → return JWT."""
    from app.auth.user_auth import PASSWORD_MIN_LENGTH

    ensure_auth_tables()

    entry = _validate_invite(body.invite_token)

    if len(body.password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {PASSWORD_MIN_LENGTH} characters",
        )

    user_id = entry["user_id"]
    org_id = entry["org_id"]
    role = entry["role"]

    password_hash = hash_password(body.password)
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=400, detail="Invited user not found")
        email = row[0]
        cur.execute(
            "UPDATE users SET password_hash = %s, is_active = TRUE WHERE id = %s",
            (password_hash, user_id),
        )
        con.commit()
    finally:
        con.close()

    _mark_invite_used(body.invite_token)

    token = issue_jwt(user_id=user_id, org_id=org_id, role=role, email=email)
    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": email,
            "role": role,
            "org_id": org_id,
            "org_name": get_org_name(org_id),
        },
    }


@router.post("/change-password", status_code=204)
def change_password(
    body: ChangePasswordRequest,
    payload: dict = Depends(_require_jwt),
) -> Response:
    """Validate current password before accepting new. Return 204."""
    from app.auth.user_auth import PASSWORD_MIN_LENGTH

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT password_hash FROM users WHERE id = %s", (user_id,)
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(body.current_password, row[0]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if len(body.new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"New password must be at least {PASSWORD_MIN_LENGTH} characters",
        )

    new_hash = hash_password(body.new_password)
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (new_hash, user_id),
        )
        con.commit()
    finally:
        con.close()

    # Revoke every JWT issued before this change (issue #4) — including the token
    # used to make this request and any session held on a compromised device.
    # The caller must log in again to obtain a fresh token.
    mark_password_changed(user_id)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Registration helper for main.py
# ---------------------------------------------------------------------------


def register_auth_routes(app: FastAPI) -> None:
    """Register auth routes once. Idempotent."""
    if getattr(app.state, "auth_routes_registered", False):
        return

    existing_paths = {getattr(r, "path", None) for r in app.routes}
    if AUTH_ROUTE_PATHS.issubset(existing_paths):
        app.state.auth_routes_registered = True
        return

    app.include_router(router)
    app.state.auth_routes_registered = True
