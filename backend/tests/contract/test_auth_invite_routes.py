"""Route-level contract tests for the invite / accept-invite flow — AUTH-1.

These exercise the HTTP endpoints (routes_auth.py) end to end via TestClient,
complementing test_user_auth.py which covers the logic layer directly.

Focus: the accept-invite request body. AUTH-1 Section 4 and the frontend
(authApi.acceptInvite) both send `invite_token`; the endpoint must accept that
name (a previous mismatch had it requiring `token`, which 422'd every real
frontend call). `token` is still accepted as a backward-compatible alias.
"""
from __future__ import annotations

import uuid


def _email() -> str:
    return f"invitee_{uuid.uuid4().hex[:10]}@example.com"


def _register_owner(client) -> str:
    """Register a fresh org + owner and return the owner's JWT."""
    resp = client.post(
        "/api/auth/register",
        json={
            "org_name": f"Org {uuid.uuid4().hex[:6]}",
            "email": _email(),
            "password": "ownerpass1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _create_invite(client, owner_token: str, role: str = "analyst") -> str:
    resp = client.post(
        "/api/auth/invite",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"email": _email(), "role": role},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["invite_token"]


def test_accept_invite_accepts_invite_token_field(client):
    """The canonical `invite_token` body field (spec + frontend) is accepted."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    resp = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "analystpass1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"]
    assert body["user"]["role"] == "analyst"


def test_accept_invite_still_accepts_legacy_token_field(client):
    """The legacy `token` field name keeps working via the alias."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    resp = client.post(
        "/api/auth/accept-invite",
        json={"token": invite_token, "password": "analystpass1"},
    )
    assert resp.status_code == 200, resp.text


def test_accept_invite_is_single_use(client):
    """AC11: a second accept with the same token returns 400."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    first = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "analystpass1"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "analystpass1"},
    )
    assert second.status_code == 400, second.text


def test_accept_invite_missing_token_is_422(client):
    """Neither `invite_token` nor `token` present → validation error, not 500."""
    resp = client.post(
        "/api/auth/accept-invite",
        json={"password": "analystpass1"},
    )
    assert resp.status_code == 422, resp.text


def test_invite_info_resolves_org_without_consuming_token(client):
    """GET /invite-info returns the org name and does NOT mark the token used."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    info = client.get(f"/api/auth/invite-info?token={invite_token}")
    assert info.status_code == 200, info.text
    body = info.json()
    assert body["org_name"]  # the inviting org's display name
    assert body["role"] == "analyst"

    # Token is still usable — invite-info must not consume it.
    accept = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "analystpass1"},
    )
    assert accept.status_code == 200, accept.text


def test_invite_info_400_for_used_token(client):
    """After activation, invite-info reports the token as used (drives the page's
    'already used' state on reload)."""
    owner_token = _register_owner(client)
    invite_token = _create_invite(client, owner_token)

    client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "analystpass1"},
    )

    info = client.get(f"/api/auth/invite-info?token={invite_token}")
    assert info.status_code == 400, info.text


def test_invite_info_400_for_unknown_token(client):
    info = client.get("/api/auth/invite-info?token=not-a-real-token")
    assert info.status_code == 400, info.text
