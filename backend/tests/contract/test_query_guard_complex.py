"""
backend/tests/contract/test_query_guard_complex.py

Sprint 12 Platform Hardening — Task 5B
sqlparse CTE and synonym fail-closed tests.

Covers acceptance criteria from Task 5B:
  AC4  — CTE query where base table is outside declared scope →
          DBScopeViolationError.  The query must not execute.
  AC5  — CTE query where base table is inside declared scope → passes.
  AC6  — Oracle synonym query → DBScopeViolationError.
          Synonym resolution is documented as a known limitation.
  AC7  — SELECT 1 and SELECT 1 FROM DUAL continue to execute without error
          after CTE handling is added.
  (implied) Multi-statement queries → DBQueryRejectedError.

Design notes
------------
sqlparse extracts CTE alias names as regular Identifiers alongside the real
base tables.  For example:

    WITH recent AS (SELECT * FROM restricted_table ...) SELECT * FROM recent

_extract_table_references() returns {'recent', 'restricted_table'}.

After _extract_cte_aliases() filtering, only 'restricted_table' is subject
to scope enforcement.  If the base table is out of scope → DBScopeViolationError
(correct outcome, now for the right reason).

Edge case: if sqlparse returns only the alias and misses the base table entirely,
filtering yields {} → empty-extraction fail-closed rule fires → still rejected.
The guard cannot be bypassed via CTE aliasing.

Oracle synonym limitation: AgentIQ cannot resolve synonyms at query-parse time.
A synonym query whose alias is not in the declared scope is rejected.  If a
synonym alias IS declared in scope the query will pass — this is a known
limitation documented in query_guard.py.  Operators must declare only real
schema-qualified table names in scope declarations.
"""

from __future__ import annotations

import pytest
from datetime import datetime

from connectors.db.query_guard import (
    validate_read_only,
    validate_scope,
    _extract_table_references,
    _extract_cte_aliases,
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
# AC4 — CTE with out-of-scope base table → DBScopeViolationError
# ---------------------------------------------------------------------------


class TestCTEOutOfScope:
    """
    AC4: A CTE query where the base table is outside declared scope raises
    DBScopeViolationError and the query does not execute.

    sqlparse behaviour: _extract_table_references returns both the CTE alias
    (e.g. 'recent') and the real base table (e.g. 'restricted_table').
    After CTE alias filtering, only 'restricted_table' remains.
    That table is not in scope → DBScopeViolationError.

    The query is rejected.  This is the correct outcome.
    """

    def test_simple_cte_out_of_scope_raises(self):
        """CTE alias wraps an out-of-scope table — query must be rejected."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH recent AS ("
            "  SELECT * FROM restricted_table"
            "  WHERE created_date >= GETDATE() - 30"
            ") SELECT * FROM recent"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_base_table_schema_out_of_scope_raises(self):
        """CTE body references a table in an undeclared schema."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH t AS ("
            "  SELECT * FROM secret_schema.sensitive_data"
            ") SELECT * FROM t"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_query_does_not_execute_on_cte_scope_violation(self):
        """Confirm the query is rejected before any execution path is reached."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH cte AS (SELECT * FROM admin_secrets) SELECT * FROM cte"
        )
        executed = []
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)
            executed.append(True)   # never reached
        assert not executed

    def test_cte_with_multiple_out_of_scope_base_tables_raises(self):
        """CTE body JOIN includes multiple out-of-scope tables."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH combined AS ("
            "  SELECT a.id FROM restricted_a a JOIN restricted_b b ON a.id = b.id"
            ") SELECT * FROM combined"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_alias_alone_not_sufficient_to_pass(self):
        """
        Even if sqlparse returns only the CTE alias (not the base table),
        the alias is not in scope.tables → violation.
        Fail-closed fires regardless of the extraction path.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # 'recent' is the alias — it is NOT in scope
        query = (
            "WITH recent AS (SELECT * FROM restricted_table) SELECT * FROM recent"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)


# ---------------------------------------------------------------------------
# AC5 — CTE with in-scope base table → passes
# ---------------------------------------------------------------------------


class TestCTEInScope:
    """
    AC5: A CTE query where the base table is inside the declared scope
    executes successfully.

    sqlparse returns both the CTE alias and the real base table.
    After CTE alias filtering, only the real base table remains.
    If that table is in scope → no exception → the guard allows the query.

    This confirms the guard is not blindly blocking all CTEs — it correctly
    distinguishes safe (in-scope base table) from unsafe (out-of-scope base
    table) CTEs.
    """

    def test_simple_cte_in_scope_passes(self):
        """CTE wrapping an in-scope table must be allowed."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH recent AS ("
            "  SELECT * FROM dbo.ServiceTickets"
            "  WHERE status != 'Closed'"
            ") SELECT * FROM recent"
        )
        validate_scope(query, scope)   # must not raise

    def test_cte_with_bare_table_name_in_explicit_scope_passes(self):
        """CTE body uses bare (unqualified) table name declared in scope."""
        scope = _scope(schemas=["dbo"], tables=["ServiceTickets"])
        query = (
            "WITH t AS (SELECT id FROM ServiceTickets WHERE priority = 'P1')"
            " SELECT * FROM t"
        )
        validate_scope(query, scope)

    def test_cte_alias_excluded_from_scope_check(self):
        """
        The CTE alias itself ('t') must not cause a scope violation even
        though 't' is not in scope.tables.  Only the base table is checked.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH t AS (SELECT * FROM dbo.ServiceTickets) SELECT COUNT(*) FROM t"
        )
        # 't' is the CTE alias — not in scope.tables — but must NOT cause a
        # violation; only the base table dbo.ServiceTickets is scope-checked.
        validate_scope(query, scope)

    def test_multiple_cte_aliases_all_filtered(self):
        """Multiple CTE aliases are all excluded; only base tables are checked."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets", "dbo.Priorities"])
        query = (
            "WITH open_tickets AS ("
            "  SELECT id, priority FROM dbo.ServiceTickets WHERE status = 'Open'"
            "), prio_map AS ("
            "  SELECT code, label FROM dbo.Priorities"
            ") SELECT o.id, p.label FROM open_tickets o JOIN prio_map p ON o.priority = p.code"
        )
        validate_scope(query, scope)


