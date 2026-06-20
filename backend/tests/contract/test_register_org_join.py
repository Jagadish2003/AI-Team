"""Contract tests — registration always creates a NEW, isolated workspace.

SECURITY (review #5): org_name is NOT a workspace join key. Org names are not
secret, so name-based joining let any external user who guessed a customer's org
name (e.g. "TCU", "City National") self-register into that workspace and read
its runs. Registration now always mints a fresh org_id the registrant owns;
joining an existing workspace is exclusively via the owner-gated invite flow.

These tests previously asserted the self-join behaviour; they now assert it is
gone and that two registrations with the same name are fully isolated (review
#5 + #16).
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth import user_auth
from auth_helpers import activate_org_by_email, member_for_email


def _email() -> str:
    return f"join_{uuid.uuid4().hex[:10]}@example.com"


# AUTH-2: register_org_and_owner returns a pending-approval ack (no user/org), so
# these isolation tests read each owner's membership directly via member_for_email.


def test_same_org_name_creates_distinct_orgs_both_owners(client: TestClient):
    name = f"Shared Workspace {uuid.uuid4().hex[:6]}"

    ea, eb = _email(), _email()
    user_auth.register_org_and_owner(name, ea, "Supersecret1!")
    user_auth.register_org_and_owner(name, eb, "Supersecret1!")
    org_a, role_a = member_for_email(ea)
    org_b, role_b = member_for_email(eb)

    # A matching name must NOT join the first workspace — distinct org_ids.
    assert org_a != org_b, "same name → separate orgs"
    # Each registrant owns their own workspace; neither is silently an analyst.
    assert role_a == "owner"
    assert role_b == "owner"


def test_different_org_name_creates_distinct_orgs(client: TestClient):
    ea, eb = _email(), _email()
    user_auth.register_org_and_owner(f"Org A {uuid.uuid4().hex[:6]}", ea, "Supersecret1!")
    user_auth.register_org_and_owner(f"Org B {uuid.uuid4().hex[:6]}", eb, "Supersecret1!")
    org_a, role_a = member_for_email(ea)
    org_b, role_b = member_for_email(eb)

    assert org_a != org_b, "different name → different org"
    assert role_a == "owner"
    assert role_b == "owner"


def test_case_or_whitespace_name_variant_still_isolated(client: TestClient):
    name = f"Casing {uuid.uuid4().hex[:6]}"

    ef, eo = _email(), _email()
    user_auth.register_org_and_owner(name, ef, "Supersecret1!")
    user_auth.register_org_and_owner(f"  {name.upper()}  ", eo, "Supersecret1!")
    org_first, _ = member_for_email(ef)
    org_other, role_other = member_for_email(eo)

    # Even a case/whitespace variant of an existing name creates its own org.
    assert org_other != org_first
    assert role_other == "owner"


def test_second_registration_same_name_cannot_see_first_users_run(client: TestClient):
    """The vulnerability scenario, now closed: a stranger who registers with a
    known org_name lands in a SEPARATE workspace and cannot see the first user's
    runs. Cross-org visibility requires an explicit invite, not a guessed name."""
    from app import db

    name = f"DWP {uuid.uuid4().hex[:6]}"

    email_a, email_b = _email(), _email()
    a = client.post(
        "/api/auth/register",
        json={"org_name": name, "email": email_a, "password": "Supersecret1!"},
    )
    assert a.status_code == 201, a.text
    a_org, _ = member_for_email(email_a)  # AUTH-2: org read from membership, not response

    db.upsert_run(
        "run_join_shared",
        {"id": "run_join_shared", "org_id": a_org, "status": "done",
         "startedAt": "2026-06-10T13:00:00Z"},
    )

    b = client.post(
        "/api/auth/register",
        json={"org_name": name, "email": email_b, "password": "Supersecret1!"},
    )
    assert b.status_code == 201, b.text
    b_org, b_role = member_for_email(email_b)
    # Different workspace, and an owner of their own org — not an analyst joiner.
    assert b_org != a_org
    assert b_role == "owner"

    # Approve org B and log in to get a usable token (AUTH-2).
    activate_org_by_email(email_b)
    b_token = client.post(
        "/api/auth/login", json={"email": email_b, "password": "Supersecret1!"}
    ).json()["token"]

    runs = client.get(
        "/api/runs", headers={"Authorization": f"Bearer {b_token}"}
    )
    assert runs.status_code == 200, runs.text
    # The first user's run is NOT visible to the second org.
    assert "run_join_shared" not in [r["id"] for r in runs.json()]