"""
Integration smoke tests for the Oracle DB driver — T2-S10-A Task T4.

These tests do NOT require a live Oracle DB or Oracle Instant Client 21.
They mock oracledb.connect and verify:
  - The exact catalogue query from the spec is issued unchanged.
  - Both TNS and direct (EZConnect) connection formats are detected and built.
  - connect_timeout_s  → tcp_connect_timeout keyword argument.
  - query_timeout_s    → conn.callTimeout in milliseconds.
  - Connection failures raise DBConnectionError with no credential details.
  - discover_schema_oracle() returns a well-formed SchemaDiscoveryResult.
  - quote_identifier() produces correct Oracle double-quote notation.
  - Trivial test query SELECT 1 FROM DUAL constant is correct.

A separate live-integration marker is provided for environments that have
Oracle Instant Client 21 and a reachable Oracle DB instance.
Those tests are skipped by default.
"""

from __future__ import annotations

import oracledb
import pytest
from unittest.mock import MagicMock, call, patch

from backend.app.db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
)
from backend.connectors.db.oracle import (
    CATALOGUE_QUERY,
    TRIVIAL_QUERY,
    _connect_kwargs,
    _is_tns,
    create_oracle_connection,
    discover_schema_oracle,
    quote_identifier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(
    host: str = "oracle.example.com",
    port: int = 1521,
    database: str = "ORCLPDB1",
    connect_timeout_s: int = 10,
    query_timeout_s: int = 30,
) -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id="oracle_db",
        org_id="acme",
        host=host,
        port=port,
        database=database,
        driver="oracledb",
        username_key="ORACLE_USERNAME",
        password_key="ORACLE_PASSWORD",
        connect_timeout_s=connect_timeout_s,
        query_timeout_s=query_timeout_s,
    )


def _tns_config() -> DBConnectorConfig:
    tns = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)"
        "(HOST=oracle.example.com)(PORT=1521))"
        "(CONNECT_DATA=(SERVICE_NAME=ORCLPDB1)))"
    )
    return _config(database=tns)


def _mock_cursor(rows: list[tuple]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


def _mock_conn(rows: list[tuple] | None = None) -> MagicMock:
    conn = MagicMock(spec=oracledb.Connection)
    conn.cursor.return_value = _mock_cursor(rows or [])
    return conn


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_catalogue_query_matches_spec(self):
        expected = (
            "SELECT OWNER, TABLE_NAME, COLUMN_NAME "
            "FROM ALL_COLUMNS "
            "WHERE OWNER NOT IN ('SYS', 'SYSTEM', 'INFORMATION_SCHEMA')"
        )
        assert CATALOGUE_QUERY == expected

    def test_catalogue_query_is_select(self):
        assert CATALOGUE_QUERY.strip().upper().startswith("SELECT")

    def test_catalogue_query_excludes_sys(self):
        assert "'SYS'" in CATALOGUE_QUERY

    def test_catalogue_query_excludes_system(self):
        assert "'SYSTEM'" in CATALOGUE_QUERY

    def test_catalogue_query_uses_all_columns(self):
        assert "ALL_COLUMNS" in CATALOGUE_QUERY

    def test_trivial_query_is_select_from_dual(self):
        assert TRIVIAL_QUERY == "SELECT 1 FROM DUAL"


# ---------------------------------------------------------------------------
# Connection format detection
# ---------------------------------------------------------------------------

class TestConnectionFormat:
    def test_plain_service_name_is_not_tns(self):
        assert _is_tns("ORCLPDB1") is False

    def test_tns_descriptor_detected(self):
        tns = "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=h)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=s)))"
        assert _is_tns(tns) is True

    def test_tns_with_leading_whitespace_detected(self):
        assert _is_tns("  (DESCRIPTION=...)") is True

    def test_direct_format_kwargs_uses_host_port_service(self):
        cfg = _config(host="db.corp.local", port=1521, database="FINPDB")
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["host"] == "db.corp.local"
        assert kwargs["port"] == 1521
        assert kwargs["service_name"] == "FINPDB"
        assert "dsn" not in kwargs

    def test_tns_format_kwargs_uses_dsn_verbatim(self):
        tns = "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=h)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=s)))"
        cfg = _config(database=tns)
        kwargs = _connect_kwargs(cfg, "user", "pass")
        assert kwargs["dsn"] == tns.strip()
        assert "host" not in kwargs
        assert "port" not in kwargs
        assert "service_name" not in kwargs

    def test_direct_format_connect_uses_separate_params(self):
        cfg = _config()
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn) as mock_connect:
            create_oracle_connection(cfg, "user", "pass")
        _, kwargs = mock_connect.call_args
        assert "service_name" in kwargs or kwargs.get("service_name") == cfg.database
        assert "dsn" not in kwargs

    def test_tns_format_connect_passes_dsn(self):
        cfg = _tns_config()
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn) as mock_connect:
            create_oracle_connection(cfg, "user", "pass")
        _, kwargs = mock_connect.call_args
        assert "dsn" in kwargs
        assert kwargs["dsn"].startswith("(DESCRIPTION=")