# ---------------------------------------------------------------------------
# AC4 (nested/complex CTEs) — fail-closed on ambiguous extraction
# ---------------------------------------------------------------------------


class TestCTEComplex:
    """
    Complex and nested CTEs where table extraction may be ambiguous.
    The fail-closed rule must fire even when the parser is uncertain.
    """

    def test_nested_cte_out_of_scope_raises(self):
        """
        Outer CTE references inner CTE; inner CTE references out-of-scope table.
        After alias filtering, the real base table (secret_db.admin_logs) remains
        and is not in scope → DBScopeViolationError.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH inner_cte AS ("
            "  SELECT * FROM secret_db.admin_logs"
            "), outer_cte AS ("
            "  SELECT * FROM inner_cte"
            ") SELECT * FROM outer_cte"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_nested_cte_mixes_in_and_out_of_scope_raises(self):
        """
        One CTE references in-scope table, another references out-of-scope.
        Any out-of-scope reference must cause rejection.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH allowed AS ("
            "  SELECT id FROM dbo.ServiceTickets"
            "), forbidden AS ("
            "  SELECT * FROM other_restricted"
            ") SELECT a.id FROM allowed a JOIN forbidden f ON a.id = f.id"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_referencing_another_cte_alias_filtered(self):
        """
        When outer CTE only references inner CTE alias (no real table),
        after filtering aliases the referenced set may become empty.
        Empty-extraction fail-closed must fire → DBScopeViolationError.
        (Protects against the edge case where sqlparse misses the base table.)
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # outer_cte only references inner_cte (another alias), not a real table.
        # After alias filtering: referenced = {} → fail-closed.
        # Note: if sqlparse successfully extracts 'inner_base_table' from the
        # inner CTE body, the violation fires for that reason instead — still
        # the correct outcome.
        query = (
            "WITH inner_cte AS ("
            "  SELECT * FROM inner_base_table"
            "), outer_cte AS ("
            "  SELECT * FROM inner_cte"
            ") SELECT * FROM outer_cte"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)


# ---------------------------------------------------------------------------
# AC6 — Oracle synonym query → DBScopeViolationError
# ---------------------------------------------------------------------------


class TestOracleSynonym:
    """
    AC6: Oracle synonym queries are rejected when the synonym alias is not
    in the declared scope.

    KNOWN LIMITATION (documented in query_guard.py):
    AgentIQ cannot resolve Oracle synonyms at query-parse time.  sqlparse
    extracts the synonym alias as a plain table reference.  If the synonym
    name is not in scope.tables the query is rejected.  If the synonym name
    IS declared in scope, the query passes — even though the synonym might
    resolve to a table in a different schema.  Operators must declare only
    real schema-qualified table names in scope declarations, not synonym aliases.
    """

    def test_synonym_not_in_scope_raises(self):
        """
        SELECT * FROM public_view — 'public_view' is a synonym not in scope.
        Must raise DBScopeViolationError.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = 'SELECT * FROM "public_view"'
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_unquoted_synonym_not_in_scope_raises(self):
        """Synonym referenced without quotes is also rejected."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = "SELECT id, status FROM public_view WHERE status = 'Open'"
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_synonym_pointing_to_different_schema_raises(self):
        """
        Synonym alias declared with a schema qualifier that is not in scope.
        e.g. SELECT * FROM public_schema.public_view when scope is dbo only.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = "SELECT * FROM public_schema.public_view"
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_known_limitation_synonym_in_scope_passes(self):
        """
        KNOWN LIMITATION: if the synonym alias is declared in scope,
        the query passes even though the synonym may resolve elsewhere.
        This test documents the limitation — not validates it as desired.
        Operators must not add synonym names to scope declarations.
        """
        # Declare the synonym alias itself as an allowed table — scope passes.
        # This is the unsafe case that operators must avoid.
        scope = _scope(schemas=["dbo"], tables=["public_view"])
        query = "SELECT * FROM public_view"
        # This passes because 'public_view' is in scope.tables.
        # It is a known limitation: the synonym resolution is not verified.
        validate_scope(query, scope)   # must not raise (documents the gap)


# ---------------------------------------------------------------------------
# AC7 — SELECT 1 and SELECT 1 FROM DUAL still work after CTE changes
# ---------------------------------------------------------------------------


class TestTrivialQueryExemption:
    """
    AC7: The trivial query exemption must continue to work correctly after
    all CTE-related changes are applied.  SELECT 1 and SELECT 1 FROM DUAL
    are used for connection health checks and must never raise.
    """

    def test_select_1_passes_validate_read_only(self):
        validate_read_only("SELECT 1")

    def test_select_1_from_dual_passes_validate_read_only(self):
        validate_read_only("SELECT 1 FROM DUAL")

    def test_select_1_passes_validate_scope(self):
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("SELECT 1", scope)

    def test_select_1_from_dual_passes_validate_scope(self):
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("SELECT 1 FROM DUAL", scope)

    def test_select_1_case_variants_pass(self):
        """Trivial query exemption is case-insensitive."""
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("select 1", scope)
        validate_scope("select 1 from dual", scope)
        validate_scope("  SELECT 1  ", scope)

    def test_trivial_exempt_after_cte_alias_filter_added(self):
        """
        Adding CTE alias filtering must not disturb the trivial query path.
        SELECT 1 has no CTE aliases and no table references — trivial
        exemption fires first and returns before any alias filtering.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        validate_scope("SELECT 1", scope)   # must not raise


# ---------------------------------------------------------------------------
# Multi-statement queries → DBQueryRejectedError
# ---------------------------------------------------------------------------


class TestMultiStatement:
    """
    Multi-statement queries must be rejected with DBQueryRejectedError.
    Multiple statements in a single string increase injection risk and must
    not be treated as normal read-only queries (fail-closed).

    This confirms the multi-statement guard added in Task 5B works for
    both mixed-type and same-type (SELECT+SELECT) batches.
    """

    def test_select_then_drop_rejected(self):
        """SELECT ; DROP must be rejected — DROP is non-SELECT."""
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(
                "SELECT * FROM orders; DROP TABLE orders"
            )

    def test_select_then_insert_rejected(self):
        """SELECT ; INSERT must be rejected."""
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(
                "SELECT id FROM users; INSERT INTO log VALUES (1)"
            )

    def test_two_selects_rejected(self):
        """
        Two SELECT statements in one string must be rejected with
        DBQueryRejectedError — not merely caught by scope validation.
        Multiple statements increase risk even when both are SELECT.
        """
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT 1; SELECT 2")

    def test_two_selects_real_tables_rejected(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(
                "SELECT * FROM orders; SELECT * FROM users"
            )

    def test_multi_statement_error_code(self):
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only("SELECT 1; SELECT 2")
        assert exc_info.value.error_code == "query_rejected"

    def test_single_cte_query_is_one_statement(self):
        """
        WITH ... SELECT is a single statement — must NOT be caught by the
        multi-statement guard.
        """
        # validate_read_only must not raise for a single CTE query
        validate_read_only(
            "WITH t AS (SELECT id FROM orders) SELECT * FROM t"
        )

    def test_single_select_not_rejected(self):
        """Sanity: a normal single SELECT must not be rejected."""
        validate_read_only("SELECT id, name FROM users WHERE active = 1")


# ---------------------------------------------------------------------------
# _extract_cte_aliases — unit tests
# ---------------------------------------------------------------------------


class TestExtractCTEAliases:
    """Unit tests for the internal CTE alias extraction helper."""

    def test_single_cte_alias_extracted(self):
        query = "WITH recent AS (SELECT * FROM t) SELECT * FROM recent"
        aliases = _extract_cte_aliases(query)
        assert "recent" in aliases

    def test_multiple_cte_aliases_extracted(self):
        query = (
            "WITH cte1 AS (SELECT * FROM t1), cte2 AS (SELECT * FROM t2)"
            " SELECT * FROM cte1 JOIN cte2 ON cte1.id = cte2.id"
        )
        aliases = _extract_cte_aliases(query)
        assert "cte1" in aliases
        assert "cte2" in aliases

    def test_no_cte_returns_empty(self):
        query = "SELECT * FROM orders WHERE id = 1"
        assert _extract_cte_aliases(query) == frozenset()

    def test_aliases_are_lowercased(self):
        query = "WITH Recent AS (SELECT * FROM t) SELECT * FROM Recent"
        aliases = _extract_cte_aliases(query)
        assert "recent" in aliases
        assert "Recent" not in aliases

    def test_select_1_has_no_aliases(self):
        assert _extract_cte_aliases("SELECT 1") == frozenset()
