"""Registration bug fixes (registration_fixes branch).

BUG 1 — the organisation name must correspond to the company email domain.
BUG 2 — every registration (including a subsequent one that JOINS a deduped org)
        must trigger the admin approval email; the org-name dedup/join path used
        to skip it.

Both fixes preserve the existing multi-tenancy design: org-name dedup, shared
org_id, the unique normalised-name constraint, and the per-ORG approval model are
all unchanged.

FAKE CREDENTIALS: all passwords/emails below are non-real, test-only values.
"""
from __future__ import annotations

import uuid

import pytest

import app.email_service as email_service
from app import db
from app.auth.user_auth import org_name_matches_email_domain

from auth_helpers import activate_org_by_email, email_for_org, rand_org_name

_PW = "Supersecret1!"


# ---------------------------------------------------------------------------
# BUG 1 — org name vs email domain (pure rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "org,email,expected",
    [
        ("Google", "user@google.com", True),
        ("google", "USER@GOOGLE.COM", True),          # case-insensitive
        ("Google", "user@mail.google.com", True),      # common sub-domain
        ("Google", "user@google.co.uk", True),         # cc-TLD (second-level)
        ("IBM", "user@ibm.com", True),
        ("Google", "user@microsoft.com", False),
        ("IBM", "user@oracle.com", False),
        ("Google", "not-an-email", False),             # malformed
        ("Google", "user@google", False),              # no TLD label
    ],
)
def test_org_name_matches_email_domain_rule(org, email, expected):
    assert org_name_matches_email_domain(org, email) is expected


# ---------------------------------------------------------------------------
# BUG 1 — enforced at the API boundary (backend source of truth)
# ---------------------------------------------------------------------------


def test_register_rejects_org_domain_mismatch(client):
    org = rand_org_name("Google")
    resp = client.post(
        "/api/auth/register",
        json={"org_name": org, "email": f"abc@microsoft.com", "password": _PW},
    )
    assert resp.status_code == 400, resp.text
    assert "email domain" in resp.json()["detail"].lower()


def test_register_accepts_matching_org_domain(client):
    org = rand_org_name("Acme")
    resp = client.post(
        "/api/auth/register",
        json={"org_name": org, "email": email_for_org(org), "password": _PW},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# BUG 2 — approval email on the dedup/join path (subsequent registrations)
# ---------------------------------------------------------------------------


def _capture_approval_emails(monkeypatch) -> list[dict]:
    """Patch the approval-request email at its source (both the new-org and join
    paths import it from app.email_service at call time)."""
    calls: list[dict] = []

    def _fake(*, admin_email, org_name, registrant_email, approval_token, org_id):
        calls.append(
            {
                "admin_email": admin_email,
                "org_name": org_name,
                "registrant_email": registrant_email,
                "org_id": org_id,
            }
        )
        return True

    monkeypatch.setattr(email_service, "send_org_approval_request_email", _fake)
    return calls


def test_first_and_subsequent_registration_both_send_approval_email(client, monkeypatch):
    calls = _capture_approval_emails(monkeypatch)

    org = rand_org_name("Shared")
    first = email_for_org(org)
    second = email_for_org(org)  # SAME org/domain, different local part

    r1 = client.post(
        "/api/auth/register",
        json={"org_name": org, "email": first, "password": _PW},
    )
    assert r1.status_code == 201, r1.text
    # First registration → new org → approval email.
    assert len(calls) == 1, "first registration must send the approval email"

    r2 = client.post(
        "/api/auth/register",
        json={"org_name": org, "email": second, "password": _PW},
    )
    assert r2.status_code == 201, r2.text
    # BUG 2 fix: the subsequent registration JOINS the deduped org and must ALSO
    # send the approval email.
    assert len(calls) == 2, "subsequent registration must also send the approval email"
    assert calls[1]["registrant_email"] == second


def test_join_preserves_dedup_and_shared_org_id(client, monkeypatch):
    _capture_approval_emails(monkeypatch)

    org = rand_org_name("Dedup")
    e1, e2 = email_for_org(org), email_for_org(org)
    assert client.post(
        "/api/auth/register", json={"org_name": org, "email": e1, "password": _PW}
    ).status_code == 201
    assert client.post(
        "/api/auth/register", json={"org_name": org, "email": e2, "password": _PW}
    ).status_code == 201

    # Exactly ONE org for the normalised name (dedup preserved, unique constraint intact).
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id FROM orgs WHERE name_normalised = %s", (org.strip().lower(),)
        )
        org_rows = cur.fetchall()
        cur.execute(
            "SELECT DISTINCT wm.org_id FROM workspace_members wm "
            "JOIN users u ON u.id = wm.user_id "
            "WHERE u.email IN (%s, %s) AND wm.is_deleted = FALSE",
            (e1, e2),
        )
        member_org_ids = [r[0] for r in cur.fetchall()]
    finally:
        con.close()

    assert len(org_rows) == 1, "dedup must keep a single org for the name"
    # Both registrants share the SAME org_id (shared-org_id behaviour preserved).
    assert len(member_org_ids) == 1
    assert member_org_ids[0] == org_rows[0][0]


def test_subsequent_registration_sends_email_even_after_org_approved(client, monkeypatch):
    """EVERY successful registration sends the approval email — including a join
    into an ALREADY-APPROVED (active) org — and doing so must NOT flip the org's
    settled approval state."""
    calls = _capture_approval_emails(monkeypatch)
    org = rand_org_name("Statekeep")
    e1, e2 = email_for_org(org), email_for_org(org)
    client.post("/api/auth/register", json={"org_name": org, "email": e1, "password": _PW})
    assert len(calls) == 1  # first registration emailed

    # Approve the org (admin step, simulated), THEN another user joins.
    activate_org_by_email(e1)
    r2 = client.post("/api/auth/register", json={"org_name": org, "email": e2, "password": _PW})
    assert r2.status_code == 201, r2.text
    # The subsequent registration ALSO sends the approval email, unconditionally.
    assert len(calls) == 2

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT approval_status FROM orgs WHERE name_normalised = %s",
            (org.strip().lower(),),
        )
        status = cur.fetchone()[0]
    finally:
        con.close()
    # Sending the email must not reset a settled (active) org back to pending.
    assert status == "active"
