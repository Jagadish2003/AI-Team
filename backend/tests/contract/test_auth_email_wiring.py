"""CS-3 (T7) — email wiring into the register / invite auth routes.

Covers the acceptance criteria owned by this task:
  AC10 — POST /api/auth/register sends a welcome email; POST /api/auth/invite
         sends an invitation email with an accept-invite link.
  AC14 — email_service never breaks the auth flow: a send that returns False (or
         even raises) still yields the route's normal response, and the failure
         is not surfaced to the caller.
  CS-3 Section 4 — non-production includes the raw invite_token plus email_sent;
         production omits the raw token.

These patch the send_* helpers as imported into app.routes_auth (the call site),
so they assert the wiring without depending on a live mail provider.
"""
from __future__ import annotations

import uuid

import pytest

import app.routes_auth as routes_auth


# ── helpers ──────────────────────────────────────────────────────────────────


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


def _register_owner(client, email=None):
    email = email or _email()
    resp = client.post(
        "/api/auth/register",
        json={
            "org_name": f"Org_{uuid.uuid4().hex[:8]}",
            "email": email,
            "password": "Ownerpass1!",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"], email


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── AC10: welcome email on register ───────────────────────────────────────────


def test_register_sends_welcome_email(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes_auth, "send_welcome_email",
        lambda to, org_name: calls.append((to, org_name)) or True,
    )

    email = _email()
    resp = client.post(
        "/api/auth/register",
        json={"org_name": f"Org_{uuid.uuid4().hex[:8]}", "email": email, "password": "Ownerpass1!"},
    )

    assert resp.status_code == 201, resp.text
    assert len(calls) == 1
    sent_to, org_name = calls[0]
    assert sent_to == email
    assert org_name  # the org's display name is passed through


def test_register_still_succeeds_when_welcome_email_fails(client, monkeypatch):
    """AC14: a welcome-email failure must not break registration."""
    def _boom(to, org_name):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(routes_auth, "send_welcome_email", _boom)

    resp = client.post(
        "/api/auth/register",
        json={"org_name": f"Org_{uuid.uuid4().hex[:8]}", "email": _email(), "password": "Ownerpass1!"},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["token"]  # normal auth response is returned regardless


# ── AC10: invite email on invite ──────────────────────────────────────────────


def test_invite_sends_invite_email_with_token_and_role(client, monkeypatch):
    captured = {}
    def _capture(to, invite_token, org_name, role):
        captured.update(to=to, invite_token=invite_token, org_name=org_name, role=role)
        return True

    monkeypatch.setattr(routes_auth, "send_invite_email", _capture)

    owner_token, _ = _register_owner(client)
    invitee = _email()
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": invitee, "role": "viewer"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email_sent"] is True
    # Non-production: raw token is returned AND it is the same token handed to the
    # email helper (so the emailed accept-invite link matches the testing token).
    assert body["invite_token"] == captured["invite_token"]
    assert captured["to"] == invitee
    assert captured["role"] == "viewer"
    assert captured["org_name"]


def test_invite_reports_email_sent_false_when_delivery_fails(client, monkeypatch):
    """AC14: invite still returns 201 when the email fails; email_sent is False."""
    monkeypatch.setattr(routes_auth, "send_invite_email", lambda *a, **k: False)

    owner_token, _ = _register_owner(client)
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email_sent"] is False
    # A token is still issued in non-production so the invite remains usable.
    assert body["invite_token"]


def test_invite_still_succeeds_when_email_raises(client, monkeypatch):
    """AC14: even an unexpected exception in the send path cannot break invite."""
    def _boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(routes_auth, "send_invite_email", _boom)

    owner_token, _ = _register_owner(client)
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["email_sent"] is False


def test_invite_token_still_usable_end_to_end(client, monkeypatch):
    """The non-production token returned alongside email_sent still activates the
    account — wiring the email in did not change the accept-invite contract."""
    monkeypatch.setattr(routes_auth, "send_invite_email", lambda *a, **k: True)

    owner_token, _ = _register_owner(client)
    invite = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": _email(), "role": "analyst"},
    )
    token = invite.json()["invite_token"]

    accept = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": token, "password": "Analystpass1!"},
    )
    assert accept.status_code == 200, accept.text
    assert accept.json()["user"]["role"] == "analyst"


# ── CS-3 Section 4: production hides the raw token ────────────────────────────


def test_invite_omits_token_in_production_but_sends_email(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        routes_auth, "send_invite_email",
        lambda to, *a, **k: sent.append(to) or True,
    )
    monkeypatch.setenv("JWT_SECRET", "x" * 40)

    owner_token, _ = _register_owner(client)

    monkeypatch.setenv("ENVIRONMENT", "production")
    invitee = _email()
    resp = client.post(
        "/api/auth/invite",
        headers=_auth(owner_token),
        json={"email": invitee, "role": "analyst"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "invite_token" not in body          # never returned in production
    assert body["email_sent"] is True
    assert sent == [invitee]                    # but the email is still sent