# ---------------------------------------------------------------------------
# Timeout application
# ---------------------------------------------------------------------------

class TestTimeouts:
    def test_connect_timeout_passed_to_oracledb_connect(self):
        cfg = _config(connect_timeout_s=15)
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn) as mock_connect:
            create_oracle_connection(cfg, "user", "pass")
        _, kwargs = mock_connect.call_args
        assert kwargs.get("tcp_connect_timeout") == 15

    def test_query_timeout_set_as_call_timeout_in_ms(self):
        """callTimeout must be query_timeout_s converted to milliseconds."""
        cfg = _config(query_timeout_s=45)
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn):
            conn = create_oracle_connection(cfg, "user", "pass")
        assert conn.callTimeout == 45 * 1000

    def test_connect_and_query_timeouts_are_independent(self):
        cfg = _config(connect_timeout_s=7, query_timeout_s=60)
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn) as mock_connect:
            conn = create_oracle_connection(cfg, "user", "pass")
        _, kwargs = mock_connect.call_args
        assert kwargs.get("tcp_connect_timeout") == 7
        assert conn.callTimeout == 60 * 1000

    def test_default_timeout_values(self):
        cfg = _config()  # defaults: connect=10, query=30
        mock_conn = _mock_conn()
        with patch("backend.connectors.db.oracle.oracledb.connect", return_value=mock_conn) as mock_connect:
            conn = create_oracle_connection(cfg, "user", "pass")
        _, kwargs = mock_connect.call_args
        assert kwargs.get("tcp_connect_timeout") == 10
        assert conn.callTimeout == 30 * 1000


# ---------------------------------------------------------------------------
# Credential isolation
# ---------------------------------------------------------------------------

