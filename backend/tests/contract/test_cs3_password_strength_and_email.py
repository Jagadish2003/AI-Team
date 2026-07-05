"""CS-3 end-to-end contract tests — Transactional Email + Password Strength (T11).

This suite proves CS-3 at the externally visible API-behavior level, not just via
internal helpers. It is the cross-cutting story coverage spanning password
strength, registration, invite acceptance, password reset, login, and email.

Acceptance-criteria map (CS-3 Section 8):
  AC1  — validate_password_strength('Password1!') returns [] (all rules met).
  AC2  — a weak password returns the correct missing-requirement messages.
  AC3  — 'Pass1!' returns only the length error.
  AC4  — POST /api/auth/register with a weak password returns 422 listing unmet rules.
  AC5  — POST /api/auth/register with a strong password returns 201.
  AC6  — POST /api/auth/login with an existing account's OLD weak password still
         returns 200 — login does NOT enforce the new strength rule (no lockout).
  AC7  — POST /api/auth/accept-invite: weak -> 422; strong -> account + JWT.
  AC8  — POST /api/auth/reset-password: weak -> 422; strong -> 200.
  AC10 — register sends a welcome email; invite sends an invitation email.
  AC14 — email_service never breaks the auth flow: a send failure still yields
         the route's normal response.

Email is patched at the call site (the names bound in app.routes_auth), so these
tests never require a real SendGrid/SES account.
"""
from __future__ import annotations

import uuid

import pytest

import app.routes_auth as routes_auth
from app import db
from app.auth.user_auth import hash_password, validate_password_strength
from auth_helpers import activate_org_by_email, rand_org_name

# A password satisfying every CS-3 rule (>=8, upper, lower, special).
STRONG = "Password1!"


# ── helpers ──────────────────────────────────────────────────────────────────


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, *, email=None, password=STRONG):
    email = email or _email()
    resp = client.post(
        "/api/auth/register",
        json={"org_name": rand_org_name(), "email": email, "password": password},
    )
    # AUTH-2: a successful registration leaves the org pending_approval. Approve it
    # (simulating the admin click) so the registrant can subsequently log in.
    if resp.status_code == 201:
        activate_org_by_email(email)
    return resp, email


def _register_owner(client) -> str:
    resp, email = _register(client)
    assert resp.status_code == 201, resp.text
    # AUTH-2: the JWT is issued on login (after approval), not at register.
    login = client.post("/api/auth/login", json={"email": email, "password": STRONG})
    assert login.status_code == 200, login.text
    return login.json()["token"]


def _create_invite(client, owner_token: str, *, email=None, role="analyst") -> str:
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": email or _email(), "role": role},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["invite_token"]


