"""Contract tests for the auth logic layer — AUTH-1 / AT-233.

Exercises app.auth.user_auth directly (the HTTP routes are AT-234). The contract
conftest runs Alembic to head and points DB_PATH at a temp DB, so the orgs /
users / login_attempts / workspace_members tables already exist.

Covered ACs:
  AC2  — register creates org + user + owner member in one txn; JWT carries
         org_id + role from workspace_members.
  AC3  — duplicate email is rejected (EmailAlreadyExistsError -> 409 in routes).
  AC4  — login JWT contains sub, org_id, role, jti, iat, exp.
  AC5  — wrong password and unknown email: identical message, both run bcrypt.
  AC6  — 5 failed attempts for an email -> 6th raises RateLimitError with the
         actual remaining seconds (~900 immediately after the burst).
  AC7  — rate limiting is scoped to the email, NOT the IP: a throttled account
         does not lock out another user sharing the same IP (deliberate
         deviation from the original per-IP AC7 — see user_auth docstring).
  AC8  — a successful login clears the email's failed-attempt count.
  AC9  — logout_token revokes the jti; verify_jwt then rejects it.
  AC16 — password_hash is a $2b$12$ bcrypt hash; no plaintext leaks.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app import db
from app.auth import user_auth


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _ip() -> str:
    # Unique-per-call IP so unrelated tests never share a rate-limit bucket.
    h = uuid.uuid4().int
    return f"10.{h % 256}.{(h >> 8) % 256}.{(h >> 16) % 256}"


# ---------------------------------------------------------------------------
# Password hashing (AC16)
# ---------------------------------------------------------------------------


def test_hash_password_is_bcrypt_cost_12_and_verifies():
    hashed = user_auth.hash_password("correct horse battery staple")
    assert hashed.startswith("$2b$12$")
    assert user_auth.verify_password("correct horse battery staple", hashed) is True
    assert user_auth.verify_password("wrong password", hashed) is False


def test_password_longer_than_72_bytes_is_capped():
    base = "a" * 72
    h = user_auth.hash_password(base)
    # bcrypt ignores bytes past 72 — extra chars must still verify.
    assert user_auth.verify_password(base + "EXTRA-IGNORED", h) is True


# ---------------------------------------------------------------------------
# Registration (AC2 / AC3 / AC16)
# ---------------------------------------------------------------------------


def test_register_creates_org_user_member_and_jwt():
    email = _email()
    result = user_auth.register_org_and_owner("Acme Bank", email, "supersecret1")

    assert result["user"]["email"] == email
    assert result["user"]["role"] == "owner"
    org_id = result["user"]["org_id"]
    user_id = result["user"]["id"]

    # JWT carries org_id + role sourced from workspace_members (AC2/AC4).
    payload = user_auth.verify_jwt(result["token"])
    assert payload["sub"] == user_id
    assert payload["org_id"] == org_id
    assert payload["role"] == "owner"

    # All three rows exist.
    con = db.connect()
    try:
        assert con.execute("SELECT name FROM orgs WHERE id = ?", (org_id,)).fetchone()[0] == "Acme Bank"
        assert con.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()[0] == email
        member = con.execute(
            "SELECT org_id, role FROM workspace_members WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        con.close()
    assert member == (org_id, "owner")


def test_register_normalizes_email_case_and_whitespace():
    email = _email().upper()
    result = user_auth.register_org_and_owner("Org", f"  {email}  ", "supersecret1")
    assert result["user"]["email"] == email.strip().lower()


def test_register_duplicate_email_raises():
    email = _email()
    user_auth.register_org_and_owner("Org One", email, "supersecret1")
    with pytest.raises(user_auth.EmailAlreadyExistsError):
        user_auth.register_org_and_owner("Org Two", email, "supersecret1")


def test_register_duplicate_does_not_create_second_org():
    email = _email()
    user_auth.register_org_and_owner("Org One", email, "supersecret1")
    before = db.connect()
    try:
        count_before = before.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        before.close()
    with pytest.raises(user_auth.EmailAlreadyExistsError):
        user_auth.register_org_and_owner("Org Two", email, "supersecret1")
    after = db.connect()
    try:
        count_after = after.execute("SELECT COUNT(*) FROM orgs").fetchone()[0]
    finally:
        after.close()
    assert count_after == count_before


def test_register_short_password_raises():
    with pytest.raises(user_auth.RegistrationError):
        user_auth.register_org_and_owner("Org", _email(), "short")


def test_register_stores_bcrypt_hash_no_plaintext():
    email = _email()
    password = "plaintext-never-stored-123"
    result = user_auth.register_org_and_owner("Org", email, password)

    con = db.connect()
    try:
        stored = con.execute(
            "SELECT password_hash FROM users WHERE id = ?", (result["user"]["id"],)
        ).fetchone()[0]
    finally:
        con.close()

    assert stored.startswith("$2b$12$")
    assert password not in stored
    # The returned dict must never echo the password back (AC16).
    assert "password" not in result["user"]
    assert password not in str(result)


# ---------------------------------------------------------------------------
# Login success (AC4)
# ---------------------------------------------------------------------------


def test_login_success_returns_jwt_with_required_claims():
    email = _email()
    reg = user_auth.register_org_and_owner("Claim Org", email, "supersecret1")

    result = user_auth.login(email, "supersecret1", _ip())
    assert result["user"]["org_id"] == reg["user"]["org_id"]
    assert result["user"]["role"] == "owner"

    payload = jwt.decode(
        result["token"], user_auth._jwt_secret(), algorithms=[user_auth.JWT_ALGORITHM]
    )
    for claim in ("sub", "org_id", "role", "jti", "iat", "exp"):
        assert claim in payload, f"missing claim {claim}"
    assert payload["sub"] == reg["user"]["id"]
    assert payload["org_id"] == reg["user"]["org_id"]
    assert payload["role"] == "owner"
    # 8-hour expiry.
    assert payload["exp"] - payload["iat"] == user_auth.JWT_EXPIRY_HOURS * 3600


def test_login_updates_last_login_at():
    email = _email()
    reg = user_auth.register_org_and_owner("Org", email, "supersecret1")
    user_auth.login(email, "supersecret1", _ip())
    con = db.connect()
    try:
        last = con.execute(
            "SELECT last_login_at FROM users WHERE id = ?", (reg["user"]["id"],)
        ).fetchone()[0]
    finally:
        con.close()
    assert last is not None


# ---------------------------------------------------------------------------
# Login failure — timing-safe identical message (AC5)
# ---------------------------------------------------------------------------


def test_wrong_password_and_unknown_email_identical_message_and_timing():
    email = _email()
    user_auth.register_org_and_owner("Org", email, "supersecret1")

    t0 = time.perf_counter()
    with pytest.raises(user_auth.InvalidCredentialsError) as wrong_pw:
        user_auth.login(email, "the-wrong-password", _ip())
    wrong_pw_elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
    with pytest.raises(user_auth.InvalidCredentialsError) as unknown:
        user_auth.login(_email(), "any-password", _ip())
    unknown_elapsed = time.perf_counter() - t1

    # Identical message — no account-existence leak (AC5).
    assert str(wrong_pw.value) == "Invalid email or password"
    assert str(unknown.value) == str(wrong_pw.value)

    # Both paths run a real bcrypt verification (cost 12), so neither is the
    # trivially-fast "no such user" early return that would leak existence.
    assert wrong_pw_elapsed > 0.02
    assert unknown_elapsed > 0.02


def test_login_inactive_user_is_rejected():
    # Simulate an invited-but-not-activated user (is_active=0).
    email = _email()
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO users (id, email, password_hash, is_active, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (user_id, email, user_auth.hash_password("supersecret1"), db.now_iso()),
        )
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (?, ?, 'analyst', ?)",
            (org_id, user_id, db.now_iso()),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(user_auth.InvalidCredentialsError):
        user_auth.login(email, "supersecret1", _ip())


# ---------------------------------------------------------------------------
# Rate limiting (AC6 / AC7 / AC8)
# ---------------------------------------------------------------------------


def test_rate_limit_by_email_fires_on_sixth_attempt():
    email = _email()
    user_auth.register_org_and_owner("Org", email, "supersecret1")

    # 5 failures spread across DIFFERENT IPs so only the email bucket fills.
    for _ in range(5):
        with pytest.raises(user_auth.InvalidCredentialsError):
            user_auth.login(email, "wrong-password", _ip())

    # 6th attempt — even with the CORRECT password — is throttled (AC6),
    # proving the rate-limit check runs before credential validation.
    with pytest.raises(user_auth.RateLimitError) as exc:
        user_auth.login(email, "supersecret1", _ip())
    # retry_after is the ACTUAL time remaining, not a fixed 15 minutes: it is the
    # full window minus the (small, bcrypt-dominated) time the 5 failures took, so
    # it sits just under 900s. The UI counts this down rather than always quoting
    # the full window.
    assert 880 <= exc.value.retry_after_seconds <= 900


def test_rate_limit_is_scoped_to_email_not_ip():
    """A throttled account must not lock out a different user on the same IP.

    Per-email scoping (deliberate deviation from AUTH-1 AC7): several POC testers
    share one IP (office NAT / localhost), so an IP-wide block would lock out the
    whole team. One email's failures throttle only that email; another registered
    user logs in normally from the same IP.
    """
    ip = _ip()
    blocked = _email()
    other = _email()
    user_auth.register_org_and_owner("Org Blocked", blocked, "supersecret1")
    user_auth.register_org_and_owner("Org Other", other, "supersecret2")

    # 5 failures for `blocked` from the shared IP → that email is throttled.
    for _ in range(5):
        with pytest.raises(user_auth.InvalidCredentialsError):
            user_auth.login(blocked, "wrong-password", ip)
    with pytest.raises(user_auth.RateLimitError):
        user_auth.login(blocked, "supersecret1", ip)

    # The other user, on the SAME IP with the correct password, still logs in.
    result = user_auth.login(other, "supersecret2", ip)
    assert result["user"]["email"] == other

    # And the unrelated email is nowhere near the throttle on that shared IP.
    user_auth.check_login_rate_limit(other, ip)  # must not raise


def test_successful_login_clears_failed_attempts_for_email():
    email = _email()
    user_auth.register_org_and_owner("Org", email, "supersecret1")

    # 4 failures (under the threshold of 5) on distinct IPs.
    for _ in range(4):
        with pytest.raises(user_auth.InvalidCredentialsError):
            user_auth.login(email, "wrong-password", _ip())

    window_start = datetime.now(timezone.utc) - timedelta(minutes=60)
    assert user_auth._count_failed_attempts(since=window_start, email=email) == 4

    # A successful login clears the email's failed-attempt count (AC8).
    user_auth.login(email, "supersecret1", _ip())
    assert user_auth._count_failed_attempts(since=window_start, email=email) == 0

    # And the email is no longer anywhere near the throttle.
    user_auth.check_login_rate_limit(email, _ip())  # must not raise


# ---------------------------------------------------------------------------
# JWT / logout (AC9)
# ---------------------------------------------------------------------------


def test_logout_revokes_token():
    email = _email()
    user_auth.register_org_and_owner("Org", email, "supersecret1")
    token = user_auth.login(email, "supersecret1", _ip())["token"]

    assert user_auth.verify_jwt(token)["email"] == email  # valid before logout
    user_auth.logout_token(token)
    with pytest.raises(user_auth.InvalidTokenError):
        user_auth.verify_jwt(token)


def test_logout_is_idempotent():
    email = _email()
    user_auth.register_org_and_owner("Org", email, "supersecret1")
    token = user_auth.login(email, "supersecret1", _ip())["token"]
    user_auth.logout_token(token)
    user_auth.logout_token(token)  # second call must not raise
    with pytest.raises(user_auth.InvalidTokenError):
        user_auth.verify_jwt(token)


def test_verify_jwt_rejects_tampered_and_expired_tokens():
    # Wrong-secret signature.
    forged = jwt.encode(
        {"sub": "x", "exp": int(time.time()) + 600}, "not-the-secret", algorithm="HS256"
    )
    with pytest.raises(user_auth.InvalidTokenError):
        user_auth.verify_jwt(forged)

    # Expired but correctly signed.
    expired = jwt.encode(
        {
            "sub": "x",
            "jti": str(uuid.uuid4()),
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,
        },
        user_auth._jwt_secret(),
        algorithm=user_auth.JWT_ALGORITHM,
    )
    with pytest.raises(user_auth.InvalidTokenError):
        user_auth.verify_jwt(expired)


def test_logout_expired_token_is_noop():
    expired = jwt.encode(
        {"sub": "x", "jti": str(uuid.uuid4()), "exp": int(time.time()) - 10},
        user_auth._jwt_secret(),
        algorithm=user_auth.JWT_ALGORITHM,
    )
    # Already invalid — logout should silently no-op, not raise.
    user_auth.logout_token(expired)
