"""Contract tests for the T2-S10-A DB connectivity framework.

These tests cover the 21 acceptance criteria from
T2_S10_A_DBConnectivity_v1.2.docx without requiring a live database.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

try:
    from backend.connectors.db import (
        DBConnectionError,
        DBConnectorConfig,
        DBQueryRejectedError,
        DBQueryResult,
        DBScopeViolationError,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        discover_schema,
        execute_query,
    )
    from backend.connectors.db import query_guard as query_guard_module
    from backend.connectors.db import service as db_service
    from backend.connectors.db.connection_pool import (
        _driver_factories,
        clear_all_pools,
        get_or_create_pool,
        register_driver,
    )
    from backend.connectors.db.sqlserver import (
        CATALOGUE_QUERY as SQLSERVER_CATALOGUE_QUERY,
        discover_schema_sqlserver,
    )
    from backend.app.db_connectors.models import ColumnMeta, TableMeta
except ModuleNotFoundError:
    from connectors.db import (
        DBConnectionError,
        DBConnectorConfig,
        DBQueryRejectedError,
        DBQueryResult,
        DBScopeViolationError,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        discover_schema,
        execute_query,
    )
    from connectors.db import query_guard as query_guard_module
    from connectors.db import service as db_service
    from connectors.db.connection_pool import (
        _driver_factories,
        clear_all_pools,
        get_or_create_pool,
        register_driver,
    )
    from connectors.db.sqlserver import (
        CATALOGUE_QUERY as SQLSERVER_CATALOGUE_QUERY,
        discover_schema_sqlserver,
    )
    from app.db_connectors.models import ColumnMeta, TableMeta

from app import db as app_db
from app import routes_db_connectors as routes_module
from app import security as app_security
from app.main import app

try:
    execute_query_module = importlib.import_module("backend.connectors.db.execute_query")
except ModuleNotFoundError:
    execute_query_module = importlib.import_module("connectors.db.execute_query")


AUTH = {"Authorization": "Bearer dev-token-change-me"}


class FakeCursor:
    def __init__(
        self,
        rows: list[Any] | None = None,
        columns: list[str] | None = None,
        execute_error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.description = [(column,) for column in (columns or [])]
        self.execute_error = execute_error
        self.executed: list[str] = []
        self.fetchmany_limits: list[int] = []
        self.closed = False

    def execute(self, query: str) -> None:
        self.executed.append(query)
        if self.execute_error is not None:
            raise self.execute_error

    def fetchmany(self, limit: int) -> list[Any]:
        self.fetchmany_limits.append(limit)
        return self.rows[:limit]

    def fetchall(self) -> list[Any]:
        return self.rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.acquire_timeouts: list[float | None] = []
        self.released: list[FakeConnection] = []

    def acquire(self, timeout: float | None = None) -> FakeConnection:
        self.acquire_timeouts.append(timeout)
        return self.conn

    def release(self, conn: FakeConnection) -> None:
        self.released.append(conn)


def _config(
    org_id: str = "org_a",
    connector_id: str = "sqlserver",
    connect_timeout_s: int = 10,
    query_timeout_s: int = 30,
) -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id=connector_id,
        org_id=org_id,
        host="db.example.com",
        port=1433,
        database="sales",
        driver="pyodbc",
        username_key="TEST_DB_USER",
        password_key="TEST_DB_PASS",
        connect_timeout_s=connect_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def _scope(
    schemas: list[str] | None = None,
    tables: list[str] | None = None,
    org_id: str = "org_a",
    connector_id: str = "sqlserver",
) -> ScopeDeclaration:
    return ScopeDeclaration(
        org_id=org_id,
        connector_id=connector_id,
        schemas=schemas or ["dbo"],
        tables=tables if tables is not None else ["orders"],
        declared_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        declared_by="analyst@example.com",
    )


def _install_service_pool(monkeypatch: pytest.MonkeyPatch, pool: FakePool) -> None:
    monkeypatch.setattr(execute_query_module, "_ensure_driver_imported", lambda connector_id: None)
    monkeypatch.setattr(execute_query_module, "get_or_create_pool", lambda config: pool)
    monkeypatch.setattr(db_service, "_ensure_driver_imported", lambda connector_id: None)
    monkeypatch.setattr(db_service, "get_or_create_pool", lambda config: pool)


@pytest.fixture(autouse=True)
def isolated_pool_registry() -> None:
    clear_all_pools()
    _driver_factories.clear()
    yield
    clear_all_pools()
    _driver_factories.clear()


class TestExecuteQueryHappyPath:
    def test_ac1_select_returns_expected_db_query_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        query = "SELECT id, amount FROM dbo.orders"
        cursor = FakeCursor(rows=[(1, 20), (2, None)], columns=["id", "amount"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)

        result = execute_query(_config(), _scope(tables=["orders"]), query, "org_a", "run_1")

        assert isinstance(result, DBQueryResult)
        assert result.columns == ["id", "amount"]
        assert result.rows == [(1, 20), (2, None)]
        assert result.row_count == 2
        assert result.query_hash == hashlib.sha256(query.encode()).hexdigest()
        assert result.truncated is False

    def test_ac19_null_value_is_returned_without_omitting_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = FakeCursor(rows=[(1, None), (2, "ready")], columns=["id", "note"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)

        result = execute_query(
            _config(),
            _scope(tables=["orders"]),
            "SELECT id, note FROM dbo.orders",
            "org_a",
            "run_nulls",
        )

        assert result.rows[0] == (1, None)
        assert result.row_count == 2

    def test_empty_result_set_returns_zero_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = FakeCursor(rows=[], columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)

        result = execute_query(
            _config(),
            _scope(tables=["orders"]),
            "SELECT id FROM dbo.orders",
            "org_a",
            "run_empty",
        )

        assert result.rows == []
        assert result.row_count == 0
        assert result.columns == ["id"]


class TestReadOnlyBeforeConnection:
    @pytest.mark.parametrize(
        "query,expected_error",
        [
            ("INSERT INTO dbo.orders (id) VALUES (1)", DBQueryRejectedError),
            ("UPDATE dbo.orders SET id = 2", DBQueryRejectedError),
            ("DELETE FROM dbo.orders WHERE id = 1", DBQueryRejectedError),
            ("CREATE TABLE dbo.tmp_orders (id INT)", DBQueryRejectedError),
        ],
    )
    def test_ac2_ac3_writes_and_ddl_rejected_before_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        query: str,
        expected_error: type[Exception],
    ) -> None:
        def fail_if_called(config: DBConnectorConfig) -> None:
            raise AssertionError("database connection should not be opened")

        monkeypatch.setattr(execute_query_module, "get_or_create_pool", fail_if_called)

        with pytest.raises(expected_error):
            execute_query(_config(), _scope(tables=["orders"]), query, "org_a", "run_reject")


class TestFailClosedScope:
    def test_ac4_out_of_scope_table_rejected_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            execute_query_module,
            "get_or_create_pool",
            lambda config: pytest.fail("database connection should not be opened"),
        )

        with pytest.raises(DBScopeViolationError):
            execute_query(
                _config(),
                _scope(tables=["orders"]),
                "SELECT id FROM dbo.payments",
                "org_a",
                "run_scope",
            )

    def test_ac5_parse_failure_rejects_query_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            query_guard_module,
            "_extract_table_references",
            lambda query: (_ for _ in ()).throw(RuntimeError("parse failed")),
        )
        monkeypatch.setattr(
            execute_query_module,
            "get_or_create_pool",
            lambda config: pytest.fail("database connection should not be opened"),
        )

        with pytest.raises(DBScopeViolationError):
            execute_query(
                _config(),
                _scope(tables=["orders"]),
                "SELECT id FROM dbo.orders",
                "org_a",
                "run_parse",
            )

    def test_ac6_empty_extraction_for_non_trivial_query_rejects_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(query_guard_module, "_extract_table_references", lambda query: set())
        monkeypatch.setattr(
            execute_query_module,
            "get_or_create_pool",
            lambda config: pytest.fail("database connection should not be opened"),
        )

        with pytest.raises(DBScopeViolationError):
            execute_query(
                _config(),
                _scope(tables=["orders"]),
                "SELECT id FROM dbo.orders",
                "org_a",
                "run_empty_extract",
            )


class TestScopeTablesEmpty:
    def test_ac14_tables_empty_allows_schema_qualified_declared_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = FakeCursor(rows=[(1,)], columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)

        result = execute_query(
            _config(),
            _scope(schemas=["dbo"], tables=[]),
            "SELECT id FROM dbo.any_table",
            "org_a",
            "run_empty_tables",
        )

        assert result.row_count == 1

    def test_ac14_tables_empty_rejects_unqualified_reference_as_not_a_bypass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            execute_query_module,
            "get_or_create_pool",
            lambda config: pytest.fail("database connection should not be opened"),
        )

        with pytest.raises(DBScopeViolationError):
            execute_query(
                _config(),
                _scope(schemas=["dbo"], tables=[]),
                "SELECT id FROM any_table",
                "org_a",
                "run_empty_tables_reject",
            )


class TestResultLimitsAndTimeouts:
    def test_ac7_truncates_at_max_rows_per_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [(i,) for i in range(execute_query_module.MAX_ROWS_PER_QUERY + 1)]
        cursor = FakeCursor(rows=rows, columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)

        result = execute_query(
            _config(),
            _scope(tables=["orders"]),
            "SELECT id FROM dbo.orders",
            "org_a",
            "run_truncate",
        )

        assert result.truncated is True
        assert len(result.rows) == execute_query_module.MAX_ROWS_PER_QUERY
        assert cursor.fetchmany_limits == [execute_query_module.MAX_ROWS_PER_QUERY + 1]

    def test_ac15_query_timeout_raises_connection_error_and_releases_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConnection(FakeCursor(execute_error=TimeoutError("query timeout")))
        pool = FakePool(conn)
        _install_service_pool(monkeypatch, pool)

        with pytest.raises(DBConnectionError) as exc_info:
            execute_query(
                _config(),
                _scope(tables=["orders"]),
                "SELECT id FROM dbo.orders",
                "org_a",
                "run_timeout",
            )

        assert exc_info.value.error_code == "query_timeout"
        assert pool.released == [conn]


class TestAuditAndTelemetry:
    def test_ac8_connector_queried_audit_event_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        query = "SELECT id FROM dbo.orders"
        cursor = FakeCursor(rows=[(1,)], columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        audit_calls: list[tuple[str, dict[str, Any]]] = []
        _install_service_pool(monkeypatch, pool)
        monkeypatch.setattr(execute_query_module, "log_event", lambda event, **fields: audit_calls.append((event, fields)))

        result = execute_query(_config(), _scope(tables=["orders"]), query, "org_a", "run_audit")

        assert audit_calls[0][0] == "connector_queried"
        assert audit_calls[0][1]["org_id"] == "org_a"
        assert audit_calls[0][1]["run_id"] == "run_audit"
        assert audit_calls[0][1]["connector_id"] == "sqlserver"
        assert audit_calls[0][1]["query_hash"] == result.query_hash
        assert audit_calls[0][1]["row_count"] == 1
        assert "duration_ms" in audit_calls[0][1]
        assert query not in str(audit_calls[0][1])

    def test_ac9_db_query_executed_telemetry_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = FakeCursor(rows=[(1,)], columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        telemetry_calls: list[tuple[str, dict[str, Any]]] = []
        _install_service_pool(monkeypatch, pool)
        monkeypatch.setattr(execute_query_module, "record_event", lambda event, payload: telemetry_calls.append((event, payload)))

        result = execute_query(
            _config(),
            _scope(tables=["orders"]),
            "SELECT id FROM dbo.orders",
            "org_a",
            "run_telemetry",
        )

        assert telemetry_calls == [
            (
                "db.query_executed",
                {
                    "connector_id": "sqlserver",
                    "query_hash": result.query_hash,
                    "row_count": 1,
                    "duration_ms": result.duration_ms,
                    "driver": "pyodbc",
                    "truncated": False,
                },
            )
        ]

    def test_ac9_telemetry_failure_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = FakeCursor(rows=[(1,)], columns=["id"])
        pool = FakePool(FakeConnection(cursor))
        _install_service_pool(monkeypatch, pool)
        monkeypatch.setattr(execute_query_module, "record_event", lambda event, payload: (_ for _ in ()).throw(RuntimeError("sink down")))

        result = execute_query(
            _config(),
            _scope(tables=["orders"]),
            "SELECT id FROM dbo.orders",
            "org_a",
            "run_telemetry_fail",
        )

        assert result.row_count == 1


class TestSchemaDiscovery:
    def test_ac10_sqlserver_discovery_uses_catalogue_query_only(self) -> None:
        cursor = FakeCursor(
            rows=[("dbo", "accounts", "id"), ("dbo", "accounts", "name")],
            columns=["schema", "table", "column"],
        )

        result = discover_schema_sqlserver(FakeConnection(cursor))

        assert cursor.executed == [SQLSERVER_CATALOGUE_QUERY]
        assert result.schemas == ["dbo"]
        assert [(table.schema, table.table) for table in result.tables] == [("dbo", "accounts")]
        assert [column.column for column in result.columns] == ["id", "name"]

    def test_ac11_discover_schema_logs_schema_discovered_not_connector_queried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schema_result = SchemaDiscoveryResult(
            schemas=["dbo"],
            tables=[TableMeta(schema="dbo", table="orders")],
            columns=[ColumnMeta(schema="dbo", table="orders", column="id")],
            estimated_row_counts=None,
        )
        pool = FakePool(FakeConnection(FakeCursor()))
        audit_calls: list[tuple[str, dict[str, Any]]] = []
        _install_service_pool(monkeypatch, pool)
        monkeypatch.setattr(db_service, "_discover_schema_for_connector", lambda conn, connector_id: schema_result)
        monkeypatch.setattr(db_service, "log_event", lambda event, **fields: audit_calls.append((event, fields)))

        result = discover_schema(_config(), "org_a")

        assert result.estimated_row_counts is None
        assert audit_calls[0][0] == "schema_discovered"
        assert audit_calls[0][0] != "connector_queried"
        assert audit_calls[0][1]["schema_count"] == 1
        assert audit_calls[0][1]["table_count"] == 1


class TestPoolsAndCredentials:
    def test_ac12_pools_are_isolated_by_org_and_connector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_driver(config: DBConnectorConfig, username: str, password: str) -> MagicMock:
            return MagicMock()

        register_driver("sqlserver", fake_driver)
        monkeypatch.setenv("TEST_DB_USER", "user")
        monkeypatch.setenv("TEST_DB_PASS", "secret")

        pool_a = get_or_create_pool(_config(org_id="org_A"))
        pool_b = get_or_create_pool(_config(org_id="org_B"))

        assert pool_a is not pool_b
        assert pool_a.key == ("org_A", "sqlserver")
        assert pool_b.key == ("org_B", "sqlserver")

    def test_ac13_credentials_resolved_but_not_retained_on_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, str]] = []

        def fake_driver(config: DBConnectorConfig, username: str, password: str) -> MagicMock:
            captured.append((username, password))
            return MagicMock()

        register_driver("sqlserver", fake_driver)
        monkeypatch.setenv("TEST_DB_USER", "db_user")
        monkeypatch.setenv("TEST_DB_PASS", "db_secret")

        pool = get_or_create_pool(_config())

        assert captured == [("db_user", "db_secret")]
        assert not hasattr(pool, "username")
        assert not hasattr(pool, "password")
        assert "db_secret" not in repr(pool)

    def test_ac20_config_serialization_contains_no_password_value(self) -> None:
        payload = asdict(_config())

        assert "password" not in payload
        assert payload["password_key"] == "TEST_DB_PASS"
        assert "db_secret" not in str(payload)

    def test_ac20_driver_exception_does_not_leak_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_driver(config: DBConnectorConfig, username: str, password: str) -> MagicMock:
            raise RuntimeError("driver failed")

        register_driver("sqlserver", failing_driver)
        monkeypatch.setenv("TEST_DB_USER", "db_user")
        monkeypatch.setenv("TEST_DB_PASS", "super_secret_password")

        with pytest.raises(DBConnectionError) as exc_info:
            get_or_create_pool(_config())

        assert "super_secret_password" not in str(exc_info.value)


class TestApiRoutes:
    @pytest.fixture()
    def client(self) -> TestClient:
        return TestClient(app)

    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("GET", "/api/db-connectors/sqlserver/schema", None),
            ("POST", "/api/db-connectors/sqlserver/scope", {"schemas": ["dbo"], "tables": []}),
            ("GET", "/api/db-connectors/sqlserver/scope", None),
            ("GET", "/api/db-connectors/sqlserver/connection-test", None),
        ],
    )
    def test_ac18_all_endpoints_return_401_when_unauthenticated(
        self,
        client: TestClient,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
    ) -> None:
        response = client.request(method, path, json=json_body)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method,path,json_body",
        [
            ("GET", "/api/db-connectors/sqlserver/schema", None),
            ("POST", "/api/db-connectors/sqlserver/scope", {"schemas": ["dbo"], "tables": []}),
            ("GET", "/api/db-connectors/sqlserver/scope", None),
            ("GET", "/api/db-connectors/sqlserver/connection-test", None),
        ],
    )
    def test_ac18_all_endpoints_return_403_for_viewer_role(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: TestClient,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
    ) -> None:
        monkeypatch.setattr(app_security, "_DEV_TOKEN_ROLES", frozenset({"viewer"}))
        response = client.request(method, path, headers=AUTH, json=json_body)
        assert response.status_code == 403

    def test_ac16_connection_test_uses_connect_timeout(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        connector_id = "sqlserver_contract_timeout"
        config = _config(
            org_id="default",
            connector_id=connector_id,
            connect_timeout_s=3,
            query_timeout_s=17,
        )
        cursor = FakeCursor(rows=[(1,)], columns=["ok"])
        pool = FakePool(FakeConnection(cursor))

        app_db.kv_set(
            routes_module._kv_config_key("default", connector_id),
            asdict(config),
        )
        monkeypatch.setattr(
            db_service,
            "_ensure_driver_imported",
            lambda connector_id: None,
        )
        monkeypatch.setattr(
            "backend.connectors.db.connection_pool.get_or_create_pool",
            lambda loaded_config: pool,
            raising=False,
        )
        monkeypatch.setattr(
            "connectors.db.connection_pool.get_or_create_pool",
            lambda loaded_config: pool,
            raising=False,
        )

        response = client.get(
            f"/api/db-connectors/{connector_id}/connection-test",
            headers=AUTH,
        )

        assert response.status_code == 200
        assert response.json() == {"connected": True, "error_code": None}
        assert pool.acquire_timeouts == [3]
        assert cursor.executed == ["SELECT 1"]

    def test_ac17_invalid_scope_table_returns_400_and_is_not_saved(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        org_id = "contract_invalid_org"
        connector_id = "sqlserver"
        config = _config(org_id=org_id, connector_id=connector_id)
        discovered = SchemaDiscoveryResult(
            schemas=["dbo"],
            tables=[TableMeta(schema="dbo", table="accounts")],
            columns=[ColumnMeta(schema="dbo", table="accounts", column="id")],
            estimated_row_counts=None,
        )
        pool = FakePool(FakeConnection(FakeCursor()))

        app_db.kv_set(routes_module._kv_config_key(org_id, connector_id), asdict(config))
        try:
            from backend.connectors.db.scope import save_discovered_schema
        except ModuleNotFoundError:
            from connectors.db.scope import save_discovered_schema

        save_discovered_schema(org_id, connector_id, discovered)
        monkeypatch.setattr(
            "backend.connectors.db.connection_pool.get_or_create_pool",
            lambda loaded_config: pool,
            raising=False,
        )
        monkeypatch.setattr(
            "connectors.db.connection_pool.get_or_create_pool",
            lambda loaded_config: pool,
            raising=False,
        )
        monkeypatch.setattr(
            routes_module,
            "_discover_schema_for_connector",
            lambda conn, loaded_connector_id: discovered,
        )

        response = client.post(
            f"/api/db-connectors/{connector_id}/scope",
            headers=AUTH,
            json={"org_id": org_id, "schemas": ["dbo"], "tables": ["missing_table"]},
        )

        assert response.status_code == 400
        assert app_db.kv_get(routes_module._kv_scope_key(org_id, connector_id)) is None


class TestPublicImports:
    def test_ac21_public_framework_api_is_importable(self) -> None:
        assert callable(execute_query)
        assert callable(discover_schema)
        assert DBConnectorConfig is not None
        assert ScopeDeclaration is not None
        assert issubclass(DBQueryRejectedError, Exception)
        assert issubclass(DBScopeViolationError, Exception)
