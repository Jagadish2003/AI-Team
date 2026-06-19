"""AUTH-2 T8 — end-to-end contract tests for the org registration approval flow.

Covers the full state machine: registration creates a pending org with no JWT;
login is blocked (403, distinct error codes) while pending or rejected; an
emailed approve/reject link drives the transition; the token is single-use,
7-day-expiring, and scoped to its own org.

How the raw approval token reaches the test: registration never returns it (it
goes only to the admin email). The test spies on
``email_service.send_org_approval_request_email`` to capture the
``approval_token``/``org_id`` that the registration flow generates — the same
values an admin would receive in the email — then drives the GET approve/reject
links with them.

These are PostgreSQL-backed contract tests (see tests/contract/conftest.py): the
shared session ``client`` and ``db.connect()`` both run against the test
database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.routes_auth import _get_org_approval_row

PASSWORD = "Supersecret1!"
APPROVE_URL = "/api/auth/org-approval/approve"
REJECT_URL = "/api/auth/org-approval/reject"


@pytest.fixture()
def register_org(client: TestClient, monkeypatch):
    """Return a helper that registers an org and exposes its approval token.

    Spies on the three AUTH-2 emails:
      * send_org_approval_request_email — captures the per-registration
        approval_token + org_id (otherwise only visible in the admin email),
      * send_org_approved_email / send_org_rejected_email — recorded so a test
        can assert the registrant was notified (and so the test does not emit
        real SMTP error logs when no mail server is configured).
    """
    import app.email_service as email_service
    import app.routes_auth as routes_auth

    requests: list[dict] = []
    approved: list[dict] = []
    rejected: list[dict] = []

    def _spy_request(*, admin_email, org_name, registrant_email, approval_token, org_id):
        requests.append(
            {
                "admin_email": admin_email,
                "org_name": org_name,
                "registrant_email": registrant_email,
                "approval_token": approval_token,
                "org_id": org_id,
            }
        )
        return True

    def _spy_approved(*, registrant_email, org_name):
        approved.append({"registrant_email": registrant_email, "org_name": org_name})
        return True

    def _spy_rejected(*, registrant_email, org_name):
        rejected.append({"registrant_email": registrant_email, "org_name": org_name})
        return True

    # register_org_and_owner imports send_org_approval_request_email locally from
    # app.email_service at call time, so patching the module attribute is picked
    # up. The two outcome emails are bound at import time in routes_auth, so they
    # are patched there.
    monkeypatch.setattr(email_service, "send_org_approval_request_email", _spy_request)
    monkeypatch.setattr(routes_auth, "send_org_approved_email", _spy_approved)
    monkeypatch.setattr(routes_auth, "send_org_rejected_email", _spy_rejected)

    def _do(org_name: str | None = None, email: str | None = None, password: str = PASSWORD):
        org_name = org_name or f"Org {uuid.uuid4().hex[:8]}"
        email = email or f"owner_{uuid.uuid4().hex[:10]}@example.com"
        before = len(requests)
        response = client.post(
            "/api/auth/register",
            json={"org_name": org_name, "email": email, "password": password},
        )
        captured = requests[-1] if len(requests) > before else {}
        return {
            "response": response,
            "org_name": org_name,
            "email": email,
            "password": password,
            "org_id": captured.get("org_id"),
            "approval_token": captured.get("approval_token"),
            "admin_email": captured.get("admin_email"),
            "registrant_email": captured.get("registrant_email"),
            "approved_emails": approved,
            "rejected_emails": rejected,
        }

    return _do


def _login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _set_token_expiry(org_id: str, expires_at: datetime) -> None:
    con = db.connect()
    try:
        con.execute(
            "UPDATE orgs SET approval_token_expires_at = %s WHERE id = %s",
            (expires_at, org_id),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# T8-AC1 — registration creates a pending org with no JWT
# ---------------------------------------------------------------------------

def test_register_creates_pending_org_with_no_jwt(register_org):
    r = register_org()
    resp = r["response"]
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["status"] == "pending_approval"
    # No usable session is issued at registration.
    assert "token" not in body
    assert "access_token" not in body
    assert "user" not in body

    # The org is persisted in pending_approval state.
    row = _get_org_approval_row(r["org_id"])
    assert row is not None
    assert row["approval_status"] == "pending_approval"

    # The admin was emailed an approval request for this org.
    assert r["admin_email"]
    assert r["approval_token"]


# ---------------------------------------------------------------------------
# T8-AC2 — pending org login → 403 with distinct error code
# ---------------------------------------------------------------------------

def test_login_pending_org_returns_403_pending_code(register_org, client):
    r = register_org()
    assert r["response"].status_code == 201

    resp = _login(client, r["email"])
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "org_pending_approval"


# ---------------------------------------------------------------------------
# T8-AC3 — rejected org login → 403 with code distinct from the pending case
# ---------------------------------------------------------------------------

def test_login_rejected_org_returns_403_distinct_code(register_org, client):
    r = register_org()
    rej = client.get(REJECT_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert rej.status_code == 200, rej.text

    resp = _login(client, r["email"])
    assert resp.status_code == 403, resp.text
    code = resp.json()["detail"]["error_code"]
    assert code == "org_rejected"
    # Must be a different code from the pending case (T8-AC3).
    assert code != "org_pending_approval"


# ---------------------------------------------------------------------------
# T8-AC4 — approve activates the org and a subsequent login returns a JWT
# ---------------------------------------------------------------------------

def test_approve_activates_org_and_login_returns_jwt(register_org, client):
    r = register_org()

    ap = client.get(APPROVE_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert ap.status_code == 200, ap.text
    assert "approved" in ap.text.lower()

    row = _get_org_approval_row(r["org_id"])
    assert row["approval_status"] == "active"
    # Token is single-use: the hash is cleared on approval.
    assert row["approval_token_hash"] is None

    # The registrant was notified of approval.
    assert any(e["registrant_email"] == r["email"] for e in r["approved_emails"])

    # Login now succeeds and returns a JWT exactly as in AUTH-1.
    resp = _login(client, r["email"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("token")
    assert body["user"]["org_id"] == r["org_id"]
    assert body["user"]["role"] == "owner"


# ---------------------------------------------------------------------------
# T8-AC5 — reject sets rejected and login is permanently blocked
# ---------------------------------------------------------------------------

def test_reject_blocks_login_permanently(register_org, client):
    r = register_org()

    rej = client.get(REJECT_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert rej.status_code == 200, rej.text

    row = _get_org_approval_row(r["org_id"])
    assert row["approval_status"] == "rejected"
    assert row["approval_token_hash"] is None
    assert any(e["registrant_email"] == r["email"] for e in r["rejected_emails"])

    # Login stays blocked with the rejected code (no reactivation path).
    resp = _login(client, r["email"])
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "org_rejected"


# ---------------------------------------------------------------------------
# T8-AC6 — an expired token is rejected (400) and the org state is unchanged
# ---------------------------------------------------------------------------

def test_expired_token_returns_400_and_leaves_state_unchanged(register_org, client):
    r = register_org()

    # Force the token to have expired (registration sets a 7-day window).
    _set_token_expiry(r["org_id"], datetime.now(timezone.utc) - timedelta(days=1))

    ap = client.get(APPROVE_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert ap.status_code == 400, ap.text
    assert "expired" in ap.text.lower()

    # State unchanged: still pending, token not consumed.
    row = _get_org_approval_row(r["org_id"])
    assert row["approval_status"] == "pending_approval"
    assert row["approval_token_hash"] is not None

    # And the org still cannot log in.
    assert _login(client, r["email"]).status_code == 403


# ---------------------------------------------------------------------------
# T8-AC7 — double-process is safe (second call is a no-op "already processed")
# ---------------------------------------------------------------------------

def test_second_approve_is_already_processed_and_no_state_change(register_org, client):
    r = register_org()

    first = client.get(APPROVE_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert first.status_code == 200
    assert _get_org_approval_row(r["org_id"])["approval_status"] == "active"

    # Second click of the same (now-consumed) link must not re-process.
    second = client.get(APPROVE_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert second.status_code == 200
    assert "already" in second.text.lower()
    assert _get_org_approval_row(r["org_id"])["approval_status"] == "active"


def test_reject_after_approve_cannot_flip_state(register_org, client):
    """An already-approved org cannot be rejected with the consumed link."""
    r = register_org()

    assert client.get(
        APPROVE_URL, params={"token": r["approval_token"], "org_id": r["org_id"]}
    ).status_code == 200
    assert _get_org_approval_row(r["org_id"])["approval_status"] == "active"

    flip = client.get(REJECT_URL, params={"token": r["approval_token"], "org_id": r["org_id"]})
    assert flip.status_code == 200
    assert "already" in flip.text.lower()
    # Still active — the consumed token cannot move an active org to rejected.
    assert _get_org_approval_row(r["org_id"])["approval_status"] == "active"


# ---------------------------------------------------------------------------
# T8-AC8 — cross-org isolation: one org's token cannot act on another org
# ---------------------------------------------------------------------------

def test_cross_org_token_cannot_approve_or_reject_another_org(register_org, client):
    a = register_org()
    b = register_org()
    assert a["org_id"] and b["org_id"] and a["org_id"] != b["org_id"]

    # A's token against B's org_id — hashes do not match → invalid link, no change.
    resp = client.get(APPROVE_URL, params={"token": a["approval_token"], "org_id": b["org_id"]})
    assert resp.status_code == 400, resp.text
    assert "invalid" in resp.text.lower()
    assert _get_org_approval_row(a["org_id"])["approval_status"] == "pending_approval"
    assert _get_org_approval_row(b["org_id"])["approval_status"] == "pending_approval"

    # And the same isolation for reject (B's token against A's org_id).
    resp2 = client.get(REJECT_URL, params={"token": b["approval_token"], "org_id": a["org_id"]})
    assert resp2.status_code == 400, resp2.text
    assert "invalid" in resp2.text.lower()
    assert _get_org_approval_row(a["org_id"])["approval_status"] == "pending_approval"
    assert _get_org_approval_row(b["org_id"])["approval_status"] == "pending_approval"

    # Each org can still be actioned by ITS OWN token afterwards.
    assert client.get(
        APPROVE_URL, params={"token": a["approval_token"], "org_id": a["org_id"]}
    ).status_code == 200
    assert _get_org_approval_row(a["org_id"])["approval_status"] == "active"
