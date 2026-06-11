"""Contract tests — org_name is the workspace join key at registration.

Registering with a NEW org_name creates the workspace and makes the registrant
its owner; registering with an EXISTING name (case-insensitive, trimmed) joins
that same org_id as an analyst, so teammates land in the same workspace without
an invite. See register_org_and_owner.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth import user_auth


def _email() -> str:
    return f"join_{uuid.uuid4().hex[:10]}@example.com"


def test_same_org_name_joins_same_org_as_analyst(client: TestClient):
    name = f"Shared Workspace {uuid.uuid4().hex[:6]}"

    first = user_auth.register_org_and_owner(name, _email(), "supersecret1")
    second = user_auth.register_org_and_owner(name, _email(), "supersecret1")

    assert first["user"]["org_id"] == second["user"]["org_id"], "same name → same org"
    assert first["user"]["role"] == "owner"     # creator owns the workspace
    assert second["user"]["role"] == "analyst"  # joiner is an analyst


def test_different_org_name_creates_distinct_orgs(client: TestClient):
    a = user_auth.register_org_and_owner(f"Org A {uuid.uuid4().hex[:6]}", _email(), "supersecret1")
    b = user_auth.register_org_and_owner(f"Org B {uuid.uuid4().hex[:6]}", _email(), "supersecret1")

    assert a["user"]["org_id"] != b["user"]["org_id"], "different name → different org"
    assert a["user"]["role"] == "owner"
    assert b["user"]["role"] == "owner"


def test_org_name_match_is_case_and_whitespace_insensitive(client: TestClient):
    name = f"Casing {uuid.uuid4().hex[:6]}"

    first = user_auth.register_org_and_owner(name, _email(), "supersecret1")
    joined = user_auth.register_org_and_owner(f"  {name.upper()}  ", _email(), "supersecret1")

    assert joined["user"]["org_id"] == first["user"]["org_id"]
    assert joined["user"]["role"] == "analyst"


def test_second_registration_same_name_sees_first_users_run(client: TestClient):
    """The reported scenario, fixed: a teammate who self-registers with the same
    org_name lands in the same workspace and sees the run created by the first
    user — no invite required."""
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
    assert b_body["user"]["org_id"] == a_org
    assert b_body["user"]["role"] == "analyst"

    runs = client.get(
        "/api/runs", headers={"Authorization": f"Bearer {b_body['token']}"}
    )
    assert runs.status_code == 200, runs.text
    assert "run_join_shared" in [r["id"] for r in runs.json()]
