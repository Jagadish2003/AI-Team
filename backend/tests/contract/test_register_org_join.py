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


def _email() -> str:
    return f"join_{uuid.uuid4().hex[:10]}@example.com"


def test_same_org_name_creates_distinct_orgs_both_owners(client: TestClient):
    name = f"Shared Workspace {uuid.uuid4().hex[:6]}"

    first = user_auth.register_org_and_owner(name, _email(), "supersecret1")
    second = user_auth.register_org_and_owner(name, _email(), "supersecret1")

    # A matching name must NOT join the first workspace — distinct org_ids.
    assert first["user"]["org_id"] != second["user"]["org_id"], "same name → separate orgs"
    # Each registrant owns their own workspace; neither is silently an analyst.
    assert first["user"]["role"] == "owner"
    assert second["user"]["role"] == "owner"


def test_different_org_name_creates_distinct_orgs(client: TestClient):
    a = user_auth.register_org_and_owner(f"Org A {uuid.uuid4().hex[:6]}", _email(), "supersecret1")
    b = user_auth.register_org_and_owner(f"Org B {uuid.uuid4().hex[:6]}", _email(), "supersecret1")

    assert a["user"]["org_id"] != b["user"]["org_id"], "different name → different org"
    assert a["user"]["role"] == "owner"
    assert b["user"]["role"] == "owner"


def test_case_or_whitespace_name_variant_still_isolated(client: TestClient):
    name = f"Casing {uuid.uuid4().hex[:6]}"

    first = user_auth.register_org_and_owner(name, _email(), "supersecret1")
    other = user_auth.register_org_and_owner(f"  {name.upper()}  ", _email(), "supersecret1")

    # Even a case/whitespace variant of an existing name creates its own org.
    assert other["user"]["org_id"] != first["user"]["org_id"]
    assert other["user"]["role"] == "owner"


def test_second_registration_same_name_cannot_see_first_users_run(client: TestClient):
    """The vulnerability scenario, now closed: a stranger who registers with a
    known org_name lands in a SEPARATE workspace and cannot see the first user's
    runs. Cross-org visibility requires an explicit invite, not a guessed name."""
    from app import db

    name = f"DWP {uuid.uuid4().hex[:6]}"

    a = client.post(
        "/api/auth/register",
        json={"org_name": name, "email": _email(), "password": "supersecret1"},
    )
    assert a.status_code == 201, a.text
    a_org = a.json()["user"]["org_id"]

    db.upsert_run(
        "run_join_shared",
        {"id": "run_join_shared", "org_id": a_org, "status": "done",
         "startedAt": "2026-06-10T13:00:00Z"},
    )

    b = client.post(
        "/api/auth/register",
        json={"org_name": name, "email": _email(), "password": "supersecret1"},
    )
    assert b.status_code == 201, b.text
    b_body = b.json()
    # Different workspace, and an owner of their own org — not an analyst joiner.
    assert b_body["user"]["org_id"] != a_org
    assert b_body["user"]["role"] == "owner"

    runs = client.get(
        "/api/runs", headers={"Authorization": f"Bearer {b_body['token']}"}
    )
    assert runs.status_code == 200, runs.text
    # The first user's run is NOT visible to the second org.
    assert "run_join_shared" not in [r["id"] for r in runs.json()]