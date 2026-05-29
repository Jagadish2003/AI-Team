"""
Integration smoke tests for the PostgreSQL driver - T2-S10-A Task T5.

The default tests mock psycopg2.connect and a PostgreSQL connection so they do
not require a live database. They verify:
  - The exact catalogue query from the task prompt is issued unchanged.
  - SSL mode supports require, prefer, and disable via config or env var.
  - connect_timeout_s and query_timeout_s are passed through psycopg2 options.
  - Connection failures raise DBConnectionError without credential details.
  - discover_schema_postgresql() returns a SchemaDiscoveryResult.
  - PostgreSQL identifiers are double-quoted.

Set POSTGRESQL_LIVE=1 and configure POSTGRESQL_* env vars to run the optional
live catalogue query smoke test.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from backend.app.db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
)
from backend.connectors.db.connection_pool import (
    clear_pool,
    get_or_create_pool,
    register_driver,
)
from backend.connectors.db.postgresql import (
    ALLOWED_SSL_MODES,
    CATALOGUE_QUERY,
    SSL_MODE_ENV_VAR,
    TRIVIAL_QUERY,
    _connect_kwargs,
    create_postgresql_connection,
    discover_schema_postgresql,
    qualified_table_name,
    quote_identifier,
)


def _config(
    host: str = "postgres.example.com",
    port: int = 5432,
    database: str = "enterprise_db",
    ssl_mode: str | None = "prefer",
    connect_timeout_s: int = 10,
    query_timeout_s: int = 30,
) -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id="postgresql",
        org_id="acme",
        host=host,
        port=port,
        database=database,
        driver="psycopg2",
        username_key="POSTGRESQL_USERNAME",
        password_key="POSTGRESQL_PASSWORD",
        ssl_mode=ssl_mode,
        connect_timeout_s=connect_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def _mock_cursor(rows: list[tuple]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


def _mock_conn(rows: list[tuple] | None = None) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = _mock_cursor(rows or [])
    return conn


class TestCatalogueQuery:
    def test_catalogue_query_matches_prompt(self):
        expected = (
            "SELECT table_schema, table_name, column_name "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
        )
        assert CATALOGUE_QUERY == expected

    def test_catalogue_query_is_select(self):
        assert CATALOGUE_QUERY.strip().upper().startswith("SELECT")

    def test_catalogue_query_excludes_postgresql_system_schemas(self):
        assert "'pg_catalog'" in CATALOGUE_QUERY
        assert "'information_schema'" in CATALOGUE_QUERY

    def test_discover_schema_executes_catalogue_query(self):
        conn = _mock_conn([])
        discover_schema_postgresql(conn)
        conn.cursor.return_value.execute.assert_called_once_with(CATALOGUE_QUERY)

    def test_trivial_query_is_select_one(self):
        assert TRIVIAL_QUERY == "SELECT 1"


class TestSslMode:
    @pytest.mark.parametrize("mode", sorted(ALLOWED_SSL_MODES))
    def test_supported_ssl_modes_from_config(self, mode: str):
        cfg = _config(ssl_mode=mode)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["sslmode"] == mode

    def test_ssl_mode_normalized_from_config(self):
        cfg = _config(ssl_mode="REQUIRE")
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["sslmode"] == "require"

    def test_ssl_mode_from_env_var_when_config_empty(self, monkeypatch):
        monkeypatch.setenv(SSL_MODE_ENV_VAR, "disable")
        cfg = _config(ssl_mode=None)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["sslmode"] == "disable"

    def test_ssl_mode_defaults_to_prefer(self, monkeypatch):
        monkeypatch.delenv(SSL_MODE_ENV_VAR, raising=False)
        cfg = _config(ssl_mode=None)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["sslmode"] == "prefer"

    def test_invalid_ssl_mode_rejected(self):
        cfg = _config(ssl_mode="verify-full")
        with pytest.raises(DBConnectionError) as exc_info:
            _connect_kwargs(cfg, "user", "pass")
        assert exc_info.value.error_code == "invalid_ssl_mode"


class TestTimeouts:
    def test_connect_timeout_passed_to_psycopg2(self):
        cfg = _config(connect_timeout_s=15)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["connect_timeout"] == 15

    def test_query_timeout_passed_as_statement_timeout_option(self):
        cfg = _config(query_timeout_s=45)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["options"] == "-c statement_timeout=45000"

    def test_connect_and_query_timeouts_are_independent(self):
        cfg = _config(connect_timeout_s=7, query_timeout_s=60)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["connect_timeout"] == 7
        assert kwargs["options"] == "-c statement_timeout=60000"

    def test_create_connection_passes_timeout_options_to_psycopg2(self):
        cfg = _config(connect_timeout_s=5, query_timeout_s=12)
        mock_conn = _mock_conn()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            return_value=mock_conn,
        ) as mock_connect:
            conn = create_postgresql_connection(cfg, "user", "pass")
        assert conn is mock_conn
        _, kwargs = mock_connect.call_args
        assert kwargs["connect_timeout"] == 5
        assert kwargs["options"] == "-c statement_timeout=12000"


class TestConnectionPoolIntegration:
    def test_pool_creates_postgresql_connection_via_psycopg2(self):
        cfg = _config(ssl_mode=None, connect_timeout_s=3, query_timeout_s=9)
        clear_pool(cfg.org_id, cfg.connector_id)
        register_driver("postgresql", create_postgresql_connection)
        env = {
            "POSTGRESQL_USERNAME": "pg_user",
            "POSTGRESQL_PASSWORD": "pg_pass",
            SSL_MODE_ENV_VAR: "require",
        }
        mock_conn = _mock_conn()
        try:
            with patch.dict(os.environ, env):
                with patch(
                    "backend.connectors.db.postgresql.psycopg2.connect",
                    return_value=mock_conn,
                ) as mock_connect:
                    pool = get_or_create_pool(cfg)
            assert pool.key == ("acme", "postgresql")
            _, kwargs = mock_connect.call_args
            assert kwargs["sslmode"] == "require"
            assert kwargs["connect_timeout"] == 3
            assert kwargs["options"] == "-c statement_timeout=9000"
        finally:
            clear_pool(cfg.org_id, cfg.connector_id)


class TestCredentialIsolation:
    def test_connection_failure_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("could not connect"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "secret_user", "secret_pass")
        assert exc_info.value.error_code == "connection_failed"

    def test_error_message_contains_no_credentials(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("could not connect"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "secret_user", "secret_pass")
        msg = str(exc_info.value)
        assert "secret_user" not in msg
        assert "secret_pass" not in msg
        assert "password" not in msg.lower()

    def test_error_message_contains_host_and_db(self):
        cfg = _config(host="prod-pg.corp.local", database="finance")
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("could not connect"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "user", "pass")
        msg = str(exc_info.value)
        assert "prod-pg.corp.local" in msg
        assert "finance" in msg


class TestSchemaDiscovery:
    SAMPLE_ROWS = [
        ("public", "accounts", "id"),
        ("public", "accounts", "name"),
        ("analytics", "events", "event_id"),
        ("analytics", "events", "created_at"),
    ]

    def test_catalogue_query_executes_successfully_against_mock_connection(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert isinstance(result, SchemaDiscoveryResult)
        conn.cursor.return_value.execute.assert_called_once_with(CATALOGUE_QUERY)

    def test_schemas_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert result.schemas == ["analytics", "public"]

    def test_tables_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        pairs = {(t.schema, t.table) for t in result.tables}
        assert ("public", "accounts") in pairs
        assert ("analytics", "events") in pairs

    def test_columns_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        triples = {(c.schema, c.table, c.column) for c in result.columns}
        assert ("public", "accounts", "id") in triples
        assert ("analytics", "events", "created_at") in triples

    def test_estimated_row_counts_is_none(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert result.estimated_row_counts is None

    def test_empty_catalogue_returns_empty_result(self):
        conn = _mock_conn([])
        result = discover_schema_postgresql(conn)
        assert result.schemas == []
        assert result.tables == []
        assert result.columns == []

    def test_cursor_closed_after_query(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        discover_schema_postgresql(conn)
        conn.cursor.return_value.close.assert_called_once()


class TestQuoteIdentifier:
    def test_simple_schema_name(self):
        assert quote_identifier("public") == '"public"'

    def test_name_with_spaces(self):
        assert quote_identifier("reporting schema") == '"reporting schema"'

    def test_embedded_double_quote_is_escaped(self):
        assert quote_identifier('odd"name') == '"odd""name"'

    def test_schema_table_reference_uses_double_quoted_schema(self):
        assert qualified_table_name("public", "accounts") == '"public"."accounts"'


LIVE = pytest.mark.skipif(
    os.environ.get("POSTGRESQL_LIVE") != "1",
    reason="Set POSTGRESQL_LIVE=1 and configure POSTGRESQL_* env vars to run live tests",
)


@LIVE
class TestLiveSchemaDiscovery:
    def test_live_catalogue_query_executes_successfully(self):
        cfg = DBConnectorConfig(
            connector_id="postgresql",
            org_id="live_test",
            host=os.environ["POSTGRESQL_HOST"],
            port=int(os.environ.get("POSTGRESQL_PORT", "5432")),
            database=os.environ["POSTGRESQL_DATABASE"],
            driver="psycopg2",
            username_key="POSTGRESQL_USERNAME",
            password_key="POSTGRESQL_PASSWORD",
            ssl_mode=os.environ.get(SSL_MODE_ENV_VAR),
        )
        conn = create_postgresql_connection(
            cfg,
            os.environ["POSTGRESQL_USERNAME"],
            os.environ["POSTGRESQL_PASSWORD"],
        )
        try:
            result = discover_schema_postgresql(conn)
            assert isinstance(result, SchemaDiscoveryResult)
        finally:
            conn.close()
