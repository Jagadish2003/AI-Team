"""
backend/tests/contract/test_query_guard.py

Contract tests for the T2-S10-A query guard.

Covers acceptance criteria:
  AC2  — INSERT rejected by validate_read_only (sqlparse detection, not string match)
  AC3  — UPDATE rejected by validate_read_only (sqlparse detection)
  AC4  — Out-of-scope table rejected by validate_scope
  AC5  — Parse failure in table extraction → fail-closed (DBScopeViolationError)
  AC6  — Empty extraction on non-trivial query → fail-closed
         SELECT 1 / SELECT 1 FROM DUAL are exempt
  AC14 — scope.tables == [] enforces schema membership (not a bypass)

Additional coverage:
  - DELETE rejected by validate_read_only
  - DDL (CREATE, DROP, ALTER, TRUNCATE) rejected by validate_read_only
  - Valid SELECT passes validate_read_only
  - Valid scoped SELECT passes validate_scope
  - Schema-qualified reference honoured in explicit table list
  - Ambiguous / unqualified reference under empty tables list → rejected
  - validate_read_only and validate_scope are importable from package root
"""

from __future__ import annotations

import pytest
from datetime import datetime
from unittest.mock import patch

from connectors.db.query_guard import (
    validate_read_only,
    validate_scope,
    _extract_table_references,
)
from connectors.db.models import (
    DBQueryRejectedError,
    DBScopeViolationError,
    ScopeDeclaration,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scope(
    schemas: list[str],
    tables: list[str],
    org_id: str = "test_org",
    connector_id: str = "sqlserver",
) -> ScopeDeclaration:
    return ScopeDeclaration(
        org_id=org_id,
        connector_id=connector_id,
        schemas=schemas,
        tables=tables,
        declared_at=datetime(2026, 1, 1),
        declared_by="analyst@example.com",
    )


# ---------------------------------------------------------------------------
# validate_read_only — happy path
# ---------------------------------------------------------------------------


class TestValidateReadOnlyAccepted:
    """SELECT statements must pass validate_read_only."""

    def test_simple_select_passes(self):
        validate_read_only("SELECT id, name FROM users")

    def test_select_star_passes(self):
        validate_read_only("SELECT * FROM orders")

    def test_select_with_where_passes(self):
        validate_read_only("SELECT id FROM accounts WHERE status = 'active'")

    def test_select_with_join_passes(self):
        validate_read_only(
            "SELECT a.id, b.name FROM accounts a JOIN contacts b ON a.id = b.account_id"
        )

    def test_select_1_trivial_passes(self):
        """SELECT 1 is used for connection health checks."""
        validate_read_only("SELECT 1")

    def test_select_1_from_dual_trivial_passes(self):
        """Oracle health-check query must pass."""
        validate_read_only("SELECT 1 FROM DUAL")

    def test_select_subquery_passes(self):
        validate_read_only("SELECT id FROM (SELECT id FROM users WHERE active = 1) sub")

    def test_select_with_cte_passes(self):
        """CTE-based queries are SELECT statements and must not be rejected at
        the read-only stage (scope validation handles table references)."""
        validate_read_only(
            "WITH recent AS (SELECT id FROM orders WHERE created > '2025-01-01') "
            "SELECT * FROM recent"
        )

    def test_leading_whitespace_stripped(self):
        validate_read_only("   SELECT id FROM users   ")


# ---------------------------------------------------------------------------
# validate_read_only — AC2: INSERT rejected
# ---------------------------------------------------------------------------


class TestValidateReadOnlyInsert:
    """AC2: INSERT raises DBQueryRejectedError. Detection uses sqlparse."""

    def test_insert_values_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("INSERT INTO users (name) VALUES ('Alice')")

    def test_insert_select_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("INSERT INTO archive SELECT * FROM users")

    def test_insert_error_code(self):
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only("INSERT INTO users VALUES (1, 'x')")
        assert exc_info.value.error_code == "query_rejected"


# ---------------------------------------------------------------------------
# validate_read_only — AC3: UPDATE rejected
# ---------------------------------------------------------------------------


class TestValidateReadOnlyUpdate:
    """AC3: UPDATE raises DBQueryRejectedError. Detection uses sqlparse."""

    def test_update_set_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("UPDATE users SET name = 'Bob' WHERE id = 1")

    def test_update_error_code(self):
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only("UPDATE accounts SET status = 'closed'")
        assert exc_info.value.error_code == "query_rejected"


# ---------------------------------------------------------------------------
# validate_read_only — DELETE rejected
# ---------------------------------------------------------------------------


class TestValidateReadOnlyDelete:
    def test_delete_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("DELETE FROM users WHERE id = 5")

    def test_delete_error_code(self):
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only("DELETE FROM orders")
        assert exc_info.value.error_code == "query_rejected"


# ---------------------------------------------------------------------------
# validate_read_only — DDL rejected
# ---------------------------------------------------------------------------


class TestValidateReadOnlyDDL:
    """All DDL statements must be rejected."""

    def test_create_table_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("CREATE TABLE staging (id INT)")

    def test_drop_table_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("DROP TABLE users")

    def test_alter_table_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("ALTER TABLE users ADD COLUMN email VARCHAR(255)")

    def test_truncate_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("TRUNCATE TABLE orders")

    def test_create_index_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("CREATE INDEX idx_name ON users(name)")


# ---------------------------------------------------------------------------
# validate_read_only — empty / blank query rejected
# ---------------------------------------------------------------------------


class TestValidateReadOnlyEdgeCases:
    def test_empty_string_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("   \t\n  ")


# ---------------------------------------------------------------------------
# validate_read_only — uses sqlparse, not string matching
# ---------------------------------------------------------------------------


class TestValidateReadOnlyUsesParser:
    """
    Demonstrate that detection goes through sqlparse statement type,
    not naive prefix matching.
    """

    def test_insert_detected_via_sqlparse(self):
        """sqlparse must detect the statement type, not a startswith check."""
        # Even with leading comment, sqlparse still identifies INSERT
        query = "/* audit */ INSERT INTO log VALUES (1)"
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only(query)
        # The error message references the detected type, not a substring match
        assert "INSERT" in str(exc_info.value) or "query_rejected" == exc_info.value.error_code

    def test_update_detected_via_sqlparse(self):
        query = "-- comment\nUPDATE users SET active = 0"
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(query)


# ---------------------------------------------------------------------------
# validate_scope — AC4: out-of-scope table rejected
# ---------------------------------------------------------------------------


class TestValidateScopeOutOfScope:
    """AC4: Table not in ScopeDeclaration raises DBScopeViolationError."""

    def test_unknown_table_rejected(self):
        scope = _scope(schemas=["dbo"], tables=["dbo.orders"])
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT * FROM dbo.users", scope)

    def test_multiple_tables_one_out_of_scope_rejected(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                "SELECT o.id, u.name FROM orders o JOIN users u ON o.user_id = u.id",
                scope,
            )

    def test_correct_table_passes(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        # Should not raise
        validate_scope("SELECT id, amount FROM orders", scope)

    def test_schema_qualified_correct_table_passes(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        validate_scope("SELECT id FROM dbo.orders", scope)

    def test_schema_not_in_declared_schemas_rejected(self):
        """Even if table name matches, wrong schema → violation."""
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT id FROM staging.orders", scope)


# ---------------------------------------------------------------------------
# validate_scope — AC5: parse failure → fail-closed
# ---------------------------------------------------------------------------


class TestValidateScopeFailClosedOnParseFailure:
    """AC5: Any exception during table extraction → DBScopeViolationError."""

    def test_extraction_exception_causes_scope_violation(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with patch(
            "connectors.db.query_guard._extract_table_references",
            side_effect=RuntimeError("simulated parse failure"),
        ):
            with pytest.raises(DBScopeViolationError) as exc_info:
                validate_scope("SELECT id FROM orders", scope)
        assert "fail-closed" in str(exc_info.value).lower()

    def test_value_error_during_extraction_causes_scope_violation(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with patch(
            "connectors.db.query_guard._extract_table_references",
            side_effect=ValueError("dialect ambiguity"),
        ):
            with pytest.raises(DBScopeViolationError):
                validate_scope("SELECT id FROM orders", scope)

    def test_query_not_executed_on_extraction_failure(self):
        """The fail-closed path must not allow the query through."""
        scope = _scope(schemas=["dbo"], tables=["orders"])
        executed = []
        with patch(
            "connectors.db.query_guard._extract_table_references",
            side_effect=Exception("parse error"),
        ):
            with pytest.raises(DBScopeViolationError):
                validate_scope("SELECT id FROM orders", scope)
                executed.append(True)  # never reached
        assert not executed


# ---------------------------------------------------------------------------
# validate_scope — AC6: empty extraction on non-trivial query → fail-closed
# ---------------------------------------------------------------------------


class TestValidateScopeFailClosedOnEmptyExtraction:
    """AC6: Empty table set on non-trivial query → DBScopeViolationError."""

    def test_empty_extraction_raises_scope_violation(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with patch(
            "connectors.db.query_guard._extract_table_references",
            return_value=set(),
        ):
            with pytest.raises(DBScopeViolationError) as exc_info:
                validate_scope("SELECT id FROM orders", scope)
        assert "fail-closed" in str(exc_info.value).lower()

    def test_select_1_exempt_from_extraction(self):
        """AC6: SELECT 1 is a trivial query and must NOT trigger fail-closed."""
        scope = _scope(schemas=["dbo"], tables=[])
        # Should not raise even though there are no table references
        validate_scope("SELECT 1", scope)

    def test_select_1_from_dual_exempt(self):
        """AC6: SELECT 1 FROM DUAL (Oracle health check) is exempt."""
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("SELECT 1 FROM DUAL", scope)

    def test_trivial_query_case_insensitive(self):
        """Trivial query exemption must be case-insensitive."""
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("select 1", scope)
        validate_scope("select 1 from dual", scope)


# ---------------------------------------------------------------------------
# validate_scope — AC14: scope.tables == [] enforces schema membership
# ---------------------------------------------------------------------------


class TestValidateScopeEmptyTablesEnforcesSchema:
    """
    AC14: scope.tables == [] is NOT a bypass.
    Any table in the declared schemas is permitted, but schema membership
    must be verifiable — i.e. references must be schema-qualified.
    """

    def test_schema_qualified_table_in_declared_schema_passes(self):
        """dbo.orders is in schema dbo → allowed."""
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("SELECT id FROM dbo.orders", scope)

    def test_schema_qualified_table_not_in_declared_schema_rejected(self):
        """staging.orders — schema 'staging' not declared → violation."""
        scope = _scope(schemas=["dbo"], tables=[])
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT id FROM staging.orders", scope)

    def test_unqualified_table_rejected_when_tables_empty(self):
        """
        Unqualified reference with scope.tables == [] must be rejected.
        Schema membership cannot be determined without a live DB lookup.
        Fail-closed: reject rather than guess.
        """
        scope = _scope(schemas=["dbo"], tables=[])
        with pytest.raises(DBScopeViolationError) as exc_info:
            validate_scope("SELECT id FROM orders", scope)
        # Error should mention schema verification
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg or "scope" in error_msg

    def test_multiple_schema_qualified_tables_all_in_scope_passes(self):
        scope = _scope(schemas=["dbo", "reporting"], tables=[])
        validate_scope(
            "SELECT a.id, b.total FROM dbo.accounts a JOIN reporting.summaries b ON a.id = b.account_id",
            scope,
        )

    def test_mixed_qualified_unqualified_rejected(self):
        """Even if one reference is valid, an unqualified one causes rejection."""
        scope = _scope(schemas=["dbo"], tables=[])
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                "SELECT a.id, o.total FROM dbo.accounts a JOIN orders o ON a.id = o.account_id",
                scope,
            )

    def test_scope_tables_empty_does_not_allow_any_unqualified_table(self):
        """
        Confirm empty tables list is strictly NOT a scope bypass.
        If it were a bypass, any unqualified reference would pass.
        """
        scope = _scope(schemas=["dbo"], tables=[])
        # This would pass if empty list bypassed scope — it must NOT
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT secret FROM admin_table", scope)


# ---------------------------------------------------------------------------
# validate_scope — multi-table queries
# ---------------------------------------------------------------------------


class TestValidateScopeMultiTable:
    def test_join_both_tables_in_scope_passes(self):
        scope = _scope(schemas=["dbo"], tables=["orders", "customers"])
        validate_scope(
            "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
            scope,
        )

    def test_join_one_table_out_of_scope_rejected(self):
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
                scope,
            )

    def test_subquery_table_out_of_scope_rejected(self):
        """Tables in subqueries are also subject to scope enforcement."""
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                "SELECT id FROM orders WHERE id IN (SELECT order_id FROM payments)",
                scope,
            )

    def test_subquery_table_in_scope_passes(self):
        scope = _scope(schemas=["dbo"], tables=["orders", "payments"])
        validate_scope(
            "SELECT id FROM orders WHERE id IN (SELECT order_id FROM payments)",
            scope,
        )


# ---------------------------------------------------------------------------
# Both functions fail-closed — general principle
# ---------------------------------------------------------------------------


class TestFailClosedGeneral:
    """Both functions must reject on any uncertainty, never silently allow."""

    def test_validate_read_only_unknown_type_rejected(self):
        """A statement sqlparse returns None for must be rejected."""
        # sqlparse may return None for certain malformed statements
        with patch("sqlparse.parse") as mock_parse:
            mock_stmt = mock_parse.return_value = [type("S", (), {"get_type": lambda self: None})()]
            with pytest.raises(DBQueryRejectedError):
                validate_read_only("SOME UNKNOWN STATEMENT")

    def test_validate_scope_preserves_fail_closed_on_exception(self):
        """Any exception path in validate_scope must raise DBScopeViolationError."""
        scope = _scope(schemas=["dbo"], tables=["orders"])
        with patch(
            "connectors.db.query_guard._extract_table_references",
            side_effect=MemoryError("OOM"),
        ):
            with pytest.raises(DBScopeViolationError):
                validate_scope("SELECT id FROM orders", scope)


# ---------------------------------------------------------------------------
# Package importability — AC21
# ---------------------------------------------------------------------------


class TestPackageImports:
    """AC21: Public API is importable from backend.connectors.db."""

    def test_validate_read_only_importable(self):
        from connectors.db import validate_read_only as vro
        assert callable(vro)

    def test_validate_scope_importable(self):
        from connectors.db import validate_scope as vs
        assert callable(vs)

    def test_exception_classes_importable(self):
        from connectors.db import DBQueryRejectedError, DBScopeViolationError
        assert issubclass(DBQueryRejectedError, Exception)
        assert issubclass(DBScopeViolationError, Exception)

    def test_data_models_importable(self):
        from connectors.db import (
            DBConnectorConfig,
            ScopeDeclaration,
            DBQueryResult,
            SchemaDiscoveryResult,
        )
        assert DBConnectorConfig is not None
        assert ScopeDeclaration is not None

    def test_no_circular_imports(self):
        """Import chain must complete without circular import errors."""
        import importlib
        importlib.import_module("connectors.db")
        importlib.import_module("connectors.db.models")
        importlib.import_module("connectors.db.query_guard")


# ---------------------------------------------------------------------------
# _extract_table_references — unit tests
# ---------------------------------------------------------------------------


class TestExtractTableReferences:
    """Unit tests for the internal extraction helper."""

    def test_simple_from(self):
        refs = _extract_table_references("SELECT id FROM users")
        assert "users" in refs

    def test_schema_qualified(self):
        refs = _extract_table_references("SELECT id FROM dbo.orders")
        assert "dbo.orders" in refs

    def test_join_extracts_both_tables(self):
        refs = _extract_table_references(
            "SELECT a.id, b.name FROM accounts a JOIN contacts b ON a.id = b.account_id"
        )
        assert "accounts" in refs
        assert "contacts" in refs

    def test_subquery_table_extracted(self):
        refs = _extract_table_references(
            "SELECT id FROM orders WHERE id IN (SELECT order_id FROM payments)"
        )
        assert "orders" in refs
        assert "payments" in refs

    def test_select_1_returns_empty(self):
        """SELECT 1 has no table references."""
        refs = _extract_table_references("SELECT 1")
        assert refs == set()

    def test_multiple_joins(self):
        refs = _extract_table_references(
            "SELECT a.id, b.name, c.total "
            "FROM accounts a "
            "LEFT JOIN contacts b ON a.id = b.account_id "
            "INNER JOIN invoices c ON a.id = c.account_id"
        )
        assert "accounts" in refs
        assert "contacts" in refs
        assert "invoices" in refs
