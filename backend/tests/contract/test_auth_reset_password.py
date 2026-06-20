"""Contract tests for the forgot/reset-password flow — CS-3 (T9).

Acceptance criteria covered:
  AC11 — POST /api/auth/forgot-password returns 200 for BOTH a registered and an
         unregistered email, with an identical body (no enumeration).
  AC12 — POST /api/auth/reset-password with an expired token returns 400.
  + weak new password -> 422; strong new password -> 200 and actually changes the
    login password; token is single-use / cleared after success; unknown token
    -> 400; the reset email is dispatched only for a registered email.

Storage: the reset token's SHA-256 hash + 1-hour expiry are stored on the users
row (reset_token_hash / reset_token_expires_at). Tests age the token by editing
reset_token_expires_at directly, the way test_auth_contract ages invites in KV.

These use the `client` fixture and the test_auth_contract helper style. The
forgot-password endpoint echoes the raw reset_token in the (non-production) test
environment, so tests read it from the response — mirroring the invite flow.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from app import db

# A password that satisfies both the registration min-length check and the CS-3
# reset strength rule (>=8, upper, lower, special).
STRONG = "Ownerpass1!"
NEW_STRONG = "Newpass1!"


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _register(client, *, email=None, password=STRONG):
    email = email or _email()
    resp = client.post(
        "/api/auth/register",
        json={"org_name": f"Org_{uuid.uuid4().hex[:8]}", "email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return email


def _forgot(client, email: str):
    return client.post("/api/auth/forgot-password", json={"email": email})


def _request_reset_token(client, email: str) -> str:
    """Register-independent helper: trigger forgot-password and return the raw
    token the non-prod endpoint echoes back."""
    resp = _forgot(client, email)
    assert resp.status_code == 200, resp.text
    token = resp.json().get("reset_token")
    assert token, "non-production forgot-password should echo reset_token"
    return token


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── AC11: forgot-password is always 200, identical shape ──────────────────────


def test_ac11_forgot_returns_200_for_registered_email(client):
    email = _register(client)
    resp = _forgot(client, email)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_ac11_forgot_returns_200_for_unregistered_email(client):
    resp = _forgot(client, _email())  # never registered
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_ac11_forgot_does_not_enumerate(client):
    """Registered and unregistered emails must be indistinguishable: same status,
    and the unregistered response must NOT carry a reset_token."""
    registered = _register(client)
    reg_resp = _forgot(client, registered)
    unreg_resp = _forgot(client, _email())

    assert reg_resp.status_code == unreg_resp.status_code == 200
    assert reg_resp.json()["status"] == unreg_resp.json()["status"] == "ok"
    # The only difference is the non-prod test seam: a real (registered) token is
    # minted; the unregistered path mints nothing.
    assert "reset_token" in reg_resp.json()
    assert "reset_token" not in unreg_resp.json()


def test_forgot_is_case_insensitive_on_email(client):
    """Emails are stored lowercased; a mixed-case request must still find the
    user and mint a token (otherwise a real user silently never gets a reset)."""
    email = _register(client)
    resp = _forgot(client, email.upper())
    assert resp.status_code == 200, resp.text
    assert resp.json().get("reset_token"), "mixed-case email must resolve the user"


def test_forgot_stores_only_the_hash_not_the_raw_token(client):
    """Security: the users row stores the SHA-256 hash, never the raw token."""
    email = _register(client)
    token = _request_reset_token(client, email)

    con = db.connect()
    try:
        row = con.execute(
            "SELECT reset_token_hash, reset_token_expires_at FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == _token_hash(token)      # hash stored
    assert row[0] != token                    # raw token NOT stored
    assert row[1] is not None                 # expiry set


# ── Email dispatch seam ───────────────────────────────────────────────────────


def test_forgot_sends_email_for_registered_user(client, monkeypatch):
    sent = {}
    import app.routes_auth as ra
    # Patch the name bound in routes_auth (it imports the function by name).
    monkeypatch.setattr(
        ra, "send_password_reset_email",
        lambda email, token: sent.update(email=email, token=token),
    )
    email = _register(client)
    resp = _forgot(client, email)
    assert resp.status_code == 200, resp.text
    assert sent.get("email") == email.strip().lower()
    assert sent.get("token")  # a token was generated and passed to the mailer


def test_forgot_does_not_send_email_for_unregistered_user(client, monkeypatch):
    calls = []
    import app.routes_auth as ra
    monkeypatch.setattr(
        ra, "send_password_reset_email",
        lambda email, token: calls.append(email),
    )
    resp = _forgot(client, _email())
    assert resp.status_code == 200, resp.text
    assert calls == []  # no mail for an unknown email


def test_forgot_still_200_when_email_send_raises(client, monkeypatch):
    """A transport failure must not change the response or leak existence."""
    import app.routes_auth as ra

    def _boom(email, token):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ra, "send_password_reset_email", _boom)
    email = _register(client)
    resp = _forgot(client, email)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


# ── reset-password: success path ──────────────────────────────────────────────


def test_reset_with_strong_password_returns_200_and_changes_login(client):
    email = _register(client, password=STRONG)
    token = _request_reset_token(client, email)

    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": NEW_STRONG},
    )
    assert resp.status_code == 200, resp.text

    # Old password no longer works; new one does — proves password_hash changed.
    old = client.post("/api/auth/login", json={"email": email, "password": STRONG})
    assert old.status_code == 401, old.text
    new = client.post("/api/auth/login", json={"email": email, "password": NEW_STRONG})
    assert new.status_code == 200, new.text


def test_reset_clears_token_and_is_single_use(client):
    email = _register(client)
    token = _request_reset_token(client, email)

    first = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": NEW_STRONG},
    )
    assert first.status_code == 200, first.text

    # The reset fields are cleared on the user row.
    con = db.connect()
    try:
        row = con.execute(
            "SELECT reset_token_hash, reset_token_expires_at FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    finally:
        con.close()
    assert row[0] is None and row[1] is None

    # Replaying the same token now fails (single-use).
    second = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": "Another1!"},
    )
    assert second.status_code == 400, second.text


def test_reset_accepts_legacy_token_field_alias(client):
    """The frontend sends `reset_token`; `token` is accepted as a back-compat alias."""
    email = _register(client)
    token = _request_reset_token(client, email)
    resp = client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": NEW_STRONG},
    )
    assert resp.status_code == 200, resp.text


# ── reset-password: token failures (400) ──────────────────────────────────────


def test_ac12_reset_with_expired_token_returns_400(client):
    email = _register(client)
    token = _request_reset_token(client, email)

    # Age the stored expiry one hour into the past.
    con = db.connect()
    try:
        con.execute(
            "UPDATE users SET reset_token_expires_at = %s WHERE email = %s",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), email),
        )
        con.commit()
    finally:
        con.close()

    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": NEW_STRONG},
    )
    assert resp.status_code == 400, resp.text


def test_reset_with_unknown_token_returns_400(client):
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": str(uuid.uuid4()), "new_password": NEW_STRONG},
    )
    assert resp.status_code == 400, resp.text


def test_reset_missing_fields_is_422(client):
    resp = client.post("/api/auth/reset-password", json={"reset_token": "x"})
    assert resp.status_code == 422, resp.text


# ── reset-password: weak password (422) ───────────────────────────────────────


def test_reset_with_weak_password_returns_422(client):
    email = _register(client)
    token = _request_reset_token(client, email)
    resp = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": "weak"},
    )
    assert resp.status_code == 422, resp.text
    assert "Password must contain" in resp.json()["detail"]


def test_reset_weak_password_does_not_consume_token(client):
    """A 422 (weak password) must leave the token usable, so the user can retry
    with a stronger password without requesting a brand-new link."""
    email = _register(client)
    token = _request_reset_token(client, email)

    weak = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": "weak"},
    )
    assert weak.status_code == 422, weak.text

    # Same token, now with a strong password, succeeds.
    ok = client.post(
        "/api/auth/reset-password",
        json={"reset_token": token, "new_password": NEW_STRONG},
    )
    assert ok.status_code == 200, ok.text


def test_reset_rejects_each_missing_character_class(client):
    """Each composition rule (upper/lower/special/length) independently 422s."""
    cases = [
        "alllower1!",   # no uppercase
        "ALLUPPER1!",   # no lowercase
        "NoSpecial1",   # no special char
        "Ab1!",         # too short (<8)
    ]
    for weak in cases:
        email = _register(client)
        token = _request_reset_token(client, email)
        resp = client.post(
            "/api/auth/reset-password",
            json={"reset_token": token, "new_password": weak},
        )
        assert resp.status_code == 422, f"{weak!r} -> {resp.status_code}: {resp.text}"
