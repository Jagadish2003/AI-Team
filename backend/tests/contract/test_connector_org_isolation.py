"""Contract tests — connector workspace isolation (per-org connection state).

Before this, connectors were one global row per connector_id with no org_id, and
connect() mutated that shared row. So once any org connected Salesforce, every
other org saw it connected. Now connection state is per-org: connect/configure/
products write a namespaced row (f"{org_id}::{connector_id}") tagged org_id, and
listing overlays the shared catalog with the current org's overrides.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer dev-token-change-me"}


# ---------------------------------------------------------------------------
# db helpers (unit) — the isolation primitives
# ---------------------------------------------------------------------------


def test_org_connector_set_does_not_mutate_catalog_or_other_orgs():
    from app import db

    # Seed a shared catalog row (no org_id) — the disconnected default.
    db.upsert(
        "connectors",
        "salesforce",
        {"id": "salesforce", "name": "Salesforce", "status": "disconnected"},
    )

    # org_A connects salesforce.
    db.org_connector_set(
        "org_A", "salesforce", {"id": "salesforce", "name": "Salesforce", "status": "connected"}
    )

    # Catalog row is untouched.
    catalog = db.get_one("connectors", "salesforce")
    assert catalog["status"] == "disconnected"
    assert "org_id" not in catalog

    # org_A sees connected; org_B sees the catalog default (disconnected).
    assert db.org_connector_get("org_A", "salesforce")["status"] == "connected"
    assert db.org_connector_get("org_B", "salesforce")["status"] == "disconnected"


def test_org_connectors_list_overlays_catalog_with_org_state():
    from app import db

    db.upsert(
        "connectors", "jira", {"id": "jira", "name": "Jira", "status": "disconnected"}
    )
    db.org_connector_set(
        "org_X", "jira", {"id": "jira", "name": "Jira", "status": "connected"}
    )

    by_id_x = {c["id"]: c for c in db.org_connectors_list("org_X")}
    by_id_y = {c["id"]: c for c in db.org_connectors_list("org_Y")}

    assert by_id_x["jira"]["status"] == "connected"   # org_X's own state
    assert by_id_y["jira"]["status"] == "disconnected"  # org_Y sees catalog default
    # No duplicate jira rows leak into the list.
    assert sum(1 for c in db.org_connectors_list("org_X") if c["id"] == "jira") == 1


def test_org_connector_get_unknown_returns_none():
    from app import db

    assert db.org_connector_get("org_A", "does_not_exist_connector") is None


# ---------------------------------------------------------------------------
# Route-level — default org never inherits another org's connection
# ---------------------------------------------------------------------------


def test_list_connectors_route_is_fresh_for_default_org(client: TestClient):
    from app import db

    # Another org connects servicenow.
    db.org_connector_set(
        "some_other_org",
        "servicenow",
        {"id": "servicenow", "name": "ServiceNow", "status": "connected"},
    )

    resp = client.get("/api/connectors", headers=AUTH)  # dev token → org "default"
    assert resp.status_code == 200
    sn = next((c for c in resp.json() if c.get("id") == "servicenow"), None)
    assert sn is not None
    assert sn.get("status") != "connected", (
        "default org must not inherit some_other_org's connection state"
    )
