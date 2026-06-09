"""User authentication logic — AUTH-1 / AT-233.

The identity/credential layer behind the /api/auth/* routes (routes_auth.py is
AT-234). Pure functions + raw-SQL data access via app.db.connect(), mirroring
the self-contained style of app/rbac.py and app/auth/vault.py. No FastAPI types
here — callers map the exceptions below to HTTP status codes.

Identity model (Section 1):
    users            — identity only (id, email, password_hash, is_active, ...).
    workspace_members — the SOURCE OF TRUTH for org_id and role.
    orgs             — organization name for an org_id.
The login flow reads org_id and role from workspace_members at JWT-assembly
time; they are never stored on the users row.

Security controls (Section 7):
    * bcrypt cost 12, password capped at 72 bytes before hashing (AC16).
    * Timing-safe login — identical "Invalid email or password" message for a
      wrong password and an unknown email, and a bcrypt verification is always
      performed (against a dummy hash for unknown/inactive users) so the two
      paths cost the same (AC5).
    * 8-hour HS256 JWT carrying sub/org_id/role/email/jti/iat/exp (AC4).
    * Logout blocklist — jti stored in the KV store until the token's own exp,
      so a logged-out token fails verify_jwt (AC9).
    * Rate limiting — 5 failed attempts per email OR per IP in 15 minutes ->
      RateLimitError (AC6/AC7); a successful login clears the email's failed
      attempts (AC8).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import bcrypt
import jwt

from app import db
from database.models.login_attempts import ALL_LOGIN_ATTEMPTS_DDL
from database.models.orgs import ALL_ORGS_DDL
from database.models.users import ALL_USERS_DDL
from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BCRYPT_ROUNDS = 12
PASSWORD_MAX_BYTES = 72  # bcrypt truncates beyond this; cap explicitly.
PASSWORD_MIN_LENGTH = 8

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 15

_INVALID_CREDENTIALS_MSG = "Invalid email or password"
# >= 32 bytes so HS256 does not warn in dev/test; production must set JWT_SECRET.
_DEV_JWT_SECRET_FALLBACK = "dev-secret-change-me-not-for-production-use"
_BLOCKLIST_PREFIX = "auth_blocklist"

# Precomputed bcrypt hash used for timing-safe comparison when the email is
# unknown or the account is inactive — so the unknown-email path performs the
# same bcrypt work as the wrong-password path (AC5). The plaintext is fixed and
# meaningless; nothing authenticates against it.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"timing-safe-placeholder", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
).decode("utf-8")


def _jwt_secret() -> str:
    """Resolve the JWT signing secret.

    Reads JWT_SECRET (the env var AUTH-1 depends on). Falls back to a dev secret
    when unset; warns loudly if that fallback is used in production so a
    misconfigured deployment is never silently insecure.
    """
    secret = os.getenv("JWT_SECRET")
    if secret:
        return secret
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        logger.error(
            "JWT_SECRET is not set in production — refusing to sign with the dev "
            "fallback secret."
        )
        raise RuntimeError("JWT_SECRET must be set in production")
    return _DEV_JWT_SECRET_FALLBACK


# ---------------------------------------------------------------------------
# Exceptions — callers (routes_auth.py, AT-234) map these to HTTP status codes.
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for authentication/registration failures."""


class RegistrationError(AuthError):
    """Registration input was rejected (e.g. weak password) — maps to 400."""


class EmailAlreadyExistsError(RegistrationError):
    """Email is already registered — maps to 409."""


class InvalidCredentialsError(AuthError):
    """Login failed. Message is intentionally identical for all causes (AC5)."""

    def __init__(self, message: str = _INVALID_CREDENTIALS_MSG) -> None:
        super().__init__(message)


