"""Auth API routes â€” AUTH-1 / AT-234.

All 7 /api/auth/* endpoints. Auth dependency uses verify_jwt (not the legacy
static-token require_auth) so dynamic JWT tokens issued by register/login/
accept-invite are accepted.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, Field

from app import db
from app.email_service import (
    send_invite_email,
    send_org_approved_email,
    send_org_rejected_email,
    send_password_reset_email,
    send_welcome_email,
)
from app.auth.user_auth import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidTokenError,
    OrgPendingApprovalError,
    OrgRejectedError,
    RateLimitError,
    RegistrationError,
    ensure_auth_tables,
    get_org_name,
    has_pending_join_requests,
    hash_password,
    issue_jwt,
    login,
    logout_token,
    mark_password_changed,
    register_org_and_owner,
    settle_join_requests,
    validate_org_matches_email_domain,
    validate_org_name,
    validate_password_strength,
    verify_jwt,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_INVITE_KV_PREFIX = "auth_invite"
INVITE_TTL_HOURS = 72

# CS-3 forgot/reset-password: the reset token's SHA-256 hash + expiry live in the
# users table (reset_token_hash / reset_token_expires_at), per the doc. The raw
# token is never stored. One-hour expiry.
RESET_TTL_HOURS = 1

AUTH_ROUTE_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/invite",
    "/api/auth/accept-invite",
    "/api/auth/change-password",
    "/api/auth/forgot-password",
    "/api/auth/org-approval/approve",
    "/api/auth/org-approval/reject",
    "/api/auth/reset-password",
}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    org_name: str
    email: str
    password: str
    # Exact name the registrant types at signup. Optional so older clients that
    # do not send it still register; used only for the welcome email greeting.
    full_name: Optional[str] = None


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


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    # CS-3 Section 5 (and the frontend authApi.resetPassword) send `reset_token`.
    # Accept `token` too as a backward-compatible alias, mirroring AcceptInvite.
    reset_token: str = Field(validation_alias=AliasChoices("reset_token", "token"))
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
# Password strength enforcement (CS-3) â€” the API security boundary.
# ---------------------------------------------------------------------------


def _enforce_password_strength(password: str) -> None:
    """Reject a weak password with HTTP 422 (CS-3). No-op when it is valid.

    Shared by the password-CREATION routes (register, accept-invite,
    reset-password). The 422 body lists exactly what is missing, e.g.
    "Password must contain: at least one uppercase letter, at least one special
    character". Login NEVER calls this â€” existing users with pre-CS-3 passwords
    must still authenticate.
    """
    errors = validate_password_strength(password)
    if errors:
        raise HTTPException(
            status_code=422,
            detail="Password must contain: " + ", ".join(errors),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/register", status_code=201)
def register(body: RegisterRequest) -> Dict[str, Any]:
    """AUTH-2: creates org + owner member in pending_approval state.

    CS-3: a weak password is rejected with 422 before any org/user is created.
    AUTH-2: no JWT is issued until a CloudFulcrum admin approves the org.
    """
    ensure_auth_tables()
    _enforce_password_strength(body.password)
    # BUG 1: the org name must correspond to the company email domain. Enforced
    # here at the API boundary (the backend source of truth), mirroring how
    # _enforce_password_strength gates the other registration input policy. A
    # mismatch is rejected before any org/user is created. Run the letters-only
    # name check FIRST so an invalid name still returns its "did you mean …?"
    # suggestion rather than the domain-mismatch message.
    try:
        validate_org_name(body.org_name)
        validate_org_matches_email_domain(body.org_name, body.email)
    except RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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

    # CS-3 (T7/AC10): send a welcome email to the registrant. Non-blocking
    # (AC14) — a delivery failure must never break registration, so any error is
    # swallowed and the pending-approval response is still returned.
    try:
        send_welcome_email(body.email, body.org_name, body.full_name)
    except Exception:  # pragma: no cover - email helper already swallows errors
        logger.exception("welcome email dispatch failed (non-blocking)")

    return result


@router.post("/login")
def login_endpoint(body: LoginRequest, request: Request) -> Dict[str, Any]:
    """AC4/AC5: rate-limit â†’ validate â†’ read membership â†’ return JWT."""
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
    except OrgPendingApprovalError as exc:
        raise HTTPException(
            status_code=403,
            detail={"message": str(exc), "error_code": exc.error_code},
        )
    except OrgRejectedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"message": str(exc), "error_code": exc.error_code},
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
    """Create an invite and email the invitee an accept-invite link (CS-3 T7).

    Always returns 201. The invitation email is sent via send_invite_email and is
    non-blocking (AC14): if delivery fails the route still returns 201 and the
    failure is logged. The response reports whether the email was sent
    (``email_sent``).

    Token visibility (CS-3 Section 4): in non-production the raw ``invite_token``
    is included for testing convenience; in production it is omitted so a real
    token is never returned in an API response â€” invitees receive it only by
    email. The pre-CS-3 production 501 stub is removed: email delivery is now a
    real supported path.
    """
    if body.role not in ("owner", "analyst", "viewer"):
        raise HTTPException(status_code=400, detail="role must be owner, analyst, or viewer")

    ensure_auth_tables()
    org_id = owner_payload.get("org_id")
    email = body.email.strip().lower()
    now = db.now_iso()
    is_active = False

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT id, is_active FROM users WHERE email = %s", (email,))
        existing_user = cur.fetchone()

        if existing_user:
            user_id = existing_user[0]
            is_active = bool(existing_user[1])

            cur.execute(
                "SELECT role FROM workspace_members "
                "WHERE org_id = %s AND user_id = %s AND is_deleted = FALSE",
                (org_id, user_id),
            )
            current_membership = cur.fetchone()
            if current_membership:
                raise HTTPException(
                    status_code=409,
                    detail="Email is already a member of this workspace",
                )

            cur.execute(
                "SELECT org_id FROM workspace_members "
                "WHERE user_id = %s AND is_deleted = FALSE LIMIT 1",
                (user_id,),
            )
            other_membership = cur.fetchone()
            if other_membership:
                raise HTTPException(status_code=409, detail="Email already belongs to another workspace")

            # Reactivating upsert: a previously-removed (soft-deleted) row still
            # occupies the (org_id, user_id) PK; re-activate it instead of failing.
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET "
                "role = EXCLUDED.role, created_at = EXCLUDED.created_at, is_deleted = FALSE",
                (org_id, user_id, body.role, now),
            )
        else:
            user_id = str(uuid4())
            cur.execute(
                "INSERT INTO users (id, email, password_hash, is_active, created_at) "
                "VALUES (%s, %s, '', FALSE, %s)",
                (user_id, email, now),
            )
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET "
                "role = EXCLUDED.role, created_at = EXCLUDED.created_at, is_deleted = FALSE",
                (org_id, user_id, body.role, now),
            )
        con.commit()
    finally:
        con.close()

    if is_active:
        return {}

    raw_token = str(uuid4())
    _store_invite(raw_token, user_id=user_id, org_id=org_id, role=body.role)

    # CS-3 (T7/AC10): email the invitee the accept-invite link. Non-blocking
    # (AC14) â€” send_invite_email never raises, but we also guard here so the
    # invite always returns 201 even if email delivery is misconfigured.
    org_name = get_org_name(org_id)
    try:
        email_sent = send_invite_email(email, raw_token, org_name, body.role)
    except Exception:  # pragma: no cover - email helper already swallows errors
        logger.exception("invite email dispatch failed (non-blocking)")
        email_sent = False

    # Token visibility: non-production returns the raw token for testing; production
    # never returns it (invitees receive it by email only).
    environment = os.getenv("ENVIRONMENT", "").strip().lower()
    response: Dict[str, Any] = {"email_sent": email_sent}
    if environment != "production":
        response["invite_token"] = raw_token
    return response


@router.get("/invite-info")
def invite_info(token: str) -> Dict[str, Any]:
    """Resolve an invite token to its org/email WITHOUT consuming it.

    Lets the accept-invite page greet the invitee by org name and show an
    'invalid / expired / already used' state on page load â€” before any submit, so
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
    """AC11/AC12: validate token â†’ enforce password strength â†’ activate â†’ return JWT.

    CS-3: the invited account is activated only once the chosen password passes
    the strength rule (422 otherwise). Token validation runs first, so an
    invalid/expired/used token still returns 400 regardless of the password.
    """
    ensure_auth_tables()

    entry = _validate_invite(body.invite_token)

    _enforce_password_strength(body.password)

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

    # Revoke every JWT issued before this change (issue #4) â€” including the token
    # used to make this request and any session held on a compromised device.
    # The caller must log in again to obtain a fresh token.
    mark_password_changed(user_id)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Org approval endpoints (AUTH-2 / AT-355)
