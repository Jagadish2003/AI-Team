"""
Integration smoke tests for the PostgreSQL driver — T2-S10-A Task T5.

These tests do NOT require a live PostgreSQL database. They mock
psycopg2.connect and verify:
  - The exact catalogue query from the spec is issued unchanged.
  - SSL mode is configurable and applied correctly.
  - connect_timeout_s and query_timeout_s are applied correctly.
  - Connection failures raise DBConnectionError with no credential details.
  - discover_schema_postgresql() returns a well-formed SchemaDiscoveryResult.
  - quote_identifier() produces correct PostgreSQL double-quote notation.

A separate live-integration marker is provided for environments that have
a reachable PostgreSQL database. Those tests are skipped by default.
"""

from __future__ import annotations

import psycopg2
import pytest
from unittest.mock import MagicMock, patch

from backend.app.db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
)
from backend.connectors.db.postgresql import (
    CATALOGUE_QUERY,
    create_postgresql_connection,
    discover_schema_postgresql,
    quote_identifier,
    _build_connection_string,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(
    host: str = "postgres.example.com",
    port: int = 5432,
    database: str = "enterprise_db",
    connect_timeout_s: int = 10,
    query_timeout_s: int = 30,
    ssl_mode: str | None = None,
) -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id="postgresql",
        org_id="acme",
        host=host,
        port=port,
        database=database,
        driver="psycopg2",
        username_key="POSTGRES_USERNAME",
        password_key="POSTGRES_PASSWORD",
        ssl_mode=ssl_mode,
        connect_timeout_s=connect_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def _mock_cursor(rows: list[tuple]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


def _mock_conn(rows: list[tuple] | None = None) -> MagicMock:
    conn = MagicMock(spec=psycopg2.extensions.connection)
    conn.cursor.return_value = _mock_cursor(rows or [])
    return conn


# ---------------------------------------------------------------------------
# Catalogue query correctness
# ---------------------------------------------------------------------------

class TestCatalogueQuery:
    def test_catalogue_query_matches_spec(self):
        """The exact catalogue SQL from the spec must be used unchanged."""
        expected = (
            "SELECT table_schema, table_name, column_name "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        assert CATALOGUE_QUERY == expected

    def test_catalogue_query_is_select(self):
        assert CATALOGUE_QUERY.strip().upper().startswith("SELECT")

    def test_catalogue_query_excludes_pg_catalog(self):
        assert "'pg_catalog'" in CATALOGUE_QUERY

    def test_catalogue_query_excludes_information_schema(self):
        assert "'information_schema'" in CATALOGUE_QUERY

    def test_discover_schema_executes_catalogue_query(self):
        """discover_schema_postgresql() must issue exactly the catalogue query."""
        conn = _mock_conn([])
        discover_schema_postgresql(conn)
        conn.cursor.return_value.execute.assert_called_once_with(CATALOGUE_QUERY)


# ---------------------------------------------------------------------------
# SSL mode handling
# ---------------------------------------------------------------------------

class TestSSLMode:
    def test_ssl_mode_prefer_by_default(self):
        """When ssl_mode is None, 'prefer' is used."""
        cfg = _config(ssl_mode=None)
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "sslmode=prefer" in conn_str

    def test_ssl_mode_require(self):
        cfg = _config(ssl_mode="require")
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "sslmode=require" in conn_str

    def test_ssl_mode_disable(self):
        cfg = _config(ssl_mode="disable")
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "sslmode=disable" in conn_str

    def test_connection_string_includes_required_params(self):
        cfg = _config(host="prod-pg.corp", port=5433, database="analytics")
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "host=prod-pg.corp" in conn_str
        assert "port=5433" in conn_str
        assert "database=analytics" in conn_str
        assert "user=user" in conn_str
        assert "password=pass" in conn_str


# ---------------------------------------------------------------------------
# Timeout application
# ---------------------------------------------------------------------------

class TestTimeouts:
    def test_connect_timeout_in_connection_string(self):
        cfg = _config(connect_timeout_s=15)
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "connect_timeout=15" in conn_str

    def test_query_timeout_set_on_connection_object(self):
        """statement_timeout must be set to query_timeout_s * 1000 (ms) after connect."""
        cfg = _config(query_timeout_s=45)
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.postgresql.psycopg2.connect", return_value=mock_conn):
            conn = create_postgresql_connection(cfg, "user", "pass")
        
        # Verify that SET statement_timeout was executed
        cursor_calls = mock_conn.cursor.return_value.execute.call_args_list
        set_timeout_call = [c for c in cursor_calls if "statement_timeout" in str(c)]
        assert len(set_timeout_call) > 0
        assert "45000" in str(set_timeout_call[0])  # 45 seconds = 45000 ms

    def test_connect_timeout_and_query_timeout_are_independent(self):
        """The two timeouts are applied to different mechanisms."""
        cfg = _config(connect_timeout_s=5, query_timeout_s=60)
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "connect_timeout=5" in conn_str


# ---------------------------------------------------------------------------
# Connection string — no credential leakage
# ---------------------------------------------------------------------------

class TestCredentialIsolation:
    def test_connection_string_not_in_error_message(self):
        """DBConnectionError message must not contain the connection string."""
        cfg = _config()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("connection refused"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "secret_user", "secret_pass")

        msg = str(exc_info.value)
        assert "secret_user" not in msg
        assert "secret_pass" not in msg
        assert "password=" not in msg

    def test_error_message_contains_host_and_db(self):
        """Error message must include host and database for diagnostics."""
        cfg = _config(host="prod-pg.corp.local", database="FinanceDB")
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("connection refused"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "user", "pass")

        msg = str(exc_info.value)
        assert "prod-pg.corp.local" in msg
        assert "FinanceDB" in msg

    def test_connection_failure_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=psycopg2.OperationalError("cannot connect"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "user", "pass")
        assert exc_info.value.error_code == "connection_failed"

    def test_unexpected_exception_also_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.postgresql.psycopg2.connect",
            side_effect=RuntimeError("unexpected"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_postgresql_connection(cfg, "user", "pass")
        assert exc_info.value.error_code == "connection_failed"


# ---------------------------------------------------------------------------
# discover_schema_postgresql — result shape
# ---------------------------------------------------------------------------

class TestSchemaDiscovery:
    SAMPLE_ROWS = [
        ("public", "accounts", "id"),
        ("public", "accounts", "name"),
        ("public", "cases", "case_id"),
        ("reporting", "summary", "total"),
    ]

    def test_schemas_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert sorted(result.schemas) == ["public", "reporting"]

    def test_tables_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        table_pairs = {(t.schema, t.table) for t in result.tables}
        assert ("public", "accounts") in table_pairs
        assert ("public", "cases") in table_pairs
        assert ("reporting", "summary") in table_pairs

    def test_columns_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        col_triples = {(c.schema, c.table, c.column) for c in result.columns}
        assert ("public", "accounts", "id") in col_triples
        assert ("public", "accounts", "name") in col_triples
        assert ("reporting", "summary", "total") in col_triples

    def test_estimated_row_counts_is_none(self):
        """PostgreSQL discovery does not return row counts in Sprint 10."""
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert result.estimated_row_counts is None

    def test_returns_schema_discovery_result_type(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_postgresql(conn)
        assert isinstance(result, SchemaDiscoveryResult)

    def test_empty_catalogue_returns_empty_result(self):
        conn = _mock_conn([])
        result = discover_schema_postgresql(conn)
        assert result.schemas == []
        assert result.tables == []
        assert result.columns == []

    def test_cursor_closed_after_query(self):
        """Cursor must be closed even if the query succeeds."""
        conn = _mock_conn(self.SAMPLE_ROWS)
        discover_schema_postgresql(conn)
        conn.cursor.return_value.close.assert_called_once()


# ---------------------------------------------------------------------------
# quote_identifier
# ---------------------------------------------------------------------------

class TestQuoteIdentifier:
    def test_simple_name(self):
        assert quote_identifier("public") == '"public"'

    def test_name_with_spaces(self):
        assert quote_identifier("my schema") == '"my schema"'

    def test_name_with_embedded_quote(self):
        # " inside a name must be doubled per ANSI SQL convention
        assert quote_identifier('odd"name') == '"odd""name"'

    def test_table_reference_style(self):
        schema = quote_identifier("public")
        table = quote_identifier("accounts")
        assert f"{schema}.{table}" == '"public"."accounts"'


# ---------------------------------------------------------------------------
# Live integration marker (skipped unless POSTGRES_LIVE=1)
# ---------------------------------------------------------------------------

LIVE = pytest.mark.skipif(
    __import__("os").environ.get("POSTGRES_LIVE") != "1",
    reason="Set POSTGRES_LIVE=1 and configure POSTGRES_* env vars to run live tests",
)


@LIVE
class TestLiveSchemaDiscovery:
    """Requires reachable PostgreSQL database. Skip by default."""

    def test_live_catalogue_query_returns_rows(self):
        import os
        cfg = DBConnectorConfig(
            connector_id="postgresql",
            org_id="live_test",
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ["POSTGRES_DATABASE"],
            driver="psycopg2",
            username_key="POSTGRES_USERNAME",
            password_key="POSTGRES_PASSWORD",
        )
        username = os.environ["POSTGRES_USERNAME"]
        password = os.environ["POSTGRES_PASSWORD"]
        conn = create_postgresql_connection(cfg, username, password)
        try:
            result = discover_schema_postgresql(conn)
            assert isinstance(result, SchemaDiscoveryResult)
            # At minimum the catalogue query must execute without error
        finally:
            conn.close()
