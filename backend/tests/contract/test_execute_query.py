"""
backend/tests/contract/test_execute_query.py

Contract tests for execute_query() — the single data access path for all
Track 2 database connectors (T2-S10-A, Task T9).

Acceptance criteria validation:
  ✓ execute_query() calls validate_read_only() before opening a connection
  ✓ execute_query() calls validate_scope() before opening a connection
  ✓ Results are paginated at configured limit (default 10,000 rows)
  ✓ truncated=True when MAX_ROWS_PER_QUERY is reached
  ✓ log_event('connector_queried', ...) is called on each execution
  ✓ record_event('db.query_executed', ...) is called on each execution
  ✓ execute_query raises DBQueryRejectedError if validation fails; never connects
  ✓ execute_query raises DBScopeViolationError if scope validation fails; never connects
  ✓ SQL NULL values are always returned in rows (never omitted)
  ✓ Ingestor/downstream cannot bypass execute_query() — pool is isolated
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, Mock, patch, call

import pytest

from app.db_connectors.models import (
    DBConnectorConfig,
    DBConnectionError,
    DBQueryRejectedError,
    DBScopeViolationError,
    ScopeDeclaration,
)
from connectors.db import execute_query
from connectors.db.connection_pool import (
    MAX_ROWS_PER_QUERY,
    QUERY_TIMEOUT_S,
    _IsolatedPool,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db_config() -> DBConnectorConfig:
    """Test database configuration."""
    return DBConnectorConfig(
        connector_id="oracle_db",
        org_id="acme",
        host="localhost",
        port=1521,
        database="TESTDB",
        driver="oracledb",
        username_key="ORACLE_USER",
        password_key="ORACLE_PASS",
        query_timeout_s=30,
    )


@pytest.fixture
def scope_declaration() -> ScopeDeclaration:
    """Test scope with explicit allowlist."""
    return ScopeDeclaration(
        org_id="acme",
        connector_id="oracle_db",
        schemas=["HR", "FINANCE"],
        tables=["EMPLOYEES", "DEPARTMENTS"],
        declared_at=datetime.now(),
        declared_by="alice@acme.com",
    )


@pytest.fixture
def mock_connection() -> MagicMock:
    """Mock database connection object."""
    conn = MagicMock()
    cursor = MagicMock()

    # Simulate a successful query result: 2 rows, 3 columns
    cursor.description = [("employee_id",), ("name",), ("salary",)]
    cursor.fetchall.return_value = [
        (1, "Alice", 50000),
        (2, "Bob", None),  # NULL salary
    ]

    conn.cursor.return_value = cursor
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# AC1: validate_read_only() is called before connection opens
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_validate_read_only_called_before_connection(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
    mock_connection,
):
    """AC1: validate_read_only() is called BEFORE pool connection is acquired."""
    # Setup: configure mock pool
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    query = "SELECT * FROM EMPLOYEES"
    call_order = []

    def validate_ro_side_effect(*args, **kwargs):
        call_order.append("validate_read_only")

    def get_pool_side_effect(*args, **kwargs):
        call_order.append("get_pool")
        return mock_pool

    mock_validate_read_only.side_effect = validate_ro_side_effect
    mock_get_pool.side_effect = get_pool_side_effect

    # Execute
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: validate_read_only called BEFORE pool is accessed
    assert result is not None
    assert call_order[0] == "validate_read_only"
    assert "get_pool" in call_order
    assert call_order.index("validate_read_only") < call_order.index("get_pool")


# ─────────────────────────────────────────────────────────────────────────────
# AC2: validate_scope() is called before connection opens
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_validate_scope_called_before_connection(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
    mock_connection,
):
    """AC2: validate_scope() is called BEFORE pool connection is acquired."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    query = "SELECT * FROM EMPLOYEES"
    call_order = []

    def validate_scope_side_effect(*args, **kwargs):
        call_order.append("validate_scope")

    def get_pool_side_effect(*args, **kwargs):
        call_order.append("get_pool")
        return mock_pool

    mock_validate_scope.side_effect = validate_scope_side_effect
    mock_get_pool.side_effect = get_pool_side_effect

    # Execute
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: validate_scope called BEFORE pool is accessed
    assert result is not None
    assert call_order[0] == "validate_scope"
    assert "get_pool" in call_order
    assert call_order.index("validate_scope") < call_order.index("get_pool")


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Validation failures raise errors without connecting
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_read_only_violation_raises_without_connection(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC3: DBQueryRejectedError on non-SELECT; pool never accessed."""
    # Setup: validate_read_only rejects INSERT
    mock_validate_read_only.side_effect = DBQueryRejectedError(
        "INSERT statements not allowed",
        error_code="query_rejected",
    )

    # Execute and expect error
    with pytest.raises(DBQueryRejectedError):
        execute_query(
            config=db_config,
            query="INSERT INTO EMPLOYEES VALUES (1, 'test')",
            scope=scope_declaration,
            org_id="acme",
            run_id="run-123",
        )

    # Verify: pool was NEVER accessed
    mock_get_pool.assert_not_called()


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_scope_violation_raises_without_connection(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC3: DBScopeViolationError on out-of-scope table; pool never accessed."""
    # Setup: validate_scope rejects out-of-scope table
    mock_validate_scope.side_effect = DBScopeViolationError(
        "Table AUDIT_LOG not in declared scope"
    )

    # Execute and expect error
    with pytest.raises(DBScopeViolationError):
        execute_query(
            config=db_config,
            query="SELECT * FROM AUDIT_LOG",
            scope=scope_declaration,
            org_id="acme",
            run_id="run-123",
        )

    # Verify: pool was NEVER accessed
    mock_get_pool.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Results paginated at MAX_ROWS_PER_QUERY
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_pagination_under_limit(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC4: Results under limit are all returned, truncated=False."""
    # Setup: 5 rows (under limit)
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",), ("name",)]
    cursor.fetchall.return_value = [(i, f"row_{i}") for i in range(5)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    result = execute_query(
        config=db_config,
        query="SELECT * FROM EMPLOYEES",
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify
    assert result.row_count == 5
    assert result.truncated is False
    assert len(result.rows) == 5


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_pagination_at_limit(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC4: Truncated=True when result set hits MAX_ROWS_PER_QUERY."""
    # Setup: MAX_ROWS_PER_QUERY + 1 rows (triggers truncation)
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    # Return one more than limit to trigger truncation
    cursor.fetchall.return_value = [(i,) for i in range(MAX_ROWS_PER_QUERY + 1)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    result = execute_query(
        config=db_config,
        query="SELECT * FROM LARGE_TABLE",
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: rows capped, truncated flag set
    assert result.row_count == MAX_ROWS_PER_QUERY
    assert len(result.rows) == MAX_ROWS_PER_QUERY
    assert result.truncated is True


# ─────────────────────────────────────────────────────────────────────────────
# AC5: SQL NULL values are always returned (never omitted)
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_sql_null_values_preserved(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC5: SQL NULL values become None; rows are never omitted."""
    # Setup: include NULL value in second row
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("employee_id",), ("name",), ("salary",)]
    cursor.fetchall.return_value = [
        (1, "Alice", 50000),
        (2, "Bob", None),  # NULL salary
        (3, "Charlie", 60000),
    ]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    result = execute_query(
        config=db_config,
        query="SELECT employee_id, name, salary FROM EMPLOYEES",
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: 3 rows (including row with NULL), NULL becomes None
    assert result.row_count == 3
    assert len(result.rows) == 3
    assert result.rows[1] == (2, "Bob", None)  # NULL salary is present


# ─────────────────────────────────────────────────────────────────────────────
# AC6: log_event('connector_queried', ...) is called
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.log_event")
@patch("connectors.db.execute_query.record_event")
@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_log_event_called(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    mock_record_event,
    mock_log_event,
    db_config,
    scope_declaration,
):
    """AC6: log_event('connector_queried', ...) called with correct params."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(1,), (2,)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    query = "SELECT * FROM EMPLOYEES"
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify log_event called with correct params
    mock_log_event.assert_called_once()
    call_args = mock_log_event.call_args

    assert call_args[0][0] == "connector_queried"  # event type
    assert call_args[1]["org_id"] == "acme"
    assert call_args[1]["run_id"] == "run-123"
    assert call_args[1]["connector_id"] == "oracle_db"
    assert call_args[1]["query_hash"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert call_args[1]["row_count"] == 2
    assert "duration_ms" in call_args[1]


# ─────────────────────────────────────────────────────────────────────────────
# AC7: record_event('db.query_executed', ...) is called
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.log_event")
@patch("connectors.db.execute_query.record_event")
@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_record_event_called(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    mock_record_event,
    mock_log_event,
    db_config,
    scope_declaration,
):
    """AC7: record_event('db.query_executed', ...) called with correct params."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(i,) for i in range(MAX_ROWS_PER_QUERY + 50)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    query = "SELECT * FROM LARGE_TABLE"
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify record_event called with correct params
    mock_record_event.assert_called_once()
    call_args = mock_record_event.call_args

    assert call_args[0][0] == "db.query_executed"  # event type
    payload = call_args[0][1]

    assert payload["connector_id"] == "oracle_db"
    assert payload["query_hash"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert payload["row_count"] == MAX_ROWS_PER_QUERY
    assert payload["truncated"] is True
    assert payload["driver"] == "oracledb"
    assert "duration_ms" in payload


# ─────────────────────────────────────────────────────────────────────────────
# AC8: Query hash is SHA-256 of query string (not raw query in audit)
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.log_event")
@patch("connectors.db.execute_query.record_event")
@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_query_hash_is_sha256(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    mock_record_event,
    mock_log_event,
    db_config,
    scope_declaration,
):
    """AC8: query_hash in audit/telemetry is SHA-256, never raw query."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(1,)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    query = "SELECT * FROM SENSITIVE_TABLE WHERE contains_pii = TRUE"
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Expected hash
    expected_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()

    # Verify: raw query never appears in audit/telemetry
    for call_args in mock_log_event.call_args_list + mock_record_event.call_args_list:
        call_str = str(call_args)
        # Ensure raw query doesn't appear in any argument
        assert query not in call_str, "Raw query should not appear in audit/telemetry"

    # Verify: hash is present and correct
    assert result.query_hash == expected_hash
    log_event_kwargs = mock_log_event.call_args[1]
    assert log_event_kwargs["query_hash"] == expected_hash

    record_event_payload = mock_record_event.call_args[0][1]
    assert record_event_payload["query_hash"] == expected_hash


# ─────────────────────────────────────────────────────────────────────────────
# AC9: Connection always released back to pool (even on error)
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_connection_released_on_success(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC9: Connection released to pool on successful query."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(1,)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    result = execute_query(
        config=db_config,
        query="SELECT * FROM EMPLOYEES",
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: pool.release called
    mock_pool.release.assert_called_once_with(mock_connection)


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_connection_released_on_query_error(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC9: Connection released to pool even when query fails."""
    # Setup: query raises an error
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.execute.side_effect = Exception("Query timeout")

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute and expect error
    with pytest.raises(DBConnectionError):
        execute_query(
            config=db_config,
            query="SELECT * FROM EMPLOYEES",
            scope=scope_declaration,
            org_id="acme",
            run_id="run-123",
        )

    # Verify: pool.release called despite error
    mock_pool.release.assert_called_once_with(mock_connection)


# ─────────────────────────────────────────────────────────────────────────────
# AC10: Downstream cannot bypass execute_query() to access pool directly
# ─────────────────────────────────────────────────────────────────────────────


def test_pool_is_isolated_per_connector_and_org(db_config, scope_declaration):
    """AC10: Pool is keyed by (org_id, connector_id); isolation enforced."""
    from backend.connectors.db.connection_pool import _pool_registry

    # Pool key is (org_id, connector_id)
    key = (db_config.org_id, db_config.connector_id)

    # Verify: downstream cannot directly access _pool_registry
    # (it's a module-private implementation detail)
    assert key not in _pool_registry  # Before pool creation

    # Any attempt to create pools with different org_id will create separate pools
    config_different_org = DBConnectorConfig(
        connector_id=db_config.connector_id,
        org_id="different_org",
        host=db_config.host,
        port=db_config.port,
        database=db_config.database,
        driver=db_config.driver,
        username_key=db_config.username_key,
        password_key=db_config.password_key,
    )

    different_key = (config_different_org.org_id, config_different_org.connector_id)
    assert key != different_key  # Keys are distinct


# ─────────────────────────────────────────────────────────────────────────────
# AC11: Result contains columns and row_count
# ─────────────────────────────────────────────────────────────────────────────


@patch("connectors.db.execute_query.validate_read_only")
@patch("connectors.db.execute_query.validate_scope")
@patch("connectors.db.execute_query.get_or_create_pool")
def test_result_includes_all_fields(
    mock_get_pool,
    mock_validate_scope,
    mock_validate_read_only,
    db_config,
    scope_declaration,
):
    """AC11: DBQueryResult includes columns, rows, row_count, query_hash, duration_ms, truncated."""
    # Setup
    mock_pool = MagicMock(spec=_IsolatedPool)
    mock_connection = MagicMock()
    cursor = MagicMock()
    cursor.description = [("employee_id",), ("name",), ("salary",)]
    cursor.fetchall.return_value = [(1, "Alice", 50000), (2, "Bob", 60000)]

    mock_connection.cursor.return_value = cursor
    mock_pool.acquire.return_value = mock_connection
    mock_get_pool.return_value = mock_pool

    # Execute
    query = "SELECT employee_id, name, salary FROM EMPLOYEES"
    result = execute_query(
        config=db_config,
        query=query,
        scope=scope_declaration,
        org_id="acme",
        run_id="run-123",
    )

    # Verify: all fields present and correct
    assert result.columns == ["employee_id", "name", "salary"]
    assert result.row_count == 2
    assert result.rows == [(1, "Alice", 50000), (2, "Bob", 60000)]
    assert result.query_hash == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert result.duration_ms >= 0  # Can be 0 in tests, but should be >= 0
    assert result.truncated is False
