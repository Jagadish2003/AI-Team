"""
Contract tests for T2-S10-A DB Connector API Routes.

Covers:
  - GET  /api/db-connectors/{connector_id}/schema
  - POST /api/db-connectors/{connector_id}/scope
  - GET  /api/db-connectors/{connector_id}/scope
  - GET  /api/db-connectors/{connector_id}/connection-test

Auth requirements:
  - 401 when no token supplied
  - 403 when token is invalid (wrong role not testable in dev single-token mode,
    but 401/403 logic is verified via missing/bad tokens)

Scope and connection-test routes are tested without a live database:
  - Scope tests rely only on KV storage (no pool needed).
  - Connection-test with no registered config returns
    {connected: false, error_code: "config_not_found"} — this is the
    fail-closed behaviour required by the spec.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db_connectors.models import ColumnMeta, SchemaDiscoveryResult, TableMeta
from app.main import app
from connectors.db.scope import save_discovered_schema

AUTH = {"Authorization": "Bearer dev-token-change-me"}
BAD_AUTH = {"Authorization": "Bearer wrong-token"}
CONNECTOR = "sqlserver"


def _seed_discovered_schema(
    connector_id: str = CONNECTOR,
    org_id: str = "default",
    schemas: list[str] | None = None,
    tables: list[tuple[str, str]] | None = None,
) -> None:
    table_pairs = tables or [("dbo", "accounts"), ("dbo", "contacts"), ("dbo", "leads")]
    schema_names = schemas or sorted({schema for schema, _table in table_pairs})
    save_discovered_schema(
        org_id,
        connector_id,
        SchemaDiscoveryResult(
            schemas=schema_names,
            tables=[
                TableMeta(schema=schema, table=table)
                for schema, table in table_pairs
            ],
            columns=[
                ColumnMeta(schema=schema, table=table, column="id")
                for schema, table in table_pairs
            ],
            estimated_row_counts=None,
        ),
    )


@pytest.fixture(autouse=True, scope="module")
def seed_dev_owner():
    """Seed dev-token as owner of 'default' org so RBAC checks pass."""
    from app.rbac import seed_owner, _ensure_members_table
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Auth guard — all four routes must return 401 without a valid token
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthGuard:
    def test_schema_no_auth_returns_401(self, client: TestClient) -> None:
        r = client.get(f"/api/db-connectors/{CONNECTOR}/schema")
        assert r.status_code == 401

    def test_scope_post_no_auth_returns_401(self, client: TestClient) -> None:
        r = client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            json={"schemas": ["dbo"], "tables": []},
        )
        assert r.status_code == 401

    def test_scope_get_no_auth_returns_401(self, client: TestClient) -> None:
        r = client.get(f"/api/db-connectors/{CONNECTOR}/scope")
        assert r.status_code == 401

    def test_connection_test_no_auth_returns_401(self, client: TestClient) -> None:
        r = client.get(f"/api/db-connectors/{CONNECTOR}/connection-test")
        assert r.status_code == 401

    def test_schema_bad_token_returns_401(self, client: TestClient) -> None:
        r = client.get(f"/api/db-connectors/{CONNECTOR}/schema", headers=BAD_AUTH)
        assert r.status_code == 401

    def test_connection_test_bad_token_returns_401(self, client: TestClient) -> None:
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/connection-test", headers=BAD_AUTH
        )
        assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# POST /scope — saves scope and returns 201
# ─────────────────────────────────────────────────────────────────────────────


class TestPostScope:
    def test_post_scope_returns_201(self, client: TestClient) -> None:
        _seed_discovered_schema()
        r = client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["dbo"], "tables": ["accounts", "contacts"]},
        )
        assert r.status_code == 201

    def test_post_scope_response_body(self, client: TestClient) -> None:
        _seed_discovered_schema(tables=[("dbo", "accounts")])
        r = client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["dbo"], "tables": []},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "scope_saved"
        assert body["connector_id"] == CONNECTOR

    def test_post_scope_empty_tables_allowed(self, client: TestClient) -> None:
        """tables=[] is valid — means any table in the declared schemas."""
        _seed_discovered_schema(schemas=["public"], tables=[("public", "events")])
        r = client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["public"], "tables": []},
        )
        assert r.status_code == 201

    def test_post_scope_multiple_schemas(self, client: TestClient) -> None:
        _seed_discovered_schema(
            schemas=["dbo", "hr", "finance"],
            tables=[("dbo", "accounts"), ("hr", "employees"), ("finance", "ledger")],
        )
        r = client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["dbo", "hr", "finance"], "tables": []},
        )
        assert r.status_code == 201

    def test_post_scope_unknown_table_returns_400(self, client: TestClient) -> None:
        connector_id = "sqlserver_unknown_table_contract"
        _seed_discovered_schema(
            connector_id=connector_id,
            tables=[("dbo", "accounts")],
        )

        r = client.post(
            f"/api/db-connectors/{connector_id}/scope",
            headers=AUTH,
            json={"schemas": ["dbo"], "tables": ["missing_table"]},
        )

        assert r.status_code == 400
        assert r.json()["detail"]["error_code"] == "unknown_table"
        assert client.get(
            f"/api/db-connectors/{connector_id}/scope",
            headers=AUTH,
        ).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# GET /scope — returns saved scope or 404
# ─────────────────────────────────────────────────────────────────────────────


class TestGetScope:
    def test_get_scope_404_when_no_scope_declared(self, client: TestClient) -> None:
        r = client.get(
            "/api/db-connectors/oracle_db/scope", headers=AUTH
        )
        assert r.status_code == 404

    def test_get_scope_returns_saved_scope(self, client: TestClient) -> None:
        # Save scope first
        _seed_discovered_schema(tables=[("dbo", "leads")])
        client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["dbo"], "tables": ["leads"]},
        )
        # Then retrieve it
        r = client.get(f"/api/db-connectors/{CONNECTOR}/scope", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["connector_id"] == CONNECTOR
        assert "dbo" in body["schemas"]
        assert "leads" in body["tables"]

    def test_get_scope_response_shape(self, client: TestClient) -> None:
        _seed_discovered_schema(schemas=["sales"], tables=[("sales", "accounts")])
        client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json={"schemas": ["sales"], "tables": []},
        )
        r = client.get(f"/api/db-connectors/{CONNECTOR}/scope", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # Required fields
        for field in ("org_id", "connector_id", "schemas", "tables", "declared_at", "declared_by"):
            assert field in body, f"Missing field: {field}"

    def test_scope_roundtrip(self, client: TestClient) -> None:
        """POST then GET returns identical schemas and tables."""
        payload = {"schemas": ["analytics", "reporting"], "tables": ["events", "pageviews"]}
        _seed_discovered_schema(
            schemas=payload["schemas"],
            tables=[("analytics", "events"), ("reporting", "pageviews")],
        )
        client.post(
            f"/api/db-connectors/{CONNECTOR}/scope",
            headers=AUTH,
            json=payload,
        )
        r = client.get(f"/api/db-connectors/{CONNECTOR}/scope", headers=AUTH)
        body = r.json()
        assert set(body["schemas"]) == set(payload["schemas"])
        assert set(body["tables"]) == set(payload["tables"])


# ─────────────────────────────────────────────────────────────────────────────
# GET /schema — 404 when connector config not registered
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSchema:
    def test_schema_404_when_config_not_registered(self, client: TestClient) -> None:
        """No DBConnectorConfig in KV → 404, not 500."""
        r = client.get(
            "/api/db-connectors/postgresql/schema", headers=AUTH
        )
        assert r.status_code == 404

    def test_schema_sqlserver_no_config_returns_404(self, client: TestClient) -> None:
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/schema", headers=AUTH
        )
        # Config not registered in test env → 404
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# GET /connection-test — fail-closed when config missing
# ─────────────────────────────────────────────────────────────────────────────


class TestConnectionTest:
    def test_connection_test_no_config_returns_200(self, client: TestClient) -> None:
        """Route always returns 200; connected field carries the result."""
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/connection-test", headers=AUTH
        )
        assert r.status_code == 200

    def test_connection_test_no_config_connected_false(self, client: TestClient) -> None:
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/connection-test", headers=AUTH
        )
        body = r.json()
        assert body["connected"] is False

    def test_connection_test_no_config_error_code(self, client: TestClient) -> None:
        """Fail-closed: config_not_found when no connector config in KV."""
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/connection-test", headers=AUTH
        )
        body = r.json()
        assert body["error_code"] == "config_not_found"

    def test_connection_test_response_shape(self, client: TestClient) -> None:
        r = client.get(
            f"/api/db-connectors/{CONNECTOR}/connection-test", headers=AUTH
        )
        body = r.json()
        assert "connected" in body
        assert "error_code" in body
        assert isinstance(body["connected"], bool)

    def test_connection_test_unknown_connector(self, client: TestClient) -> None:
        """Unknown connector_id with no config → connected=false, config_not_found."""
        r = client.get(
            "/api/db-connectors/unknown_db/connection-test", headers=AUTH
        )
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is False
        assert body["error_code"] == "config_not_found"
