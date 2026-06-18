"""Auth API routes â€” AUTH-1 / AT-234.

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

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, Field

from app import db
from app.email_service import (
    send_invite_email,
    send_password_reset_email,
    send_welcome_email,
)
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
    "/api/auth/reset-password",
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
    """AC2: creates org + user + workspace_member in one transaction; returns JWT.

    CS-3: a weak password is rejected with 422 before any org/user is created.
    """
    ensure_auth_tables()
    _enforce_password_strength(body.password)
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

    # CS-3 (T7/AC10): send the welcome email after a successful registration.
    # Non-blocking by design (AC14) â€” send_welcome_email never raises, but we also
    # guard here so a registration always returns its normal 201 even if email
    # delivery is misconfigured. Failures are logged, never surfaced to the user.
    user = result.get("user", {}) if isinstance(result, dict) else {}
    try:
        send_welcome_email(user.get("email"), user.get("org_name"))
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
            cur = con.execute(
                "SELECT last_login_at FROM users WHERE id = ?", (user_id,)
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

    con = db.connect()
    try:
        cur = con.execute("SELECT id, is_active FROM users WHERE email = ?", (email,))
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
            con.execute(
                "INSERT INTO users (id, email, password_hash, is_active, created_at) "
                "VALUES (?, ?, '', 0, ?)",
                (user_id, email, now),
            )
            con.execute(
                "INSERT OR IGNORE INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (org_id, user_id, body.role, now),
            )
            con.commit()
        finally:
            con.close()

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
            cur = con.execute("SELECT email FROM users WHERE id = ?", (user_id,))
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
        cur = con.execute("SELECT email FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=400, detail="Invited user not found")
        email = row[0]
        con.execute(
            "UPDATE users SET password_hash = ?, is_active = 1 WHERE id = ?",
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
        cur = con.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
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
        con.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
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
# Forgot / reset password (CS-3 Section 5)
#
# The reset token's SHA-256 hash and a one-hour expiry are stored in the users
# table (reset_token_hash / reset_token_expires_at). The raw token is never
# persisted â€” only its hash â€” so a DB leak does not yield usable reset links.
# ---------------------------------------------------------------------------


def _store_reset_token(user_id: str, raw_token: str) -> None:
    """Persist the SHA-256 hash of a reset token + a one-hour expiry on the user."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=RESET_TTL_HOURS)
    ).isoformat()
    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET reset_token_hash = ?, reset_token_expires_at = ? "
            "WHERE id = ?",
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
        row = con.execute(
            "SELECT id, reset_token_expires_at FROM users WHERE reset_token_hash = ?",
            (token_hash,),
        ).fetchone()
    finally:
        con.close()

    if row is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    expires_at_str = row[1] or ""
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
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
        row = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
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
        con.execute(
            "UPDATE users SET password_hash = ?, reset_token_hash = NULL, "
            "reset_token_expires_at = NULL WHERE id = ?",
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
