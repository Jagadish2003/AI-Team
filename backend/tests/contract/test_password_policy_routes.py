"""Contract tests for CS-3 — password strength enforcement on the auth routes.

The backend is the real security boundary (the frontend indicator is only UX):
the password-CREATION routes reject weak passwords with 422 even when the caller
bypasses the browser and hits the API directly. Login is exempt so existing
users with pre-CS-3 passwords are never locked out.

AC coverage (this task = doc T2):
  AC4 — POST /api/auth/register weak password → 422 listing the unmet rules.
  AC5 — POST /api/auth/register strong password → 201.
  AC6 — POST /api/auth/login with an existing weak password → 200 (not enforced).
  AC7 — POST /api/auth/accept-invite weak → 422; strong → 200 + JWT.
  AC8 — POST /api/auth/reset-password weak → 422; strong → 200.
Plus reset-flow edges: forgot-password is enumeration-safe (always 200, same
message), and reset-password with an invalid / expired / used token → 400.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import db
from auth_helpers import activate_org_by_email

STRONG = "Password1!"
WEAK = "password"  # 8 chars, lowercase only — missing uppercase + special


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── AC4 / AC5: register ──────────────────────────────────────────────────────


def test_ac4_register_weak_password_returns_422_listing_rules(client):
    resp = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": _email(), "password": WEAK},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("Password must contain:")
    assert "at least one uppercase letter" in detail
    assert "at least one special character" in detail


def test_ac4_register_weak_password_creates_no_user(client):
    email = _email()
    resp = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": email, "password": WEAK},
    )
    assert resp.status_code == 422
    con = db.connect()
    try:
        row = con.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    finally:
        con.close()
    assert row is None, "no account may be created when the password is rejected"


def test_ac5_register_strong_password_returns_201(client):
    email = _email()
    resp = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": email, "password": STRONG},
    )
    assert resp.status_code == 201, resp.text
    # AUTH-2: the JWT is issued on login after approval, not at register.
    activate_org_by_email(email)
    login = client.post("/api/auth/login", json={"email": email, "password": STRONG})
    assert login.status_code == 200, login.text
    assert login.json()["token"]


# ── AC6: login is exempt ──────────────────────────────────────────────────────


def test_ac6_login_does_not_enforce_strength_for_existing_weak_password(client):
    """A user whose stored password predates CS-3 still logs in.

    The weak password is planted through the logic layer (register_org_and_owner),
    which enforces only the length floor — exactly an existing pre-CS-3 account.
    The login ROUTE must accept it, because login never calls the strength
    validator (applying it would lock those users out).
    """
    from app.auth.user_auth import register_org_and_owner

    email = _email()
    register_org_and_owner(org_name="Legacy", email=email, password=WEAK)
    activate_org_by_email(email)  # AUTH-2 admin approval (simulated) so login works

    # The register ROUTE would reject this very password…
    rejected = client.post(
        "/api/auth/register",
        json={"org_name": "X", "email": _email(), "password": WEAK},
    )
    assert rejected.status_code == 422

    # …but login accepts the existing weak password unchanged.
    ok = client.post("/api/auth/login", json={"email": email, "password": WEAK})
    assert ok.status_code == 200, ok.text
    assert ok.json()["token"]


# ── AC7: accept-invite ────────────────────────────────────────────────────────


def _make_invite(client) -> str:
    owner_email = _email()
    owner = client.post(
        "/api/auth/register",
        json={"org_name": f"Org {uuid.uuid4().hex[:6]}", "email": owner_email, "password": STRONG},
    )
    assert owner.status_code == 201, owner.text
    # AUTH-2: approve + login to get the owner JWT.
    activate_org_by_email(owner_email)
    owner_token = client.post(
        "/api/auth/login", json={"email": owner_email, "password": STRONG}
    ).json()["token"]
    inv = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )
    assert inv.status_code == 201, inv.text
    return inv.json()["invite_token"]


def test_ac7_accept_invite_weak_password_returns_422(client):
    invite_token = _make_invite(client)
    resp = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": WEAK},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("Password must contain:")


def test_ac7_accept_invite_strong_password_activates_and_returns_jwt(client):
    invite_token = _make_invite(client)
    resp = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": STRONG},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["user"]["role"] == "analyst"


# ── AC8: reset-password ───────────────────────────────────────────────────────


def _register_and_get_reset_token(client) -> tuple[str, str]:
    email = _email()
    reg = client.post(
        "/api/auth/register",
        json={"org_name": f"Org {uuid.uuid4().hex[:6]}", "email": email, "password": STRONG},
    )
    assert reg.status_code == 201, reg.text
    activate_org_by_email(email)  # AUTH-2 admin approval (simulated) so post-reset login works
    fp = client.post("/api/auth/forgot-password", json={"email": email})
    assert fp.status_code == 200, fp.text
    return email, fp.json()["reset_token"]


def test_ac8_reset_password_weak_returns_422(client):
    _, reset_token = _register_and_get_reset_token(client)
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": reset_token, "new_password": WEAK},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("Password must contain:")


def test_ac8_reset_password_strong_resets_and_returns_200(client):
    email, reset_token = _register_and_get_reset_token(client)
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": reset_token, "new_password": "Newpass1!"},
    )
    assert resp.status_code == 200, resp.text
    # The new password now authenticates.
    ok = client.post("/api/auth/login", json={"email": email, "password": "Newpass1!"})
    assert ok.status_code == 200, ok.text


# ── reset-flow edges ──────────────────────────────────────────────────────────


def test_forgot_password_always_200_with_same_message(client):
    email, _ = _register_and_get_reset_token(client)  # a real, registered email
    known = client.post("/api/auth/forgot-password", json={"email": email})
    unknown = client.post("/api/auth/forgot-password", json={"email": _email()})
    assert known.status_code == unknown.status_code == 200
    # Identical user-facing status → no account enumeration.
    assert known.json()["status"] == unknown.json()["status"] == "ok"
    assert "reset_token" in known.json()
    assert "reset_token" not in unknown.json()


def test_reset_password_invalid_token_returns_400(client):
    # Strong password passes the strength gate; the unknown token then 400s.
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": "not-a-real-token", "new_password": "Newpass1!"},
    )
    assert resp.status_code == 400, resp.text


def test_reset_password_expired_token_returns_400(client):
    email, reset_token = _register_and_get_reset_token(client)
    # Age the DB-backed reset token past its 1-hour TTL.
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET reset_token_expires_at = %s WHERE email = %s",
            (expired_at, email),
        )
        con.commit()
    finally:
        con.close()
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": reset_token, "new_password": "Newpass1!"},
    )
    assert resp.status_code == 400, resp.text


def test_reset_password_token_is_single_use(client):
    _, reset_token = _register_and_get_reset_token(client)
    first = client.post(
        "/api/auth/reset-password",
        json={"reset_token": reset_token, "new_password": "Newpass1!"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/auth/reset-password",
        json={"reset_token": reset_token, "new_password": "Newpass2!"},
    )
    assert second.status_code == 400, second.text