def _request_reset_token(client, email: str) -> str:
    resp = client.post("/api/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.text
    token = resp.json().get("reset_token")
    assert token, "non-production forgot-password should echo reset_token"
    return token


# ── AC1 / AC2 / AC3: validate_password_strength() directly ────────────────────


def test_ac1_valid_password_returns_no_errors():
    assert validate_password_strength("Password1!") == []


def test_ac2_weak_password_returns_correct_missing_requirements():
    # 'password' is 8 chars + lowercase, so length and lowercase are MET; it is
    # missing uppercase and a special character. (The doc's AC2 prose says "3
    # errors" but itself lists only these two and notes length is met.)
    unmet = validate_password_strength("password")
    assert "at least one uppercase letter" in unmet
    assert "at least one special character" in unmet
    assert "at least 8 characters" not in unmet
    assert "at least one lowercase letter" not in unmet


def test_ac3_short_password_returns_only_length_error():
    # 'Pass1!' has upper + lower + special but is < 8 chars.
    assert validate_password_strength("Pass1!") == ["at least 8 characters"]


def test_strength_messages_are_specific_per_missing_class():
    assert "at least one uppercase letter" in validate_password_strength("lowercase1!")
    assert "at least one lowercase letter" in validate_password_strength("UPPERCASE1!")
    assert "at least one special character" in validate_password_strength("NoSpecial1")
    # A fully empty password is missing all four rules.
    assert len(validate_password_strength("")) == 4


# ── AC4 / AC5: register enforces strength at the API level ────────────────────


def test_ac4_register_weak_password_returns_422_with_unmet_rules(client):
    resp = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": _email(), "password": "password"},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "Password must contain" in detail
    assert "uppercase" in detail and "special" in detail


def test_ac5_register_strong_password_returns_201(client):
    resp, email = _register(client, password=STRONG)
    assert resp.status_code == 201, resp.text
    # AUTH-2: register returns a pending-approval ack; the owner JWT comes from
    # logging in after approval (_register approved the org above).
    login = client.post("/api/auth/login", json={"email": email, "password": STRONG})
    assert login.status_code == 200, login.text
    assert login.json()["token"]
    assert login.json()["user"]["role"] == "owner"


@pytest.mark.parametrize(
    "weak",
    [
        "short",            # too short + missing upper/special
        "alllowercase1!",   # missing uppercase
        "ALLUPPERCASE1!",   # missing lowercase
        "NoSpecialChar1",   # missing special character
        "Sh0rt!",           # has upper/lower/special but only 6 chars
    ],
)
def test_register_rejects_each_weak_class_with_422(client, weak):
    resp = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": _email(), "password": weak},
    )
    assert resp.status_code == 422, f"{weak!r} -> {resp.status_code}: {resp.text}"


# ── AC6: login does NOT enforce the new strength rule (no lockout) ─────────────


def test_ac6_login_allows_existing_account_with_old_weak_password(client):
    """The headline backwards-compat guarantee: an account whose password predates
    CS-3 (and would fail the new strength rule) must still log in. We register
    normally, then overwrite the stored hash with a hash of an OLD weak password
    (simulating a pre-CS-3 user) — bypassing the registration gate the same way a
    historical row would — and prove login succeeds."""
    _, email = _register(client, password=STRONG)

    old_weak = "pass1234"  # 8 chars but no upper/special -> fails the new rule.
    assert validate_password_strength(old_weak), "test premise: old pw is weak"

    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (hash_password(old_weak), email),
        )
        con.commit()
    finally:
        con.close()

    resp = client.post("/api/auth/login", json={"email": email, "password": old_weak})
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]


def test_login_does_not_run_strength_validation_path(client):
    """A second angle on AC6: a brand-new login with a weak (but correct) password
    returns 200, never 422 — proving the login route has no strength gate."""
    _, email = _register(client, password=STRONG)
    weak = "weakpw12"
    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (hash_password(weak), email),
        )
        con.commit()
    finally:
        con.close()

    resp = client.post("/api/auth/login", json={"email": email, "password": weak})
    assert resp.status_code == 200, resp.text
    # And a wrong password is still a normal 401 (not a 422 strength rejection).
    bad = client.post("/api/auth/login", json={"email": email, "password": "totally-wrong"})
    assert bad.status_code == 401, bad.text


# ── AC7: accept-invite enforces strength ──────────────────────────────────────


def test_ac7_accept_invite_weak_password_returns_422(client):
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)
    resp = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "password"},
    )
    assert resp.status_code == 422, resp.text
    assert "Password must contain" in resp.json()["detail"]


def test_ac7_accept_invite_strong_password_creates_account_and_returns_jwt(client):
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token, role="analyst")
    resp = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": STRONG},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]
    assert resp.json()["user"]["role"] == "analyst"