# ---------------------------------------------------------------------------


def _get_org_approval_row(org_id: str) -> dict | None:
    """Fetch approval-related columns for an org. Returns None if not found."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT name, approval_status, approval_token_hash, "
            "approval_token_expires_at "
            "FROM orgs WHERE id = %s",
            (org_id,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {
        "name": row[0],
        "approval_status": row[1],
        "approval_token_hash": row[2],
        "approval_token_expires_at": row[3],
    }


def _get_org_owner_email(org_id: str) -> str | None:
    """Return the email of the first owner-role member of an org."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT u.email FROM users u "
            "JOIN workspace_members wm ON wm.user_id = u.id "
            "WHERE wm.org_id = %s AND wm.role = 'owner' AND wm.is_deleted = FALSE "
            "ORDER BY wm.created_at ASC LIMIT 1",
            (org_id,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _update_org_approval(
    org_id: str, *, status: str, action: str, now_iso: str
) -> None:
    """Set approval_status, audit action metadata, and consume the token."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE orgs SET approval_status = %s, approved_at = %s, "
            "approved_by_action = %s, approval_token_hash = NULL "
            "WHERE id = %s",
            (status, now_iso, action, org_id),
        )
        con.commit()
    finally:
        con.close()


def _html_page(title: str, heading: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        "<!DOCTYPE html><html>"
        f"<head><meta charset=\"utf-8\"><title>{title}</title></head>"
        "<body style=\"font-family:Arial,sans-serif;max-width:600px;"
        "margin:40px auto;padding:24px;color:#333\">"
        f"<h2>{heading}</h2><p>{body}</p>"
        "</body></html>"
    )
    return HTMLResponse(content=html, status_code=status_code)


def _approval_link_expired(expires_at: object) -> bool:
    if expires_at is None:
        return False
    try:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return False
    return datetime.now(timezone.utc) > expires_at


def _approval_token_state(token: str, org_id: str) -> "tuple[dict | None, str]":
    """Resolve an approval token against an org WITHOUT leaking org state.

    Returns (org_row, state) where state is one of:
      "ok"              — org is pending, the token matches, link unexpired.
      "expired"         — token matches a pending org but the link is past expiry.
      "already_active"  — token matches but the org is ALREADY approved.
      "already_rejected"— token matches but the org was ALREADY rejected.
      "invalid"         — unknown org, no/consumed token, or a WRONG token.

    The token match is checked FIRST, before the org's approval_status. A caller
    WITHOUT the real token can only ever reach "invalid", so a mismatched
    token+org_id still cannot distinguish a pending org from a processed or
    nonexistent one (security review #5 — anti-probing preserved). The
    already_active / already_rejected states are reachable ONLY by a caller who
    holds the valid token (the legitimate approver), so revealing the settled
    state to them leaks nothing.

    Why this matters (the subsequent-registration bug): org-name dedup means a
    later registrant JOINS an existing org and gets a fresh approval email whose
    token is stored on the SAME org. If that org was already approved, the old
    "status must be pending" gate collapsed the (valid) link to "invalid". Ranking
    the token match ahead of the status lets the approve endpoint treat that link
    as an idempotent success instead.
    """
    org = _get_org_approval_row(org_id)
    if (
        org is None
        or not org["approval_token_hash"]
        or _token_hash(token) != org["approval_token_hash"]
    ):
        return org, "invalid"
    # The caller holds the real token from here on.
    status = org["approval_status"]
    if status == "active":
        return org, "already_active"
    if status == "rejected":
        return org, "already_rejected"
    # pending_approval
    if _approval_link_expired(org["approval_token_expires_at"]):
        return org, "expired"
    return org, "ok"


def _invalid_link_page() -> HTMLResponse:
    """Uniform response for every non-actionable case (security review #5)."""
    return _html_page(
        "Link Not Valid",
        "This link is no longer valid",
        "This approval link is invalid or has already been used. "
        "No further action is needed.",
        status_code=400,
    )


def _expired_link_page() -> HTMLResponse:
    return _html_page(
        "Link Expired",
        "This link has expired",
        "This approval link expired after 7 days. "
        "Contact engineering to reissue an approval link.",
        status_code=400,
    )


def _confirmation_page(action: str, org_name: str, token: str, org_id: str) -> HTMLResponse:
    """Render the GET confirmation page whose form POSTs the actual decision.

    The email link is a GET to this page; the state change happens only when the
    admin submits the form (a POST), so an email security scanner that pre-fetches
    the GET link cannot approve or reject an org (security review #1, HIGH). The
    single-use approval token carried in the form is the unguessable action
    credential, so no separate CSRF token is required.
    """
    is_approve = action == "approve"
    verb = "Approve" if is_approve else "Reject"
    btn_color = "#15803d" if is_approve else "#b91c1c"
    post_url = f"/api/auth/org-approval/{action}?" + urlencode(
        {"token": token, "org_id": org_id}
    )
    safe_org = html.escape(org_name or "")
    safe_url = html.escape(post_url, quote=True)
    doc = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{verb} organisation</title></head>"
        "<body style=\"font-family:Arial,sans-serif;max-width:600px;margin:40px auto;"
        "padding:24px;color:#333\">"
        f"<h2>{verb} {safe_org}?</h2>"
        f"<p>You are about to <strong>{verb.lower()}</strong> the organisation "
        f"<strong>{safe_org}</strong>. This action is final and notifies the registrant.</p>"
        f"<form method=\"post\" action=\"{safe_url}\">"
        f"<button type=\"submit\" style=\"background:{btn_color};color:#fff;border:none;"
        "padding:12px 22px;border-radius:6px;font-size:15px;font-weight:700;cursor:pointer\">"
        f"Confirm {verb.lower()}</button>"
        "</form>"
        "<p style=\"color:#666;font-size:13px;margin-top:18px\">"
        "Nothing changes until you click the button above, so you can safely close "
        "this page if you did not mean to open it.</p>"
        "</body></html>"
    )
    return HTMLResponse(content=doc)


def _process_decision(token, org_id, *, status, action, email_sender) -> HTMLResponse:
    """Shared POST handler: validate the token then mutate + notify."""
    org, state = _approval_token_state(token, org_id)
    if state == "expired":
        return _expired_link_page()

    # Approve on an ALREADY-approved org (a subsequent registration joined it): the
    # org is active already, so do NOT re-mutate or re-notify — but DO approve any
    # PENDING join requests tied to it (TENANT-1), which is what this link is for
    # when the org is already active. Those joiners can then log in.
    if status == "active" and state == "already_active":
        settle_join_requests(org_id, "approved")
        safe_org = html.escape(org["name"] or "")
        return _html_page(
            "Organisation Approved",
            "Organisation approved",
            f"<strong>{safe_org}</strong> is already approved. Any pending join "
            "requests have been approved; those users can now log in.",
        )

    # Reject on an ALREADY-approved org: NEVER flip the org (anti-flip). If a
    # subsequent registration is waiting as a PENDING join request, this reject
    # link rejects that join — the org stays active, the joiner stays blocked. With
    # nothing pending it is a stray/settled link → invalid.
    if status == "rejected" and state == "already_active":
        if has_pending_join_requests(org_id):
            settle_join_requests(org_id, "rejected")
            safe_org = html.escape(org["name"] or "")
            return _html_page(
                "Join Request Rejected",
                "Join request rejected",
                f"The pending join request for <strong>{safe_org}</strong> has "
                "been rejected. The organisation itself remains active.",
            )
        return _invalid_link_page()

    if state != "ok":
        return _invalid_link_page()

    _update_org_approval(org_id, status=status, action=action, now_iso=db.now_iso())

    registrant_email = _get_org_owner_email(org_id)
    if registrant_email:
        email_sender(registrant_email=registrant_email, org_name=org["name"])

    # Settle any join requests that arrived while the org was still pending so they
    # follow the org's decision (approve → approved, reject → rejected).
    settle_join_requests(org_id, "approved" if status == "active" else "rejected")

    safe_org = html.escape(org["name"] or "")
    if status == "active":
        return _html_page(
            "Organisation Approved",
            "Organisation approved",
            f"<strong>{safe_org}</strong> has been approved. "
            "The registrant has been notified and can now log in.",
        )
    return _html_page(
        "Organisation Rejected",
        "Organisation registration rejected",
        f"<strong>{safe_org}</strong> has been rejected. "
        "The registrant has been notified.",
    )


@router.get("/org-approval/approve", response_class=HTMLResponse)
def approve_org_confirm(token: str, org_id: str) -> HTMLResponse:
    """GET renders a confirmation page only — it never mutates state.

    Enterprise email security gateways (Proofpoint, Mimecast, Microsoft Defender
    for Office 365) pre-fetch every link in inbound mail, so a state-mutating GET
    would let the scanner approve/reject an org before the admin reads the email.
    The decision is committed only by the POST below, triggered by the human
    clicking the confirmation button (security review #1, HIGH).
    """
    org, state = _approval_token_state(token, org_id)
    if state == "expired":
        return _expired_link_page()
    # "ok" (pending) and "already_active" (a subsequent registration under an
    # already-approved org) both render the confirmation page; the POST then
    # approves (pending) or reports the idempotent already-approved success.
    if state not in ("ok", "already_active"):
        return _invalid_link_page()
    return _confirmation_page("approve", org["name"], token, org_id)


@router.post("/org-approval/approve", response_class=HTMLResponse)
def approve_org(token: str, org_id: str) -> HTMLResponse:
    """Commit the approval. State-mutating → POST only (link scanners use GET)."""
    return _process_decision(
        token, org_id, status="active", action="approved",
        email_sender=send_org_approved_email,
    )


@router.get("/org-approval/reject", response_class=HTMLResponse)
def reject_org_confirm(token: str, org_id: str) -> HTMLResponse:
    """GET renders a confirmation page only — it never mutates state (see approve)."""
    org, state = _approval_token_state(token, org_id)
    if state == "expired":
        return _expired_link_page()
    # Render the confirmation form when the reject link is actionable:
    #   * "ok"            — the org is still pending → rejecting the ORG.
    #   * "already_active" WITH a pending join request → rejecting that JOIN (the
    #     org stays active; TENANT-1). Without a pending join, a settled/active
    #     org's reject link must never flip it → invalid.
    if state == "ok" or (
        state == "already_active" and has_pending_join_requests(org_id)
    ):
        return _confirmation_page("reject", org["name"], token, org_id)
    return _invalid_link_page()


@router.post("/org-approval/reject", response_class=HTMLResponse)
def reject_org(token: str, org_id: str) -> HTMLResponse:
    """Commit the rejection. State-mutating → POST only (link scanners use GET)."""
    return _process_decision(
        token, org_id, status="rejected", action="rejected",
        email_sender=send_org_rejected_email,
    )


# ---------------------------------------------------------------------------
# Forgot / reset password (CS-3 Section 5)
#
# The reset token's SHA-256 hash and a one-hour expiry are stored in the users
# table (reset_token_hash / reset_token_expires_at). The raw token is never
# persisted â€” only its hash â€” so a DB leak does not yield usable reset links.
# ---------------------------------------------------------------------------


def _store_reset_token(user_id: str, raw_token: str) -> None:
    """Persist the SHA-256 hash of a reset token + a one-hour expiry on the user."""
    # Store naive UTC: the column is `timestamp without time zone`, so a tz-aware
    # value could be shifted to the session timezone on write. A naive UTC value
    # is stored verbatim and re-stamped as UTC on read in _consume_reset_token.
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=RESET_TTL_HOURS)
    ).replace(tzinfo=None)
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE users SET reset_token_hash = %s, reset_token_expires_at = %s "
            "WHERE id = %s",
            (_token_hash(raw_token), expires_at, user_id),
        )
        con.commit()
    finally:
        con.close()