class TestCredentialIsolation:
    def test_connection_failure_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.oracle.oracledb.connect",
            side_effect=oracledb.DatabaseError("ORA-12541: no listener"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_oracle_connection(cfg, "secret_user", "secret_pass")
        assert exc_info.value.error_code == "connection_failed"

    def test_error_message_contains_no_credentials(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.oracle.oracledb.connect",
            side_effect=oracledb.DatabaseError("ORA-12541"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_oracle_connection(cfg, "secret_user", "secret_pass")
        msg = str(exc_info.value)
        assert "secret_user" not in msg
        assert "secret_pass" not in msg
        assert "password" not in msg.lower()

    def test_error_message_contains_host_and_service(self):
        cfg = _config(host="prod-oracle.corp.local", database="FINPDB")
        with patch(
            "backend.connectors.db.oracle.oracledb.connect",
            side_effect=oracledb.DatabaseError("ORA-12541"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_oracle_connection(cfg, "user", "pass")
        msg = str(exc_info.value)
        assert "prod-oracle.corp.local" in msg
        assert "FINPDB" in msg

    def test_unexpected_exception_raises_db_connection_error(self):
        cfg = _config()
        with patch(
            "backend.connectors.db.oracle.oracledb.connect",
            side_effect=RuntimeError("network failure"),
        ):
            with pytest.raises(DBConnectionError) as exc_info:
                create_oracle_connection(cfg, "user", "pass")
        assert exc_info.value.error_code == "connection_failed"

    def test_connect_kwargs_contains_credentials(self):
        """_connect_kwargs includes user/password — must never be logged."""
        cfg = _config()
        kwargs = _connect_kwargs(cfg, "db_user", "db_pass")
        assert kwargs["user"] == "db_user"
        assert kwargs["password"] == "db_pass"


# ---------------------------------------------------------------------------
# discover_schema_oracle — catalogue query execution and result shape
# ---------------------------------------------------------------------------

class TestSchemaDiscovery:
    SAMPLE_ROWS = [
        ("HR", "EMPLOYEES", "EMPLOYEE_ID"),
        ("HR", "EMPLOYEES", "FIRST_NAME"),
        ("HR", "DEPARTMENTS", "DEPARTMENT_ID"),
        ("FINANCE", "LEDGER", "AMOUNT"),
    ]

    def test_catalogue_query_issued_to_cursor(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        discover_schema_oracle(conn)
        conn.cursor.return_value.execute.assert_called_once_with(CATALOGUE_QUERY)

    def test_schemas_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_oracle(conn)
        assert sorted(result.schemas) == ["FINANCE", "HR"]

    def test_tables_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_oracle(conn)
        pairs = {(t.schema, t.table) for t in result.tables}
        assert ("HR", "EMPLOYEES") in pairs
        assert ("HR", "DEPARTMENTS") in pairs
        assert ("FINANCE", "LEDGER") in pairs

    def test_columns_extracted_correctly(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_oracle(conn)
        triples = {(c.schema, c.table, c.column) for c in result.columns}
        assert ("HR", "EMPLOYEES", "EMPLOYEE_ID") in triples
        assert ("HR", "EMPLOYEES", "FIRST_NAME") in triples
        assert ("FINANCE", "LEDGER", "AMOUNT") in triples

    def test_estimated_row_counts_is_none(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_oracle(conn)
        assert result.estimated_row_counts is None

    def test_returns_schema_discovery_result_type(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        result = discover_schema_oracle(conn)
        assert isinstance(result, SchemaDiscoveryResult)

    def test_empty_catalogue_returns_empty_result(self):
        conn = _mock_conn([])
        result = discover_schema_oracle(conn)
        assert result.schemas == []
        assert result.tables == []
        assert result.columns == []

    def test_cursor_closed_after_query(self):
        conn = _mock_conn(self.SAMPLE_ROWS)
        discover_schema_oracle(conn)
        conn.cursor.return_value.close.assert_called_once()


# ---------------------------------------------------------------------------
# quote_identifier
# ---------------------------------------------------------------------------

class TestQuoteIdentifier:
    def test_simple_schema_name(self):
        assert quote_identifier("HR") == '"HR"'

    def test_lowercase_name(self):
        assert quote_identifier("hr") == '"hr"'

    def test_name_with_spaces(self):
        assert quote_identifier("my schema") == '"my schema"'

    def test_name_with_embedded_double_quote(self):
        assert quote_identifier('odd"name') == '"odd""name"'

    def test_schema_table_reference_style(self):
        schema = quote_identifier("HR")
        table = quote_identifier("EMPLOYEES")
        assert f"{schema}.{table}" == '"HR"."EMPLOYEES"'


# ---------------------------------------------------------------------------
# Live integration marker (skipped unless ORACLE_LIVE=1)
# ---------------------------------------------------------------------------

LIVE = pytest.mark.skipif(
    __import__("os").environ.get("ORACLE_LIVE") != "1",
    reason="Set ORACLE_LIVE=1 and configure ORACLE_* env vars to run live tests",
)


@LIVE
class TestLiveSchemaDiscovery:
    """Requires Oracle Instant Client 21 + reachable Oracle DB. Skip by default."""

    def test_live_catalogue_query_returns_rows(self):
        import os
        from backend.connectors.db.oracle import init_thick_mode
        init_thick_mode()  # activate thick mode if Oracle Instant Client is present
        cfg = DBConnectorConfig(
            connector_id="oracle_db",
            org_id="live_test",
            host=os.environ["ORACLE_HOST"],
            port=int(os.environ.get("ORACLE_PORT", "1521")),
            database=os.environ["ORACLE_SERVICE"],
            driver="oracledb",
            username_key="ORACLE_USERNAME",
            password_key="ORACLE_PASSWORD",
        )
        username = os.environ["ORACLE_USERNAME"]
        password = os.environ["ORACLE_PASSWORD"]
        conn = create_oracle_connection(cfg, username, password)
        try:
            result = discover_schema_oracle(conn)
            assert isinstance(result, SchemaDiscoveryResult)
        finally:
            conn.close()

    def test_live_trivial_query_executes(self):
        import os
        cfg = DBConnectorConfig(
            connector_id="oracle_db",
            org_id="live_test",
            host=os.environ["ORACLE_HOST"],
            port=int(os.environ.get("ORACLE_PORT", "1521")),
            database=os.environ["ORACLE_SERVICE"],
            driver="oracledb",
            username_key="ORACLE_USERNAME",
            password_key="ORACLE_PASSWORD",
        )
        username = os.environ["ORACLE_USERNAME"]
        password = os.environ["ORACLE_PASSWORD"]
        conn = create_oracle_connection(cfg, username, password)
        try:
            cursor = conn.cursor()
            cursor.execute(TRIVIAL_QUERY)
            row = cursor.fetchone()
            assert row[0] == 1
        finally:
            conn.close()
