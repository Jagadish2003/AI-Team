"""Contract test — an analyst invited INTO the run's org sees the org's run.

Reproduces the reported scenario the correct way: the owner who created the
workspace + run invites a teammate, so the teammate joins THAT org (not a fresh
one). Expectations:
  * invited analyst's JWT carries the inviting org's org_id
  * GET /api/runs lists the org's run for the analyst (role >= viewer)
  * GET /api/runs/{id} returns 200 for the analyst
  * GET /api/connectors/salesforce/products shows the owner's declaration

This is the flip side of the isolation tests: members of the SAME org share the
workspace; only cross-org access is denied.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_invited_analyst_sees_org_run_and_products(client: TestClient):
    from app import db

    # 1. Owner registers org "DWP".
    reg = client.post(
        "/api/auth/register",
        json={"org_name": "DWP", "email": "owner_dwp@example.com", "password": "password123"},
    )
    assert reg.status_code == 201, reg.text
    owner = reg.json()
    owner_token = owner["token"]
    owner_org = owner["user"]["org_id"]

    # 2. A run owned by DWP (tagged with the org, as the launch/runner would).
    db.upsert_run(
        "run_dwp_shared",
        {"id": "run_dwp_shared", "org_id": owner_org, "status": "done",
         "startedAt": "2026-06-10T10:00:00Z"},
    )
    # ...and a Salesforce product declaration for DWP.
    db.org_connector_set(
        owner_org,
        "salesforce",
        {"id": "salesforce", "name": "Salesforce", "status": "connected",
         "products": ["salesforce_ncino"]},
    )

    # 3. Owner invites an analyst INTO DWP.
    inv = client.post(
        "/api/auth/invite",
        headers=_bearer(owner_token),
        json={"email": "analyst_dwp@example.com", "role": "analyst"},
    )
    assert inv.status_code == 201, inv.text
    invite_token = inv.json()["invite_token"]

    # 4. Analyst accepts → JWT for the SAME org, role analyst.
    acc = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": invite_token, "password": "password123"},
    )
    assert acc.status_code == 200, acc.text
    analyst = acc.json()
    analyst_token = analyst["token"]
    assert analyst["user"]["org_id"] == owner_org, "analyst must join the inviter's org"
    assert analyst["user"]["role"] == "analyst"

    # 5. Analyst sees the org's run in the list…
    runs = client.get("/api/runs", headers=_bearer(analyst_token))
    assert runs.status_code == 200, runs.text
    assert "run_dwp_shared" in [r["id"] for r in runs.json()]

    # …and can open it directly.
    detail = client.get("/api/runs/run_dwp_shared", headers=_bearer(analyst_token))
    assert detail.status_code == 200, detail.text

    # 6. Analyst sees the owner's Salesforce product declaration.
    prods = client.get(
        "/api/connectors/salesforce/products", headers=_bearer(analyst_token)
    )
    assert prods.status_code == 200, prods.text
    assert "salesforce_ncino" in prods.json()["products"]


def test_invited_analyst_sees_connectors_via_real_endpoints(client: TestClient):
    """Full-fidelity reproduction of the reported flow through the REAL routes.

    Owner connects + configures Salesforce and declares products using the owner
    JWT, creates a run in the org, then invites an analyst. The analyst — using
    their OWN accepted-invite JWT — must see those connectors connected/configured
    and the run, because they are in the same org.
    """
    from app import db

    reg = client.post(
        "/api/auth/register",
        json={"org_name": "DWP3", "email": "owner_dwp3@example.com", "password": "password123"},
    )
    assert reg.status_code == 201, reg.text
    owner = reg.json()
    owner_token = owner["token"]
    owner_org = owner["user"]["org_id"]

    # Owner connects + configures Salesforce through the real endpoints.
    assert client.post(
        "/api/connectors/salesforce/connect",
        headers=_bearer(owner_token),
        json={"status": "connected"},
    ).status_code == 200
    assert client.post(
        "/api/connectors/salesforce/configure", headers=_bearer(owner_token)
    ).status_code == 200
    assert client.patch(
        "/api/connectors/salesforce/products",
        headers=_bearer(owner_token),
        json={"products": ["salesforce_ncino"]},
    ).status_code == 200

    # A run in the owner's org (as the launch/runner tags it).
    db.upsert_run(
        "run_dwp3",
        {"id": "run_dwp3", "org_id": owner_org, "status": "done",
         "startedAt": "2026-06-10T12:00:00Z"},
    )

    # Owner invites an analyst INTO this org.
    inv = client.post(
        "/api/auth/invite",
        headers=_bearer(owner_token),
        json={"email": "analyst_dwp3@example.com", "role": "analyst"},
    )
    assert inv.status_code == 201, inv.text
    acc = client.post(
        "/api/auth/accept-invite",
        json={"invite_token": inv.json()["invite_token"], "password": "password123"},
    )
    assert acc.status_code == 200, acc.text
    analyst_token = acc.json()["token"]

    # The analyst sees the SAME connection state the owner created.
    conns = client.get("/api/connectors", headers=_bearer(analyst_token))
    assert conns.status_code == 200, conns.text
    sf = next((c for c in conns.json() if c.get("id") == "salesforce"), None)
    assert sf is not None
    assert sf.get("status") == "connected", "invited analyst must see Salesforce connected"
    assert sf.get("configured") is True, "invited analyst must see Salesforce configured"

    prods = client.get(
        "/api/connectors/salesforce/products", headers=_bearer(analyst_token)
    )
    assert prods.status_code == 200
    assert "salesforce_ncino" in prods.json()["products"]

    runs = client.get("/api/runs", headers=_bearer(analyst_token))
    assert runs.status_code == 200
    assert "run_dwp3" in [r["id"] for r in runs.json()]


def test_outsider_in_another_org_does_not_see_the_run(client: TestClient):
    from app import db

    reg = client.post(
        "/api/auth/register",
        json={"org_name": "DWP2", "email": "owner_dwp2@example.com", "password": "password123"},
    )
    owner_org = reg.json()["user"]["org_id"]
    db.upsert_run(
        "run_dwp2_only",
        {"id": "run_dwp2_only", "org_id": owner_org, "status": "done",
         "startedAt": "2026-06-10T11:00:00Z"},
    )

    # A separate owner / separate org (mirrors "registered a new account to invite").
    other = client.post(
        "/api/auth/register",
        json={"org_name": "CF", "email": "owner_cf@example.com", "password": "password123"},
    )
    other_token = other.json()["token"]

    runs = client.get("/api/runs", headers=_bearer(other_token))
    assert runs.status_code == 200
    assert "run_dwp2_only" not in [r["id"] for r in runs.json()]
    assert client.get(
        "/api/runs/run_dwp2_only", headers=_bearer(other_token)
    ).status_code == 404
