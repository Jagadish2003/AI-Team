"""Route-level contract tests for AUTH-1 — AT-240.

Exercises the /api/auth/* HTTP endpoints (and the cross-route integration AC15)
through TestClient, complementing the logic-layer suite in test_user_auth.py and
the schema suite in test_users_login_attempts_schema.py.

AC coverage map (the testable criteria from AUTH-1 Section 10):
  AC2  — register creates org + user + owner member; JWT carries org_id + role.
  AC3  — duplicate-email register → 409.
  AC4  — login JWT carries sub / org_id / role / jti / iat / exp.
  AC5  — wrong password and unknown email → 401, identical message.
  AC6  — 6th failed login for an email → 429 with Retry-After.
  AC7  — rate limiting is email-scoped, NOT IP-scoped (deliberate deviation —
         see user_auth docstring / deployment/README.md): another user on the
         same client still logs in.
  AC8  — a successful login clears the email's failed-attempt count.
  AC9  — after logout the same token is rejected (401) on a protected route.
  AC10 — invite returns invite_token in non-production; 501 in production.
  AC11 — accept-invite is single-use (also in test_auth_invite_routes.py).
  AC12 — accept-invite with an expired token → 400.
  AC15 — register → JWT passes require_auth + require_role on an existing route
         (GET /api/integration-hub/workspace-catalog → 200); login JWT too.
  AC16 — password_hash is a $2b$12$ bcrypt hash; no plaintext anywhere.

AC1 (schema) is covered by test_users_login_attempts_schema.py. AC13 (frontend
401 interceptor) is covered by the frontend Vitest suites. AC14 is design review.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app import db
from app.routes_auth import _INVITE_KV_PREFIX, _token_hash

WORKSPACE_CATALOG = "/api/integration-hub/workspace-catalog"


# ── helpers ────────────────────────────────────────────────────────────────────


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _claims(token: str) -> dict:
    """Decode JWT claims without signature verification (for assertions only)."""
    return pyjwt.decode(
        token, options={"verify_signature": False, "verify_exp": False}, algorithms=["HS256"]
    )


def _register(client, *, org=None, email=None, password="Ownerpass1!"):
    # Default to a UNIQUE org per call: registering with an existing org_name now
    # joins that workspace as an analyst (not a new owner), so tests that expect a
    # fresh owner must use a fresh org name. Tests exercising the join behaviour
    # pass an explicit shared `org`.
    email = email or _email()
    org = org or f"Org_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/auth/register",
        json={"org_name": org, "email": email, "password": password},
    )
    return resp, email


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── AC2 / AC16: registration ────────────────────────────────────────────────────


def test_ac2_register_returns_jwt_with_org_and_owner_role(client):
    resp, email = _register(client, org="Acme")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    user = body["user"]
    assert user["email"] == email
    assert user["role"] == "owner"
    assert user["org_id"]

    claims = _claims(body["token"])
    assert claims["org_id"] == user["org_id"]
    assert claims["role"] == "owner"
    assert claims["email"] == email


def test_ac16_password_hash_is_bcrypt_and_no_plaintext(client):
    password = "Supersecret-plaintext-1"
    resp, email = _register(client, password=password)
    assert resp.status_code == 201

    # No plaintext password in the response body.
    assert password not in resp.text

    con = db.connect()
    try:
        row = con.execute(
            "SELECT password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0].startswith("$2b$12$")
    assert password not in row[0]


# ── AC3: duplicate email ─────────────────────────────────────────────────────────


def test_ac3_duplicate_email_returns_409(client):
    _, email = _register(client, email=_email())
    dupe = client.post(
        "/api/auth/register",
        json={"org_name": "Other", "email": email, "password": "Anotherpass1!"},
    )
    assert dupe.status_code == 409, dupe.text


# ── AC4 / AC5: login ─────────────────────────────────────────────────────────────


def test_ac4_login_jwt_has_required_claims(client):
    _, email = _register(client, email=_email(), password="Loginpass1!")
    resp = client.post(
        "/api/auth/login", json={"email": email, "password": "Loginpass1!"}
    )
    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["token"])
    for field in ("sub", "org_id", "role", "jti", "iat", "exp"):
        assert field in claims, f"missing claim {field}"
    assert claims["role"] == "owner"


def test_ac5_wrong_password_and_unknown_email_identical_401(client):
    _, email = _register(client, email=_email(), password="Rightpass1!")

    wrong = client.post(
        "/api/auth/login", json={"email": email, "password": "wrongpass1"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": _email(), "password": "whatever1"}
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "Invalid email or password"


# ── AC6 / AC7 / AC8: rate limiting ──────────────────────────────────────────────


def test_ac6_sixth_failed_attempt_returns_429_with_retry_after(client):
    _, email = _register(client, email=_email(), password="Correctpass1!")

    for _ in range(5):
        bad = client.post(
            "/api/auth/login", json={"email": email, "password": "badpass1"}
        )
        assert bad.status_code == 401

    throttled = client.post(
        "/api/auth/login", json={"email": email, "password": "Correctpass1!"}
    )
    assert throttled.status_code == 429, throttled.text
    # Retry-After header present and within the window.
    retry_after = int(throttled.headers["retry-after"])
    assert 0 < retry_after <= 900
    # Body carries retry_after too (CORS can't expose the header to the SPA).
    assert throttled.json()["detail"]["retry_after"] == retry_after


def test_ac7_rate_limit_is_email_scoped_not_ip(client):
    """A throttled account does not lock out another user on the same client/IP
    (deliberate deviation from per-IP AC7)."""
    _, blocked = _register(client, org="Blocked", email=_email(), password="Blockedpass1!")
    _, other = _register(client, org="Other", email=_email(), password="Otherpass1!")

    for _ in range(5):
        client.post("/api/auth/login", json={"email": blocked, "password": "wrong1"})
    assert (
        client.post("/api/auth/login", json={"email": blocked, "password": "Blockedpass1!"}).status_code
        == 429
    )

    # Same TestClient (same IP) — the other account logs in fine.
    ok = client.post("/api/auth/login", json={"email": other, "password": "Otherpass1!"})
    assert ok.status_code == 200, ok.text


def test_ac8_successful_login_clears_failed_attempts(client):
    _, email = _register(client, email=_email(), password="Recoverpass1!")

    # 4 failures — under the threshold of 5.
    for _ in range(4):
        client.post("/api/auth/login", json={"email": email, "password": "wrong1"})

    # A correct login still succeeds and resets the counter…
    assert (
        client.post("/api/auth/login", json={"email": email, "password": "Recoverpass1!"}).status_code
        == 200
    )
    # …so four more failures don't immediately trip the limit (count was reset).
    for _ in range(4):
        client.post("/api/auth/login", json={"email": email, "password": "wrong1"})
    assert (
        client.post("/api/auth/login", json={"email": email, "password": "Recoverpass1!"}).status_code
        == 200
    )


# ── AC9: logout revokes the token across protected routes ────────────────────────


def test_ac9_logout_revokes_token_on_protected_route(client):
    resp, _ = _register(client, email=_email())
    token = resp.json()["token"]

    # Token works on a protected existing route before logout.
    assert client.get(WORKSPACE_CATALOG, headers=_auth(token)).status_code == 200

    assert client.post("/api/auth/logout", headers=_auth(token)).status_code == 204

    # After logout the same token is rejected (jti blocklist, enforced by the
    # tenancy middleware on every authenticated request).
    assert client.get(WORKSPACE_CATALOG, headers=_auth(token)).status_code == 401


# ── AC10: invite token environment gating ────────────────────────────────────────


def test_ac10_invite_returns_token_in_non_production(client):
    resp, _ = _register(client, email=_email())
    owner_token = resp.json()["token"]
    invite = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )
    assert invite.status_code == 201, invite.text
    assert invite.json()["invite_token"]


def test_ac10_invite_returns_501_in_production(client, monkeypatch):
    # A signing secret is required once ENVIRONMENT=production.
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    resp, _ = _register(client, email=_email())
    owner_token = resp.json()["token"]

    monkeypatch.setenv("ENVIRONMENT", "production")
    invite = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )
    assert invite.status_code == 501, invite.text


# ── AC11 / AC12: accept-invite single-use + expiry ───────────────────────────────


def test_ac11_accept_invite_single_use(client):
    resp, _ = _register(client, email=_email())
    owner_token = resp.json()["token"]
    invite_token = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    ).json()["invite_token"]

    first = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "Analystpass1!"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "Analystpass1!"},
    )
    assert second.status_code == 400, second.text


def test_ac12_accept_invite_expired_returns_400(client):
    resp, _ = _register(client, email=_email())
    owner_token = resp.json()["token"]
    invite_token = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "viewer"},
    ).json()["invite_token"]

    # Age the stored invite past its 72h expiry.
    key = f"{_INVITE_KV_PREFIX}:{_token_hash(invite_token)}"
    entry = db.kv_get(key)
    entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.kv_set(key, entry)

    expired = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "viewerpass1"},
    )
    assert expired.status_code == 400, expired.text


# ── AC15: the AUTH-1 JWT works across existing protected routes ──────────────────


def test_ac15_register_jwt_passes_require_auth_and_role(client):
    resp, _ = _register(client, email=_email())
    token = resp.json()["token"]
    catalog = client.get(WORKSPACE_CATALOG, headers=_auth(token))
    assert catalog.status_code == 200, catalog.text


def test_ac15_login_jwt_passes_require_auth_and_role(client):
    _, email = _register(client, email=_email(), password="E2epass1!")
    token = client.post(
        "/api/auth/login", json={"email": email, "password": "E2epass1!"}
    ).json()["token"]
    catalog = client.get(WORKSPACE_CATALOG, headers=_auth(token))
    assert catalog.status_code == 200, catalog.text


def test_ac15_unauthenticated_request_is_401(client):
    # Guard against the additive require_auth change weakening the gate.
    assert client.get(WORKSPACE_CATALOG).status_code == 401
    assert client.get(WORKSPACE_CATALOG, headers=_auth("not-a-real-token")).status_code == 401