def _consume_reset_token(raw_token: str) -> dict:
    """Resolve a raw reset token to its user, enforcing match + expiry.

    Returns {"user_id": ...} on success. Raises HTTPException(400) if the token
    is unknown, already consumed, malformed, or expired â€” a single 400 surface so
    the client cannot distinguish the failure modes. Does NOT mutate; the caller
    clears the token only after the password is written (so a failed write leaves
    the token usable for a retry).
    """
    token_hash = _token_hash(raw_token)
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, reset_token_expires_at FROM users WHERE reset_token_hash = %s",
            (token_hash,),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None or row[1] is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    # psycopg2 returns a datetime for the TIMESTAMP column; tolerate an ISO string
    # too (e.g. a legacy SQLite-written value) for safety.
    expires_at = row[1]
    try:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    return {"user_id": row[0]}


def _dispatch_reset(user_id: str, email: str, raw_token: str) -> None:
    """Persist the reset token and send the email â€” runs off the request path.

    Scheduled as a BackgroundTask so the registered and unregistered branches of
    forgot_password do the same (minimal) request-path work, closing the
    write/email timing side-channel that would otherwise let an attacker
    distinguish a registered email by response latency. Mirrors how the login
    path equalizes timing via a dummy bcrypt compare. Never raises.
    """
    try:
        _store_reset_token(user_id, raw_token)
        send_password_reset_email(email, raw_token)
    except Exception:  # pragma: no cover - defensive; must never surface
        logger.warning("password reset dispatch failed (non-blocking)")