class RateLimitError(AuthError):
    """Too many failed attempts — maps to 429 with Retry-After (AC6/AC7)."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many login attempts. Try again later.")


class InvalidTokenError(AuthError):
    """JWT is malformed, expired, or revoked — maps to 401 (AC9)."""


# ---------------------------------------------------------------------------
# Table init — lazy, idempotent. seed_loader.py does not run Alembic, so the
# runtime creates these the same way ensure_signal_snapshots_table() does.
# CREATE TABLE IF NOT EXISTS — a no-op once migration 0004/0005 has run.
# ---------------------------------------------------------------------------

_AUTH_TABLES_INITIALISED = False


def ensure_auth_tables() -> None:
    """Create orgs, users, login_attempts and workspace_members if missing."""
    global _AUTH_TABLES_INITIALISED
    if _AUTH_TABLES_INITIALISED:
        return
    con = db.connect()
    try:
        for ddl in (
            *ALL_ORGS_DDL,
            *ALL_USERS_DDL,
            *ALL_LOGIN_ATTEMPTS_DDL,
        ):
            con.execute(ddl)
        con.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
        con.commit()
        _AUTH_TABLES_INITIALISED = True
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Password hashing (AC16)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return a bcrypt (cost 12) hash, e.g. '$2b$12$...'. Input capped at 72 bytes."""
    return bcrypt.hashpw(
        password[:PASSWORD_MAX_BYTES].encode("utf-8"),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verification. Never raises on a bad hash format."""
    try:
        return bcrypt.checkpw(
            password[:PASSWORD_MAX_BYTES].encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT (AC4 / AC9)
# ---------------------------------------------------------------------------


def issue_jwt(user_id: str, org_id: str, role: str, email: str) -> str:
    """Sign an 8-hour HS256 JWT with sub/org_id/role/email/jti/iat/exp (AC4)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "org_id": org_id,
        "role": role,
        "email": email,  # display only
        "jti": str(uuid4()),  # logout blocklist key
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRY_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_jwt(token: str) -> dict:
    """Decode and validate a JWT. Raises InvalidTokenError if invalid/expired/revoked."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid or expired token") from exc
    jti = payload.get("jti")
    if jti and _is_jti_blocked(jti):
        raise InvalidTokenError("Token has been revoked")
    return payload


def logout_token(token: str) -> None:
    """Add the token's jti to the blocklist until the token's own exp (AC9).

    A malformed token is rejected; an already-expired token is a no-op (it is
    invalid anyway). Idempotent — logging out twice is harmless.
    """
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return  # already expired — nothing to revoke
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid token") from exc
    jti = payload.get("jti")
    if jti:
        _block_jti(jti, int(payload.get("exp", 0)))


def _block_jti(jti: str, exp_epoch: int) -> None:
    # KV has no native TTL; store exp so a stale entry past the token's own
    # expiry is treated as not-blocked (the token is invalid by then regardless).
    db.kv_set(f"{_BLOCKLIST_PREFIX}:{jti}", {"exp": exp_epoch})


def _is_jti_blocked(jti: str) -> bool:
    entry = db.kv_get(f"{_BLOCKLIST_PREFIX}:{jti}")
    if not entry:
        return False
    exp = entry.get("exp")
    if exp is not None and int(exp) <= int(time.time()):
        return False  # entry (and token) have expired
    return True


# ---------------------------------------------------------------------------
# Rate limiting (AC6 / AC7 / AC8)
# ---------------------------------------------------------------------------


def check_login_rate_limit(email: str, ip_address: str) -> None:
    """Raise RateLimitError when 5+ failed attempts exist for the email OR the IP
    within the last 15 minutes."""
    ensure_auth_tables()
    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=RATE_LIMIT_WINDOW_MINUTES
    )
    if _count_failed_attempts(since=window_start, email=email) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise RateLimitError(retry_after_seconds=RATE_LIMIT_WINDOW_MINUTES * 60)
    if _count_failed_attempts(since=window_start, ip=ip_address) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise RateLimitError(retry_after_seconds=RATE_LIMIT_WINDOW_MINUTES * 60)


def record_login_attempt(email: str, ip_address: str, succeeded: bool) -> None:
    """Persist a login attempt. A successful attempt clears the email's failed
    attempt count so a throttled user recovers on success (AC8)."""
    ensure_auth_tables()
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO login_attempts (id, email, ip_address, attempted_at, succeeded) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), email, ip_address, db.now_iso(), 1 if succeeded else 0),
        )
        if succeeded:
            con.execute(
                "DELETE FROM login_attempts WHERE email = ? AND succeeded = 0",
                (email,),
            )
        con.commit()
    finally:
        con.close()


def _count_failed_attempts(
    *, since: datetime, email: str | None = None, ip: str | None = None
) -> int:
    if (email is None) == (ip is None):
        raise ValueError("Provide exactly one of email or ip")
    column = "email" if email is not None else "ip_address"
    value = email if email is not None else ip
    con = db.connect()
    try:
        cur = con.execute(
            f"SELECT COUNT(*) FROM login_attempts "
            f"WHERE {column} = ? AND succeeded = 0 AND attempted_at >= ?",
            (value, since.isoformat()),
        )
        return int(cur.fetchone()[0])
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Registration (AC2)
# ---------------------------------------------------------------------------


def register_org_and_owner(org_name: str, email: str, password: str) -> dict:
    """Create org + user (identity only) + owner workspace_member in one
    transaction and return {token, user{id,email,role,org_id}} (AC2).

    Raises EmailAlreadyExistsError (409) / RegistrationError (400) on bad input.
    """
    ensure_auth_tables()
    email = email.strip().lower()
    if not org_name or not org_name.strip():
        raise RegistrationError("Organization name is required")
    if not email:
        raise RegistrationError("Email is required")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise RegistrationError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
        )
    if _get_user_by_email(email) is not None:
        raise EmailAlreadyExistsError("Email already registered")

    org_id = str(uuid4())
    user_id = str(uuid4())
    password_hash = hash_password(password)
    now = db.now_iso()

    con = db.connect()
    try:
        # Single transaction — org_id and role live in workspace_members, never
        # on the users row. Roll back all three rows if any insert fails.
        con.execute(
            "INSERT INTO orgs (id, name, created_at) VALUES (?, ?, ?)",
            (org_id, org_name.strip(), now),
        )
        con.execute(
            "INSERT INTO users (id, email, password_hash, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, email, password_hash, 1, now),
        )
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (?, ?, 'owner', ?)",
            (org_id, user_id, now),
        )
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        # Lost the race on the global unique email index.
        raise EmailAlreadyExistsError("Email already registered") from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    token = issue_jwt(user_id=user_id, org_id=org_id, role="owner", email=email)
    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": email,
            "role": "owner",
            "org_id": org_id,
            "org_name": org_name.strip(),
        },
    }


# ---------------------------------------------------------------------------
# Login (AC4 / AC5 / AC6 / AC7 / AC8)
# ---------------------------------------------------------------------------


def login(email: str, password: str, ip_address: str) -> dict:
    """Authenticate and return {token, user{id,email,role,org_id}} (AC4).

    org_id and role are read from workspace_members. Failure raises
    InvalidCredentialsError with an identical message for every cause and always
    performs one bcrypt verification, so timing does not leak account existence
    (AC5). RateLimitError is raised before any credential check (AC6/AC7).
    """
    ensure_auth_tables()
    email = email.strip().lower()

    # Throttle check first — before any credential work (AC6/AC7).
    check_login_rate_limit(email, ip_address)

    user = _get_user_by_email(email)
    membership = _get_workspace_member(user["id"]) if user else None

    # Always run exactly one bcrypt verification — real hash for an active,
    # registered member; dummy hash otherwise — so both paths cost the same.
    usable = bool(user) and bool(user["is_active"]) and membership is not None
    hash_to_check = user["password_hash"] if usable else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)

    if not usable or not password_ok:
        record_login_attempt(email, ip_address, succeeded=False)
        raise InvalidCredentialsError()

    record_login_attempt(email, ip_address, succeeded=True)
    _update_last_login(user["id"])

    token = issue_jwt(
        user_id=user["id"],
        org_id=membership["org_id"],
        role=membership["role"],
        email=email,
    )
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": email,
            "role": membership["role"],
            "org_id": membership["org_id"],
            "org_name": get_org_name(membership["org_id"]),
        },
    }


# ---------------------------------------------------------------------------
# Data access — users / workspace_members
# ---------------------------------------------------------------------------


def _get_user_by_email(email: str) -> dict | None:
    con = db.connect()
    try:
        cur = con.execute(
            "SELECT id, email, password_hash, is_active FROM users WHERE email = ?",
            (email.strip().lower(),),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "password_hash": row[2],
        "is_active": bool(row[3]),
    }


def get_org_name(org_id: str | None) -> str | None:
    """Return the human-readable organization name for an org_id, or None.

    org_id is a UUID primary key, not an encrypted value — the display name lives
    in the orgs table. Callers use this to show "<org name>'s Profile" rather than
    the raw UUID.
    """
    if not org_id:
        return None
    con = db.connect()
    try:
        cur = con.execute("SELECT name FROM orgs WHERE id = ?", (org_id,))
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _get_workspace_member(user_id: str) -> dict | None:
    """Return {org_id, role} for the user's workspace membership (POC: one each)."""
    con = db.connect()
    try:
        cur = con.execute(
            "SELECT org_id, role FROM workspace_members WHERE user_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {"org_id": row[0], "role": row[1]}


def _update_last_login(user_id: str) -> None:
    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (db.now_iso(), user_id),
        )
        con.commit()
    finally:
        con.close()
