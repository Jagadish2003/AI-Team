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


# ---------------------------------------------------------------------------
# DB-connector scope routes — org resolved from auth context, never the body
# (R17-D3 / AT-448). The dev token carries no JWT org claim in tests, so the
# X-Org-Id header drives the per-request org.
# ---------------------------------------------------------------------------


def _seed_discovered_schema(org_id: str, connector_id: str) -> None:
    try:
        from backend.app.db_connectors.models import (
            ColumnMeta,
            SchemaDiscoveryResult,
            TableMeta,
        )
        from backend.connectors.db.scope import save_discovered_schema
    except ModuleNotFoundError:
        from app.db_connectors.models import (
            ColumnMeta,
            SchemaDiscoveryResult,
            TableMeta,
        )
        from connectors.db.scope import save_discovered_schema

    save_discovered_schema(
        org_id,
        connector_id,
        SchemaDiscoveryResult(
            schemas=["dbo"],
            tables=[TableMeta(schema="dbo", table="accounts")],
            columns=[ColumnMeta(schema="dbo", table="accounts", column="id")],
            estimated_row_counts=None,
        ),
    )


def test_db_connector_scope_saved_in_one_org_is_not_visible_to_another(client: TestClient):
    from app.rbac import seed_owner

    connector_id = "sqlserver"
    # Give the dev user a role in both orgs so the scope routes pass RBAC and the
    # test exercises org-scoped persistence/isolation rather than the role gate.
    seed_owner("org_scope_A", "dev-token-change-me")
    seed_owner("org_scope_B", "dev-token-change-me")
    _seed_discovered_schema("org_scope_A", connector_id)

    # org_A declares scope.
    created = client.post(
        f"/api/db-connectors/{connector_id}/scope",
        headers={**AUTH, "X-Org-Id": "org_scope_A"},
        json={"schemas": ["dbo"], "tables": ["accounts"]},
    )
    assert created.status_code == 201
    assert created.json()["org_id"] == "org_scope_A"

    # org_A reads its scope back.
    own = client.get(
        f"/api/db-connectors/{connector_id}/scope",
        headers={**AUTH, "X-Org-Id": "org_scope_A"},
    )
    assert own.status_code == 200
    assert own.json()["org_id"] == "org_scope_A"

    # org_B never declared scope for this connector → 404, no cross-org read.
    other = client.get(
        f"/api/db-connectors/{connector_id}/scope",
        headers={**AUTH, "X-Org-Id": "org_scope_B"},
    )
    assert other.status_code == 404


def test_db_connector_scope_ignores_body_org_id(client: TestClient):
    """A caller cannot redirect a scope write to another org via the body."""
    from app import db
    from app.rbac import seed_owner

    connector_id = "sqlserver"
    seed_owner("org_real", "dev-token-change-me")
    _seed_discovered_schema("org_real", connector_id)

    resp = client.post(
        f"/api/db-connectors/{connector_id}/scope",
        headers={**AUTH, "X-Org-Id": "org_real"},
        # Attempt to spoof a different org through the (now-ignored) body field.
        json={"org_id": "org_victim", "schemas": ["dbo"], "tables": ["accounts"]},
    )
    assert resp.status_code == 201
    assert resp.json()["org_id"] == "org_real"

    # Persisted only under the authenticated org, never the body-supplied one.
    assert db.kv_get("db_connector_scope:org_real:sqlserver") is not None
    assert db.kv_get("db_connector_scope:org_victim:sqlserver") is None
