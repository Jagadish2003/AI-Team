"""User authentication logic — AUTH-1 / AT-233, AUTH-2 / AT-352/353, AT-354.

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
    * Rate limiting — 5 failed attempts for an EMAIL in 15 minutes ->
      RateLimitError (AC6); a successful login clears the email's failed
      attempts (AC8). Scoping is per-email, NOT per-IP: a throttled account must
      never lock out other users who share the same IP (a whole team logging in
      from one office IP, or several POC testers on localhost). retry_after is
      the ACTUAL remaining time until the block lifts, so the UI can count it
      down rather than always quoting the full 15-minute window.

      Design note (deviation from AUTH-1 AC7): the original spec also throttled
      per-IP. That was dropped intentionally because it locked out legitimate
      co-located users during POC testing. login_attempts still records the IP
      for audit; it is simply not used as a blocking key.

AUTH-2 approval workflow (AT-353):
    Every new org starts in approval_status='pending_approval'. A signed token
    is emailed to AGENTIQ_ADMIN_EMAIL; the admin clicks Approve or Reject.
    register_org_and_owner() no longer returns a JWT — it returns a
    pending-approval confirmation. OrgPendingApprovalError and OrgRejectedError
    are defined here for use by T3 (login-side enforcement, AT-354).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import secrets
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import bcrypt
import jwt
import psycopg2

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

# CS-3 — the locked password strength rule. Each tuple is (regex, the message
# returned when the password contains no match). Mirrored on the frontend by
# PasswordStrengthIndicator.getPasswordRequirements(); keep the two in sync.
# Digits are deliberately NOT treated as special characters — the special class
# is exactly the punctuation set below, per the locked CS-3 spec.
PASSWORD_RULES = [
    (r"[A-Z]", "at least one uppercase letter"),
    (r"[a-z]", "at least one lowercase letter"),
    (r"[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]", "at least one special character"),
]

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 15

# AUTH-2: approval workflow
AGENTIQ_ADMIN_EMAIL: str = os.getenv("AGENTIQ_ADMIN_EMAIL", "agentiqadmin@dwpglobal.com")
APPROVAL_TOKEN_EXPIRY_DAYS = 7

_INVALID_CREDENTIALS_MSG = "Invalid email or password"
# >= 32 bytes so HS256 does not warn in dev/test; production must set JWT_SECRET.
_DEV_JWT_SECRET_FALLBACK = "dev-secret-change-me-not-for-production-use"
# Known-weak secrets that must never sign tokens in a shared deployment. A match
# is warned about in EVERY environment, not just production, because staging /
# shared-dev deployments frequently leave ENVIRONMENT unset (see issue #9).
_KNOWN_WEAK_SECRETS = frozenset(
    {
        _DEV_JWT_SECRET_FALLBACK,
        "dev-secret-change-me",
        "changeme",
        "change-me",
        "secret",
    }
)
_BLOCKLIST_PREFIX = "auth_blocklist"
# Per-user "password last changed" marker (issue #4). A token whose iat predates
# this timestamp is treated as revoked by verify_jwt, so changing a password
# immediately invalidates every JWT issued before the change.
_PWD_CHANGED_PREFIX = "auth_pwd_changed"
_WEAK_SECRET_WARNED = False

# Precomputed bcrypt hash used for timing-safe comparison when the email is
# unknown or the account is inactive — so the unknown-email path performs the
# same bcrypt work as the wrong-password path (AC5). The plaintext is fixed and
# meaningless; nothing authenticates against it.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"timing-safe-placeholder", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
).decode("utf-8")


def _warn_weak_secret_once() -> None:
    """Emit a single weak-secret warning per process (avoids per-request spam)."""
    global _WEAK_SECRET_WARNED
    if not _WEAK_SECRET_WARNED:
        logger.warning(
            "JWT_SECRET is a known weak/default value — it MUST be changed before "
            "any shared (staging/production) deployment. Tokens signed with it are "
            "forgeable."
        )
        _WEAK_SECRET_WARNED = True


def _jwt_secret() -> str:
    """Resolve the JWT signing secret.

    Reads JWT_SECRET (the env var AUTH-1 depends on). Falls back to a dev secret
    when unset; refuses the fallback in production. A weak/default secret is
    warned about in EVERY environment — not only when ENVIRONMENT=production —
    because staging and shared-dev deployments routinely leave ENVIRONMENT unset
    and would otherwise sign with a guessable secret silently (issue #9).
    """
    secret = os.getenv("JWT_SECRET")
    if secret:
        if secret in _KNOWN_WEAK_SECRETS:
            _warn_weak_secret_once()
        return secret
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        logger.error(
            "JWT_SECRET is not set in production — refusing to sign with the dev "
            "fallback secret."
        )
        raise RuntimeError("JWT_SECRET must be set in production")
    _warn_weak_secret_once()
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


class OrgPendingApprovalError(AuthError):
    """Login attempted for an org still awaiting admin approval — maps to 403.

    Distinct from InvalidCredentialsError (401) and OrgRejectedError (403) so
    the frontend can render the correct message for each state.
    """

    error_code = "org_pending_approval"


class OrgRejectedError(AuthError):
    """Login attempted for an org whose registration was rejected — maps to 403.

    Distinct from OrgPendingApprovalError so the frontend renders the
    'contact your representative' copy, not the 'wait for approval' copy.
    """

    error_code = "org_rejected"


# ---------------------------------------------------------------------------
# Table init — lazy, idempotent. seed_loader.py does not run Alembic, so the
# runtime creates these the same way ensure_signal_snapshots_table() does.
# CREATE TABLE IF NOT EXISTS — a no-op once migration 0004/0005 has run.
# ---------------------------------------------------------------------------

_AUTH_TABLES_INITIALISED = False


def ensure_auth_tables() -> None:
    """No-op. orgs/users/login_attempts/workspace_members are provisioned externally.

    Created by database/provision/provision.sh; the application no longer
    creates these tables at runtime.
    """
    return None


# ---------------------------------------------------------------------------
# Password strength validation (CS-3)
# ---------------------------------------------------------------------------


def validate_password_strength(password: str) -> list[str]:
    """Return the CS-3 strength requirements the password fails to meet.

    The rule (locked, CS-3 Section 1): at least PASSWORD_MIN_LENGTH (8)
    characters, with at least one uppercase letter, one lowercase letter, and one
    special character from ``!@#$%^&*()_+-=[]{}|;:,.<>?``.

    An empty list means the password is valid. This function NEVER raises and
    NEVER hashes — it only inspects the plaintext and returns the list of unmet
    requirements, so each API route can decide how to turn that into an HTTP
    response (e.g. 422 with ``", ".join(errors)``).

    Strength validation is intended to run on the FULL input before hashing. The
    bcrypt 72-byte truncation in _password_bytes() is unchanged and independent of
    this check, so a password longer than 72 bytes that passes here is still
    accepted and hashed on its first 72 bytes.

    Login does NOT call this: existing users may hold passwords created before the
    rule and must still be able to sign in (login verifies against the stored hash
    only). Enforcement lives in the password-CREATION routes, not in login.
    """
    errors: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    for pattern, message in PASSWORD_RULES:
        if not re.search(pattern, password):
            errors.append(message)
    return errors


# ---------------------------------------------------------------------------
# Password hashing (AC16)
# ---------------------------------------------------------------------------


def _password_bytes(password: str) -> bytes:
    """Encode then truncate to PASSWORD_MAX_BYTES bytes — never the reverse.

    bcrypt silently truncates at 72 BYTES internally. Slicing the string to 72
    characters first and encoding afterwards is wrong: a multi-byte character
    (accented letters, emoji, CJK) can push a 72-character string well past 72
    bytes, so bcrypt would still truncate and two passwords differing only past
    the 72nd byte would hash identically. Encoding first and truncating the byte
    string makes the effective key exactly the bytes bcrypt will consume, so the
    cap is explicit and consistent between hashing and verification.
    """
    return password.encode("utf-8")[:PASSWORD_MAX_BYTES]


def hash_password(password: str) -> str:
    """Return a bcrypt (cost 12) hash, e.g. '$2b$12$...'. Input capped at 72 bytes."""
    return bcrypt.hashpw(
        _password_bytes(password),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verification. Never raises on a bad hash format."""
    try:
        return bcrypt.checkpw(
            _password_bytes(password),
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
        # Millisecond issued-at, used only by the password-change revocation
        # check (issue #4). Second-granularity iat would falsely revoke a fresh
        # login that lands in the same second as a password change; milliseconds
        # avoid that boundary collision.
        "iat_ms": int(now.timestamp() * 1000),
        "exp": int((now + timedelta(hours=JWT_EXPIRY_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_signed(token: str) -> dict | None:
    """Return the payload IFF the signature and expiry are valid, else None.

    Signature/expiry verification ONLY — it does not consult the logout blocklist
    or the password-change marker. Used by trust-establishing callers that must
    decide whether a token's claims (org_id, jti) are genuine before acting on
    them — notably the tenancy middleware, which must never set org context from
    an unverified/forged claim (issue #3). Returns None for static dev tokens and
    any non-JWT / tampered / expired token.
    """
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def verify_jwt(token: str) -> dict:
    """Decode and validate a JWT. Raises InvalidTokenError if invalid/expired/revoked."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid or expired token") from exc
    jti = payload.get("jti")
    if jti and _is_jti_blocked(jti):
        raise InvalidTokenError("Token has been revoked")
    # Password-change revocation (issue #4): a token issued before the user last
    # changed their password is no longer valid, so rotating a credential ends
    # every active session for that user (including a stolen one) immediately.
    sub = payload.get("sub")
    if sub and _password_changed_after(sub, payload):
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


def mark_password_changed(user_id: str) -> None:
    """Record that the user's password just changed, revoking older tokens (issue #4).

    Every JWT issued before this instant fails verify_jwt afterwards (its iat is
    older than the marker), so a password change ends all active sessions for the
    user — including any session held by an attacker who stole a token. The marker
    is kept until the longest-lived token that could exist (8h) has expired; a
    little extra lifetime is harmless since an expired token is rejected anyway.
    """
    now = time.time()
    db.kv_set(
        f"{_PWD_CHANGED_PREFIX}:{user_id}",
        {"at_ms": int(now * 1000), "exp": int(now) + JWT_EXPIRY_HOURS * 3600},
    )


def _password_changed_after(user_id: str, payload: dict) -> bool:
    """True if the user changed their password after this token was issued.

    Compares in milliseconds (token ``iat_ms`` vs the marker's ``at_ms``) with a
    strict ``<``: a token issued strictly before the change is revoked, while a
    fresh login issued at/after the change survives — second-granularity ``iat``
    would falsely revoke a new token minted in the same second as the change.
    Tokens predating the iat_ms claim fall back to second-granularity iat.
    """
    entry = db.kv_get(f"{_PWD_CHANGED_PREFIX}:{user_id}")
    if not entry:
        return False
    changed_at_ms = entry.get("at_ms")
    if changed_at_ms is None:
        return False
    token_ms = payload.get("iat_ms")
    if token_ms is None:
        iat = payload.get("iat")
        if iat is None:
            return False  # no issue time to compare — cannot prove it is stale
        token_ms = int(iat) * 1000
    return int(token_ms) < int(changed_at_ms)


# ---------------------------------------------------------------------------
# Rate limiting (AC6 / AC7 / AC8)
# ---------------------------------------------------------------------------
# NOTE (AUTH-1 AC7 deviation): Per-IP rate limiting was intentionally removed.
# Reason: per-IP blocking is unreliable behind reverse proxies and NAT without
# explicit X-Forwarded-For trust configuration, which is not yet in place.
# Current implementation: per-email rate limiting only (5 failed attempts → lockout).
# Tech debt: file a ticket to add per-IP limiting once proxy trust is configured.
# ---------------------------------------------------------------------------


def check_login_rate_limit(email: str, ip_address: str) -> None:
    """Raise RateLimitError when 5+ failed attempts exist for this EMAIL within
    the last 15 minutes (AC6).

    Scoped to the email only — a throttled account does not block other users who
    share the same IP. ``ip_address`` is accepted for signature compatibility and
    is still recorded for audit by record_login_attempt(), but it is not a
    blocking key (see the module docstring for the AC7 deviation rationale).

    The RateLimitError carries the ACTUAL number of seconds until the block lifts,
    not a fixed 15 minutes, so the UI can show a live countdown.
    """
    ensure_auth_tables()
    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=RATE_LIMIT_WINDOW_MINUTES
    )
    if _count_failed_attempts(since=window_start, email=email) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise RateLimitError(
            retry_after_seconds=_seconds_until_email_unblocked(email)
        )


def record_login_attempt(email: str, ip_address: str, succeeded: bool) -> None:
    """Persist a login attempt. A successful attempt clears the email's failed
    attempt count so a throttled user recovers on success (AC8)."""
    ensure_auth_tables()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO login_attempts (id, email, ip_address, attempted_at, succeeded) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid4()), email, ip_address, db.now_iso(), bool(succeeded)),
        )
        if succeeded:
            cur.execute(
                "DELETE FROM login_attempts WHERE email = %s AND succeeded = FALSE",
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
        cur = con.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM login_attempts "
            f"WHERE {column} = %s AND succeeded = FALSE AND attempted_at >= %s",
            (value, since.isoformat()),
        )
        return int(cur.fetchone()[0])
    finally:
        con.close()


def _seconds_until_email_unblocked(email: str) -> int:
    """Seconds until the email's failed-attempt count drops below the threshold.

    The block lifts when the Nth-most-recent failure (N = RATE_LIMIT_MAX_ATTEMPTS)
    ages out of the trailing window: once it leaves, only N-1 failures remain and
    the count is back under the limit. So the unblock time is that attempt's
    timestamp + the window length. Returns a value that shrinks as time passes,
    clamped to at least 1 second while the block is active, so the UI can render a
    live countdown instead of a fixed 15 minutes.

    Falls back to the full window if no qualifying attempt is found (the email is
    not actually blocked) — a defensive default that never under-reports.
    """
    window = timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    window_start = datetime.now(timezone.utc) - window
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT attempted_at FROM login_attempts "
            "WHERE email = %s AND succeeded = FALSE AND attempted_at >= %s "
            "ORDER BY attempted_at DESC LIMIT 1 OFFSET %s",
            (email, window_start.isoformat(), RATE_LIMIT_MAX_ATTEMPTS - 1),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None or not row[0]:
        return RATE_LIMIT_WINDOW_MINUTES * 60
    raw_attempted_at = row[0]
    # attempted_at is a TIMESTAMP column: psycopg2 returns a datetime. Only parse
    # when the value is a string (defensive for any text-typed source). Checking
    # `isinstance(..., str)` rather than `isinstance(..., datetime)` keeps this
    # correct even when a test patches the module-level `datetime` to a subclass.
    if isinstance(raw_attempted_at, str):
        try:
            oldest_relevant = datetime.fromisoformat(raw_attempted_at)
        except (ValueError, TypeError):
            return RATE_LIMIT_WINDOW_MINUTES * 60
    else:
        oldest_relevant = raw_attempted_at
    if oldest_relevant.tzinfo is None:
        oldest_relevant = oldest_relevant.replace(tzinfo=timezone.utc)
    remaining = (oldest_relevant + window) - datetime.now(timezone.utc)
    return max(1, math.ceil(remaining.total_seconds()))


# ---------------------------------------------------------------------------
# Registration (AC2)
# ---------------------------------------------------------------------------


def register_org_and_owner(org_name: str, email: str, password: str) -> dict:
    """Register a brand-new workspace and submit it for admin approval (AUTH-2).

    Registration ALWAYS creates a fresh org (a new org_id) owned by the
    registrant. It never joins an existing workspace, even when the org_name
    collides with one that already exists — org names are not unique and not
    secret, so they are display labels only, never a membership key. org_id and
    role always live in workspace_members, never on the users row.

    SECURITY (issue #5): the previous behaviour joined an existing org as analyst
    when the name matched, which let any external user who guessed a customer's
    org name (e.g. "TCU", "City National") self-register into that workspace and
    read all of its runs/opportunities/evidence. Joining an existing workspace is
    now EXCLUSIVELY via the invite flow (POST /api/auth/invite), which is the
    controlled, owner-gated path.

    AUTH-2: The org is created in approval_status='pending_approval'. A signed
    approval token (stored as its SHA-256 hash) is emailed to AGENTIQ_ADMIN_EMAIL.
    No JWT is issued — the response is a pending-approval confirmation only. The
    registrant cannot log in until a CloudFulcrum admin clicks Approve. Raises
    EmailAlreadyExistsError (409) / RegistrationError (400) on bad input.
    """
    from app.email_service import send_org_approval_request_email

    ensure_auth_tables()
    email = email.strip().lower()
    org_name = (org_name or "").strip()
    if not org_name:
        raise RegistrationError("Organization name is required")
    if not email:
        raise RegistrationError("Email is required")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise RegistrationError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
        )
    if _get_user_by_email(email) is not None:
        raise EmailAlreadyExistsError("Email already registered")

    user_id = str(uuid4())
    password_hash = hash_password(password)
    now = db.now_iso()

    org_id = str(uuid4())
    role = "owner"  # registration always creates and owns a NEW workspace (issue #5)

    # AUTH-2: generate single-use approval token; store only its SHA-256 hash.
    approval_token = secrets.token_urlsafe(32)
    approval_token_hash = hashlib.sha256(approval_token.encode()).hexdigest()
    approval_expires = datetime.now(timezone.utc) + timedelta(days=APPROVAL_TOKEN_EXPIRY_DAYS)

    con = db.connect()
    try:
        cur = con.cursor()
        # Always a fresh org — no name-based join. Single transaction: roll back
        # every row if any insert fails.
        cur.execute(
            "INSERT INTO orgs "
            "(id, name, created_at, approval_status, approval_token_hash, approval_token_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (org_id, org_name, now, "pending_approval", approval_token_hash, approval_expires),
        )
        cur.execute(
            "INSERT INTO users (id, email, password_hash, is_active, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, email, password_hash, True, now),
        )
        cur.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, user_id, role, now),
        )
        con.commit()
    except psycopg2.IntegrityError as exc:
        con.rollback()
        # Lost the race on the global unique email index.
        raise EmailAlreadyExistsError("Email already registered") from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    # Notify the admin — outside the DB transaction so a transient email failure
    # does not roll back the registration. The org is persisted; the admin can be
    # re-notified manually if the email bounces.
    try:
        send_org_approval_request_email(
            admin_email=AGENTIQ_ADMIN_EMAIL,
            org_name=org_name,
            registrant_email=email,
            approval_token=approval_token,
            org_id=org_id,
        )
    except Exception:
        logger.exception(
            "Failed to send org approval request email for org %s (%s). "
            "The org was created in pending_approval state. "
            "Notify %s manually.",
            org_id, org_name, AGENTIQ_ADMIN_EMAIL,
        )

    return {
        "status": "pending_approval",
        "message": (
            "Your organisation has been submitted for approval. "
            "You will be notified once it is reviewed."
        ),
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

    AUTH-2 / AT-354: after credential verification, the org's approval_status is
    checked. pending_approval raises OrgPendingApprovalError (403); rejected raises
    OrgRejectedError (403). Both are distinct from the generic 401 used for bad
    credentials. The check occurs after password verification deliberately — this
    avoids leaking whether a pending/rejected org email exists to callers who supply
    the wrong password.
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

    # Credentials are valid — now enforce the org approval gate (AUTH-2 / AT-354).
    #
    # Review #2: a correct-password login for a pending/rejected org is NOT a
    # failed credential attempt, so it must NOT be recorded as one. Counting it
    # toward the per-email lockout would lock a registrant out after 5 polite
    # "is my org approved yet?" attempts. The rate limit exists to throttle
    # WRONG-password brute force; a correct password against an ungated-yet org
    # is not that. We record nothing here (neither success nor failure).
    approval_status = _get_org_approval_status(membership["org_id"])
    if approval_status == "pending_approval":
        raise OrgPendingApprovalError(
            "Your organisation is awaiting approval. "
            "You will receive an email once it is reviewed."
        )
    if approval_status == "rejected":
        raise OrgRejectedError(
            "This organisation registration was not approved. "
            "Contact your CloudFulcrum representative."
        )

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
        cur = con.cursor()
        cur.execute(
            "SELECT id, email, password_hash, is_active FROM users WHERE email = %s",
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
        cur = con.cursor()
        cur.execute("SELECT name FROM orgs WHERE id = %s", (org_id,))
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _get_org_approval_status(org_id: str) -> str:
    """Return the org's approval_status. Falls back to 'active' when the column
    does not exist (pre-migration environments) so legacy orgs are never blocked."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT approval_status FROM orgs WHERE id = %s", (org_id,))
        row = cur.fetchone()
    except Exception:
        # Column absent (pre-T1 DB) — treat as active so old envs keep working.
        return "active"
    finally:
        con.close()
    if row is None:
        return "active"
    return row[0] if row[0] else "active"


def _get_workspace_member(user_id: str) -> dict | None:
    """Return {org_id, role} for the user's workspace membership (POC: one each)."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT org_id, role FROM workspace_members WHERE user_id = %s "
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
        cur = con.cursor()
        cur.execute(
            "UPDATE users SET last_login_at = %s WHERE id = %s",
            (db.now_iso(), user_id),
        )
        con.commit()
    finally:
        con.close()