@router.post("/forgot-password", status_code=200)
def forgot_password(
    body: ForgotPasswordRequest, background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """CS-3 §5 / AC11: request a password reset.

    Always returns 200 with an identical body whether or not the email is
    registered, so the endpoint cannot be used to enumerate accounts. When the
    email IS registered, a UUID reset token is generated and its SHA-256 hash + a
    one-hour expiry are stored on the user, then send_password_reset_email is
    called — both off the request path via a BackgroundTask, so the registered
    and unregistered branches return with equal request-path work (no timing
    oracle). Email/transport failures are swallowed (logged) — they must not leak
    account existence or change the response.
    """
    ensure_auth_tables()
    email = body.email.strip().lower()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
    finally:
        con.close()

    response: Dict[str, Any] = {"status": "ok"}

    if row is not None:
        # UUID + SHA-256 are microsecond-cheap; the DB write + email (the real
        # latency) run in the background so they cannot be timed against the
        # unregistered path.
        raw_token = str(uuid4())
        background_tasks.add_task(_dispatch_reset, row[0], email, raw_token)
        # Non-production convenience (mirrors the invite flow): expose the raw
        # token so automated tests / local manual testing can complete the reset
        # without a live mailer. Never returned in production.
        if os.getenv("ENVIRONMENT", "").strip().lower() != "production":
            response["reset_token"] = raw_token

    return response


@router.post("/reset-password", status_code=200)
def reset_password(body: ResetPasswordRequest) -> Dict[str, Any]:
    """CS-3 §5 / AC12: complete a password reset.

    Hashes the supplied token, finds the matching user, checks expiry (400 on
    invalid/expired), validates the new password's strength (422 on weak), writes
    the new password hash, and clears the reset-token fields so the token is
    single-use. Returns 200 on success.
    """
    ensure_auth_tables()

    # 1) Token validity first (400) — before any password work, mirroring invites.
    entry = _consume_reset_token(body.reset_token)
    user_id = entry["user_id"]

    # 2) Strength (422). validate_password_strength returns the unmet rules; an
    # empty list means valid. Matches the same rule registration/invite will use.
    unmet = validate_password_strength(body.new_password)
    if unmet:
        raise HTTPException(
            status_code=422,
            detail="Password must contain: " + ", ".join(unmet),
        )

    # 3) Write the new hash and clear the reset token (single-use) atomically.
    password_hash = hash_password(body.new_password)
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE users SET password_hash = %s, reset_token_hash = NULL, "
            "reset_token_expires_at = NULL WHERE id = %s",
            (password_hash, user_id),
        )
        con.commit()
    finally:
        con.close()

    # Revoke every JWT issued before this reset (parity with change-password).
    mark_password_changed(user_id)

    return {"status": "ok"}


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