def test_accept_invite_weak_password_does_not_consume_token(client):
    """A weak-password 422 must leave the invite usable for a strong retry."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    weak = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "password"},
    )
    assert weak.status_code == 422, weak.text

    ok = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": STRONG},
    )
    assert ok.status_code == 200, ok.text


# ── AC8: reset-password enforces strength ─────────────────────────────────────


def test_ac8_reset_password_weak_returns_422(client):
    _, email = _register(client, password=STRONG)
    token = _request_reset_token(client, email)
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": "password"},
    )
    assert resp.status_code == 422, resp.text
    assert "Password must contain" in resp.json()["detail"]


def test_ac8_reset_password_strong_returns_200_and_changes_login(client):
    _, email = _register(client, password=STRONG)
    token = _request_reset_token(client, email)
    new_strong = "Brandnew2!"
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": new_strong},
    )
    assert resp.status_code == 200, resp.text
    # The reset actually rotated the password.
    assert client.post("/api/auth/login", json={"email": email, "password": STRONG}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": new_strong}).status_code == 200


def test_strength_rule_is_consistent_across_all_three_entry_points(client):
    """The same weak password is rejected with 422 by register, accept-invite, and
    reset-password — one rule, one contract, three entry points (CS-3 §1 'Locked')."""
    weak = "nopunch1"  # lower+digit, no upper, no special

    # register
    r = client.post(
        "/api/auth/register",
        json={"org_name": "Acme", "email": _email(), "password": weak},
    )
    assert r.status_code == 422, r.text

    # accept-invite
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)
    a = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": weak},
    )
    assert a.status_code == 422, a.text

    # reset-password
    _, email = _register(client, password=STRONG)
    token = _request_reset_token(client, email)
    rp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": weak},
    )
    assert rp.status_code == 422, rp.text


# ── AC10: emails are sent (patched seam, no real provider) ────────────────────


def test_ac10_register_sends_welcome_email(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        routes_auth, "send_welcome_email",
        lambda to, org_name: sent.update(to=to, org_name=org_name) or True,
    )
    resp, email = _register(client, password=STRONG)
    assert resp.status_code == 201, resp.text
    assert sent.get("to") == email
    assert sent.get("org_name")  # the org display name is passed through


def test_ac10_invite_sends_invitation_email(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        routes_auth, "send_invite_email",
        lambda to, token, org_name, role: captured.update(
            to=to, token=token, org_name=org_name, role=role
        ) or True,
    )
    owner_token = _register_owner(client)
    invitee = _email()
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": invitee, "role": "viewer"},
    )
    assert resp.status_code == 201, resp.text
    assert captured.get("to") == invitee
    assert captured.get("role") == "viewer"
    assert captured.get("token")  # the accept-invite token is handed to the mailer


def test_forgot_password_sends_reset_email(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        routes_auth, "send_password_reset_email",
        lambda email, token: sent.update(email=email, token=token),
    )
    _, email = _register(client, password=STRONG)
    resp = client.post("/api/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.text
    assert sent.get("email") == email
    assert sent.get("token")


# ── AC14: email failure is non-blocking ───────────────────────────────────────


def test_ac14_register_succeeds_when_welcome_email_raises(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("SendGrid down")

    monkeypatch.setattr(routes_auth, "send_welcome_email", _boom)
    resp, _ = _register(client, password=STRONG)
    assert resp.status_code == 201, resp.text  # registration unaffected
    # AUTH-2: register returns the pending-approval ack (no JWT); the point is the
    # welcome-email failure didn't break registration.
    assert resp.json()["status"] == "pending_approval"


def test_ac14_invite_succeeds_when_invite_email_raises(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("SendGrid down")

    monkeypatch.setattr(routes_auth, "send_invite_email", _boom)
    owner_token = _register_owner(client)
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )
    assert resp.status_code == 201, resp.text  # invite unaffected
    # The invite is still usable (token issued despite the email failure).
    assert resp.json()["invite_token"]


def test_ac14_forgot_password_succeeds_when_reset_email_raises(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("SendGrid down")

    monkeypatch.setattr(routes_auth, "send_password_reset_email", _boom)
    _, email = _register(client, password=STRONG)
    resp = client.post("/api/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
