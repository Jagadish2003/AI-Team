"""
Integration smoke tests for the SQL Server driver — T2-S10-A Task T3.

These tests do NOT require a live SQL Server or ODBC Driver 18.  They mock
pyodbc.connect and verify:
  - The exact catalogue query from the spec is issued unchanged.
  - SSL is enforced in every connection string (Encrypt=yes,
    TrustServerCertificate=no).
  - connect_timeout_s and query_timeout_s are applied correctly.
  - Connection failures raise DBConnectionError with no credential details.
  - discover_schema_sqlserver() returns a well-formed SchemaDiscoveryResult.
  - quote_identifier() produces correct SQL Server bracket notation.

A separate live-integration marker is provided for environments that have
ODBC Driver 18 installed and a reachable SQL Server.  Those tests are skipped
by default.
"""

from __future__ import annotations

import pyodbc
import pytest
from unittest.mock import MagicMock, patch, call

from backend.app.db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
)
from backend.connectors.db.sqlserver import (
    CATALOGUE_QUERY,
    ODBC_DRIVER,
    create_sqlserver_connection,
    discover_schema_sqlserver,
    quote_identifier,
    _build_connection_string,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(
    host: str = "sqlserver.example.com",
    port: int = 1433,
    database: str = "EnterpriseDB",
    connect_timeout_s: int = 10,
    query_timeout_s: int = 30,
) -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id="sqlserver",
        org_id="acme",
        host=host,
        port=port,
        database=database,
        driver="pyodbc",
        username_key="SQLSERVER_USERNAME",
        password_key="SQLSERVER_PASSWORD",
        connect_timeout_s=connect_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def _mock_cursor(rows: list[tuple]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


def _mock_conn(rows: list[tuple] | None = None) -> MagicMock:
    conn = MagicMock(spec=pyodbc.Connection)
    conn.cursor.return_value = _mock_cursor(rows or [])
    return conn


# ---------------------------------------------------------------------------
# Catalogue query correctness
# ---------------------------------------------------------------------------

class TestCatalogueQuery:
    def test_catalogue_query_matches_spec(self):
        """The exact catalogue SQL from the spec must be used unchanged."""
        expected = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA NOT IN ('sys', 'information_schema')"
        )
        assert CATALOGUE_QUERY == expected

    def test_catalogue_query_is_select(self):
        assert CATALOGUE_QUERY.strip().upper().startswith("SELECT")

    def test_catalogue_query_excludes_sys_schema(self):
        assert "'sys'" in CATALOGUE_QUERY

    def test_catalogue_query_excludes_information_schema(self):
        assert "'information_schema'" in CATALOGUE_QUERY

    def test_discover_schema_executes_catalogue_query(self):
        """discover_schema_sqlserver() must issue exactly the catalogue query."""
        conn = _mock_conn([])
        discover_schema_sqlserver(conn)
        conn.cursor.return_value.execute.assert_called_once_with(CATALOGUE_QUERY)


# ---------------------------------------------------------------------------
# SSL enforcement
# ---------------------------------------------------------------------------

class TestSSLEnforcement:
    def test_connection_string_contains_encrypt_yes(self):
        cfg = _config()
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "Encrypt=yes" in conn_str

    def test_connection_string_contains_trust_server_cert_no(self):
        cfg = _config()
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "TrustServerCertificate=no" in conn_str

    def test_ssl_params_present_regardless_of_ssl_mode_field(self):
        """SSL is always enforced — ssl_mode field on config is not used."""
        cfg = _config()
        cfg.ssl_mode = None
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "Encrypt=yes" in conn_str
        assert "TrustServerCertificate=no" in conn_str

    def test_odbc_driver_18_used(self):
        cfg = _config()
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert ODBC_DRIVER in conn_str
        assert "ODBC Driver 18 for SQL Server" in conn_str


# ---------------------------------------------------------------------------
# Timeout application
# ---------------------------------------------------------------------------

class TestTimeouts:
    def test_connect_timeout_in_connection_string(self):
        cfg = _config(connect_timeout_s=15)
        conn_str = _build_connection_string(cfg, "user", "pass")
        assert "ConnectTimeout=15" in conn_str

    def test_query_timeout_set_on_connection_object(self):
        """conn.timeout must be set to query_timeout_s after connect."""
        cfg = _config(query_timeout_s=45)
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.sqlserver.pyodbc.connect", return_value=mock_conn):
            conn = create_sqlserver_connection(cfg, "user", "pass")
        assert conn.timeout == 45

    def test_connect_timeout_and_query_timeout_are_independent(self):
        """The two timeouts are applied to different mechanisms."""
        cfg = _config(connect_timeout_s=5, query_timeout_s=60)
        conn_str = _build_connection_string(cfg, "user", "pass")
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.sqlserver.pyodbc.connect", return_value=mock_conn):
            conn = create_sqlserver_connection(cfg, "user", "pass")
        assert "ConnectTimeout=5" in conn_str
        assert conn.timeout == 60


# ---------------------------------------------------------------------------
# Connection string — no credential leakage
# ---------------------------------------------------------------------------

class TestCredentialIsolation:
    def test_connection_string_not_in_error_message(self):
        """DBConnectionError message must not contain the connection string."""
        cfg = _config()
        with patch(
            "backend.connectors.db.sqlserver.pyodbc.connect",
            side_effect=pyodbc.Error("08001", "server not reachable"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_sqlserver_connection(cfg, "secret_user", "secret_pass")

        msg = str(exc_info.value)
        assert "secret_user" not in msg
        assert "secret_pass" not in msg
        assert "PWD" not in msg
        assert "UID" not in msg

    def test_error_message_contains_host_and_db(self):
        """Error message must include host and database for diagnostics."""
        cfg = _config(host="prod-db.corp.local", database="FinanceDB")
        with patch(
            "backend.connectors.db.sqlserver.pyodbc.connect",
            side_effect=pyodbc.Error("08001", "server not reachable"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_sqlserver_connection(cfg, "user", "pass")

        msg = str(exc_info.value)
        assert "prod-db.corp.local" in msg
        assert "FinanceDB" in msg

    def test_connection_failure_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.sqlserver.pyodbc.connect",
            side_effect=pyodbc.Error("08001", "cannot connect"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_sqlserver_connection(cfg, "user", "pass")
        assert exc_info.value.error_code == "connection_failed"

    def test_unexpected_exception_also_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.sqlserver.pyodbc.connect",
            side_effect=RuntimeError("unexpected"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_sqlserver_connection(cfg, "user", "pass")
        assert exc_info.value.error_code == "connection_failed"


# ---------------------------------------------------------------------------
# discover_schema_sqlserver — result shape
# ---------------------------------------------------------------------------

class TestSchemaDiscovery:
    SAMPLE_ROWS = [
        ("dbo", "accounts", "id"),
        ("dbo", "accounts", "name"),
        ("dbo", "cases", "case_id"),
        ("reporting", "summary", "total"),
    ]

    def test_schemas_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_sqlserver(conn)
        assert sorted(result.schemas) == ["dbo", "reporting"]

    def test_tables_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_sqlserver(conn)
        table_pairs = {(t.schema, t.table) for t in result.tables}
        assert ("dbo", "accounts") in table_pairs
        assert ("dbo", "cases") in table_pairs
        assert ("reporting", "summary") in table_pairs

    def test_columns_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_sqlserver(conn)
        col_triples = {(c.schema, c.table, c.column) for c in result.columns}
        assert ("dbo", "accounts", "id") in col_triples
        assert ("dbo", "accounts", "name") in col_triples
        assert ("reporting", "summary", "total") in col_triples

    def test_estimated_row_counts_is_none(self):
        """SQL Server discovery does not return row counts in Sprint 10."""
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_sqlserver(conn)
        assert result.estimated_row_counts is None

    def test_returns_schema_discovery_result_type(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_sqlserver(conn)
        assert isinstance(result, SchemaDiscoveryResult)

    def test_empty_catalogue_returns_empty_result(self):
        conn = _mock_conn([])
        result = discover_schema_sqlserver(conn)
        assert result.schemas == []
        assert result.tables == []
        assert result.columns == []

    def test_cursor_closed_after_query(self):
        """Cursor must be closed even if the query succeeds."""
        conn = _mock_conn(self.SAMPLE_ROWS)
        discover_schema_sqlserver(conn)
        conn.cursor.return_value.close.assert_called_once()


# ---------------------------------------------------------------------------
# quote_identifier
# ---------------------------------------------------------------------------

class TestQuoteIdentifier:
    def test_simple_name(self):
        assert quote_identifier("dbo") == "[dbo]"

    def test_name_with_spaces(self):
        assert quote_identifier("my schema") == "[my schema]"

    def test_name_with_embedded_bracket(self):
        # ] inside a name must be doubled per SQL Server convention
        assert quote_identifier("odd]name") == "[odd]]name]"

    def test_table_reference_style(self):
        schema = quote_identifier("dbo")
        table = quote_identifier("accounts")
        assert f"{schema}.{table}" == "[dbo].[accounts]"


# ---------------------------------------------------------------------------
# Live integration marker (skipped unless SQLSERVER_LIVE=1)
# ---------------------------------------------------------------------------

LIVE = pytest.mark.skipif(
    __import__("os").environ.get("SQLSERVER_LIVE") != "1",
    reason="Set SQLSERVER_LIVE=1 and configure SQLSERVER_* env vars to run live tests",
)


@LIVE
class TestLiveSchemaDiscovery:
    """Requires ODBC Driver 18 + reachable SQL Server.  Skip by default."""

    def test_live_catalogue_query_returns_rows(self):
        import os
        cfg = DBConnectorConfig(
            connector_id="sqlserver",
            org_id="live_test",
            host=os.environ["SQLSERVER_HOST"],
            port=int(os.environ.get("SQLSERVER_PORT", "1433")),
            database=os.environ["SQLSERVER_DATABASE"],
            driver="pyodbc",
            username_key="SQLSERVER_USERNAME",
            password_key="SQLSERVER_PASSWORD",
        )
        username = os.environ["SQLSERVER_USERNAME"]
        password = os.environ["SQLSERVER_PASSWORD"]
        conn = create_sqlserver_connection(cfg, username, password)
        try:
            result = discover_schema_sqlserver(conn)
            assert isinstance(result, SchemaDiscoveryResult)
            # At minimum the catalogue query must execute without error
        finally:
            conn.close()
