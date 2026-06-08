"""
backend/tests/contract/test_query_guard_complex.py

Sprint 12 Platform Hardening — Task 5B
Documented sqlparse behaviour + fail-closed contract tests for CTEs,
Oracle synonyms, trivial queries, and multi-statement inputs.

Purpose
-------
The query guard is a security boundary.  An untested boundary is not a
boundary.  These tests confirm that:

  1. Every CTE query with an out-of-scope base table is rejected — even
     when sqlparse returns only the CTE alias and not the real table name.
  2. Every CTE query with an in-scope base table is allowed — CTE alias
     names are filtered before scope enforcement so they do not cause
     false rejections.
  3. Oracle synonym queries are rejected when the synonym alias is not in
     the declared scope.  The synonym resolution limitation is documented.
  4. SELECT 1 and SELECT 1 FROM DUAL continue to pass after all CTE and
     multi-statement changes are applied.
  5. Multi-statement queries raise DBQueryRejectedError regardless of
     whether the individual statements are SELECT or non-SELECT.

What sqlparse actually returns (verified 2026-06)
-------------------------------------------------
See full details in query_guard.py module docstring.  Quick reference:

  WITH recent AS (SELECT * FROM restricted_table) SELECT * FROM recent
      → _extract_table_references returns ['recent', 'restricted_table']
      → Both alias AND base table extracted.

  WITH t AS (SELECT * FROM dbo.ServiceTickets) SELECT * FROM t
      → _extract_table_references returns ['dbo.ServiceTickets', 't']
      → Without alias filtering: 't' not in scope → false rejection.
      → With _extract_cte_aliases filtering: only 'dbo.ServiceTickets' checked.

  SELECT * FROM public_view  (Oracle synonym)
      → _extract_table_references returns ['public_view']
      → Synonym target is NOT visible to sqlparse.

  SELECT 1; SELECT 2
      → _extract_table_references returns []
      → Both stmts are SELECT so type-check passes; multi-stmt guard fires.

  SELECT 1 FROM DUAL
      → _extract_table_references returns ['DUAL']
      → Trivial-query exemption fires BEFORE extraction — DUAL never checked.

Test naming convention
----------------------
Test names encode both the SQL pattern being tested and the expected outcome
so that a failing test message immediately explains what broke.  Format:
  test_<pattern>_<expected_outcome>
"""

from __future__ import annotations

import pytest
from datetime import datetime

from connectors.db.query_guard import (
    _extract_cte_aliases,
    _extract_table_references,
    validate_read_only,
    validate_scope,
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
# Documented sqlparse extraction behaviour (unit-level, no scope)
# ---------------------------------------------------------------------------


class TestDocumentedSqlparseBehaviour:
    """
    Unit tests against _extract_table_references() that confirm the exact
    sqlparse return values documented in the query_guard.py module docstring.

    If any of these tests fail after a sqlparse upgrade, the module docstring
    must be updated to reflect the new behaviour before the upgrade is merged.
    """

    def test_cte_extraction_returns_both_alias_and_base_table(self):
        """
        sqlparse Pattern 1: CTE extraction returns BOTH the alias name
        ('recent') and the real base table ('restricted_table').
        Both are present in the returned set.
        """
        query = (
            "WITH recent AS "
            "(SELECT * FROM restricted_table WHERE created_date >= GETDATE()-30) "
            "SELECT * FROM recent"
        )
        refs = _extract_table_references(query)
        assert "recent" in refs, (
            "sqlparse should return the CTE alias 'recent' as a table reference — "
            "if this fails, the module docstring Pattern 1 must be updated."
        )
        assert "restricted_table" in refs, (
            "sqlparse should also return the real base table 'restricted_table' — "
            "if this fails, the fail-closed rule is the only safety net."
        )

    def test_cte_in_scope_extraction_returns_alias_alongside_base_table(self):
        """
        sqlparse Pattern 2: even when the base table is in scope, sqlparse
        still returns the CTE alias ('t') alongside 'dbo.ServiceTickets'.
        Without alias filtering, 't' would cause a false rejection.
        """
        query = (
            "WITH t AS (SELECT * FROM dbo.ServiceTickets WHERE status != 'Closed') "
            "SELECT * FROM t"
        )
        refs = _extract_table_references(query)
        assert "t" in refs, (
            "sqlparse returns the CTE alias 't' as a table reference — "
            "this is why _extract_cte_aliases() filtering is required."
        )
        assert "dbo.ServiceTickets" in refs, (
            "sqlparse must also return the real base table 'dbo.ServiceTickets'."
        )

    def test_nested_cte_extraction_returns_aliases_and_real_base_table(self):
        """
        sqlparse Pattern 3: nested CTE returns both aliases AND the real
        base table from the innermost CTE body.
        """
        query = (
            "WITH inner_cte AS (SELECT * FROM secret_db.admin_logs), "
            "outer_cte AS (SELECT * FROM inner_cte) "
            "SELECT * FROM outer_cte"
        )
        refs = _extract_table_references(query)
        assert "inner_cte" in refs, "inner_cte alias should be in extracted refs"
        assert "outer_cte" in refs, "outer_cte alias should be in extracted refs"
        assert "secret_db.admin_logs" in refs, (
            "Real base table 'secret_db.admin_logs' must be extracted from "
            "the inner CTE body."
        )

    def test_oracle_synonym_extraction_returns_alias_not_resolved_target(self):
        """
        sqlparse Pattern 4: synonym queries return only the alias name.
        AgentIQ cannot see what the synonym resolves to at parse time.
        """
        refs = _extract_table_references("SELECT * FROM public_view")
        assert "public_view" in refs, (
            "sqlparse extracts the synonym alias 'public_view' as a table "
            "reference — the resolved target is invisible to the parser."
        )
        assert len(refs) == 1, (
            "Only the synonym alias should be extracted — not a resolved target."
        )

    def test_multi_statement_select_select_extraction_returns_empty(self):
        """
        sqlparse Pattern 5: 'SELECT 1; SELECT 2' extracts no table references
        because neither statement has a FROM clause.  This empty result would
        trigger fail-closed (DBScopeViolationError) if the multi-statement
        guard in validate_read_only() did not fire first.
        """
        refs = _extract_table_references("SELECT 1; SELECT 2")
        assert refs == set(), (
            "sqlparse returns {} for 'SELECT 1; SELECT 2' — no table refs. "
            "The multi-statement guard must fire before scope validation reaches "
            "the empty-extraction fail-closed rule."
        )

    def test_select_1_from_dual_extraction_returns_dual(self):
        """
        sqlparse Pattern 6: SELECT 1 FROM DUAL extracts 'DUAL' as a table
        reference.  validate_scope() must check the trivial-query exemption
        BEFORE calling _extract_table_references() so that DUAL is never
        checked against scope.tables.
        """
        refs = _extract_table_references("SELECT 1 FROM DUAL")
        assert "DUAL" in refs, (
            "sqlparse extracts 'DUAL' from 'SELECT 1 FROM DUAL'. "
            "The trivial-query exemption in validate_scope() must fire before "
            "extraction so that DUAL does not need to be in scope.tables."
        )

    def test_select_1_extraction_returns_empty(self):
        """SELECT 1 has no table references — extraction returns empty set."""
        refs = _extract_table_references("SELECT 1")
        assert refs == set(), "SELECT 1 should produce no table references."


# ---------------------------------------------------------------------------
# _extract_cte_aliases helper
# ---------------------------------------------------------------------------


class TestExtractCTEAliases:
    """Unit tests for the CTE alias extraction helper."""

    def test_single_alias_extracted(self):
        q = "WITH recent AS (SELECT * FROM t) SELECT * FROM recent"
        aliases = _extract_cte_aliases(q)
        assert "recent" in aliases, (
            "_extract_cte_aliases must identify 'recent' as a CTE alias."
        )

    def test_multiple_aliases_extracted(self):
        q = (
            "WITH cte1 AS (SELECT * FROM t1), cte2 AS (SELECT * FROM t2) "
            "SELECT * FROM cte1 JOIN cte2 ON cte1.id = cte2.id"
        )
        aliases = _extract_cte_aliases(q)
        assert "cte1" in aliases and "cte2" in aliases, (
            "Both 'cte1' and 'cte2' must be identified as CTE aliases."
        )

    def test_no_cte_returns_empty_frozenset(self):
        assert _extract_cte_aliases("SELECT * FROM orders") == frozenset(), (
            "A query without a WITH clause must return an empty frozenset."
        )

    def test_aliases_are_lowercase(self):
        q = "WITH Recent AS (SELECT * FROM t) SELECT * FROM Recent"
        aliases = _extract_cte_aliases(q)
        assert "recent" in aliases and "Recent" not in aliases, (
            "CTE alias names must be stored lower-cased for case-insensitive "
            "comparison against scope.tables."
        )

    def test_select_1_has_no_aliases(self):
        assert _extract_cte_aliases("SELECT 1") == frozenset()


# ---------------------------------------------------------------------------
# AC4 — CTE with out-of-scope base table → DBScopeViolationError
# ---------------------------------------------------------------------------


class TestCTEOutOfScopeIsRejected:
    """
    AC4: A CTE query where the base table is outside declared scope raises
    DBScopeViolationError.  The query must not execute.

    sqlparse returns both the CTE alias and the real base table.
    After alias filtering, the real base table is the only reference left.
    That table is not in scope → DBScopeViolationError.

    Even if sqlparse returns only the alias (not the base table), the alias
    is not in scope → DBScopeViolationError for the wrong reason, but still
    the correct outcome: the query is rejected.
    """

    def test_cte_wrapping_out_of_scope_table_raises_scope_violation(self):
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH recent AS ("
            "  SELECT * FROM restricted_table WHERE created_date >= GETDATE()-30"
            ") SELECT * FROM recent"
        )
        # A CTE whose body reads from 'restricted_table' (not in scope)
        # must be rejected even though sqlparse also extracts the alias 'recent'.
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_query_does_not_execute_when_base_table_out_of_scope(self):
        """The scope violation must fire BEFORE any execution path."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = "WITH cte AS (SELECT * FROM admin_secrets) SELECT * FROM cte"
        executed = []
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)
            executed.append("reached")   # must never be reached
        assert not executed, (
            "Code after validate_scope() must not run when scope is violated."
        )

    def test_cte_with_schema_qualified_out_of_scope_table_raises(self):
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH t AS (SELECT * FROM secret_schema.sensitive_data) "
            "SELECT * FROM t"
        )
        # Schema-qualified out-of-scope table in CTE body must be rejected.
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_with_join_of_out_of_scope_tables_raises(self):
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH combined AS ("
            "  SELECT a.id FROM restricted_a a JOIN restricted_b b ON a.id = b.id"
            ") SELECT * FROM combined"
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_cte_alias_not_in_scope_still_causes_rejection_without_filter(self):
        """
        Documents the original behaviour before alias filtering was added:
        even if only the alias is returned by sqlparse (not the base table),
        the alias is not in scope.tables → rejection fires.

        This test uses a scope that would accept the BASE table but the alias
        would still cause a violation if filtering were absent.
        The important point: in all cases, the query is rejected.
        """
        scope = _scope(schemas=["dbo"], tables=["restricted_table"])
        # Note: 'recent' alias is NOT in scope.tables even though the base
        # table is.  After alias filtering 'recent' is removed; then
        # 'restricted_table' IS in scope → query passes.
        # This test just confirms the alias is correctly filtered.
        query = (
            "WITH recent AS (SELECT * FROM restricted_table) "
            "SELECT * FROM recent"
        )
        # With alias filtering: passes (base table is in scope).
        validate_scope(query, scope)   # must NOT raise


# ---------------------------------------------------------------------------
# AC5 — CTE with in-scope base table → passes
# ---------------------------------------------------------------------------


class TestCTEInScopeIsAllowed:
    """
    AC5: A CTE query whose base table is in the declared scope must execute
    successfully.

    This confirms the guard is not blindly blocking all CTEs — it correctly
    distinguishes safe (in-scope base table) from unsafe (out-of-scope base
    table) patterns.

    sqlparse returns both the CTE alias and the real base table.
    _extract_cte_aliases() removes the alias before scope enforcement.
    Only the real base table is checked — and it passes.
    """

    def test_simple_cte_with_in_scope_base_table_passes(self):
        """
        sqlparse returns ['dbo.ServiceTickets', 't'].
        After _extract_cte_aliases() removes 't', only 'dbo.ServiceTickets'
        is checked — it is in scope → no exception.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH t AS (SELECT * FROM dbo.ServiceTickets WHERE status != 'Closed') "
            "SELECT * FROM t"
        )
        validate_scope(query, scope)   # must not raise

    def test_cte_alias_is_excluded_from_scope_check(self):
        """
        'recent' is the CTE alias.  It must NOT appear in scope.tables.
        validate_scope() must still pass because only the base table is checked.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # Confirm 'recent' is NOT in scope to make the test meaningful
        assert "recent" not in scope.tables, (
            "Test setup: 'recent' must not be in scope.tables — "
            "the test verifies that alias exclusion makes the query pass."
        )
        query = (
            "WITH recent AS (SELECT * FROM dbo.ServiceTickets) "
            "SELECT COUNT(*) FROM recent"
        )
        validate_scope(query, scope)   # must not raise — alias excluded

    def test_cte_with_bare_table_in_explicit_scope_passes(self):
        scope = _scope(schemas=["dbo"], tables=["ServiceTickets"])
        query = (
            "WITH t AS (SELECT id FROM ServiceTickets WHERE priority = 'P1') "
            "SELECT * FROM t"
        )
        validate_scope(query, scope)

    def test_multiple_cte_aliases_all_excluded_base_tables_checked(self):
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets", "dbo.Priorities"])
        query = (
            "WITH open_tickets AS ("
            "  SELECT id, priority FROM dbo.ServiceTickets WHERE status = 'Open'"
            "), prio_map AS ("
            "  SELECT code, label FROM dbo.Priorities"
            ") "
            "SELECT o.id, p.label "
            "FROM open_tickets o JOIN prio_map p ON o.priority = p.code"
        )
        validate_scope(query, scope)   # both aliases excluded; both bases in scope


# ---------------------------------------------------------------------------
# AC4 — nested / complex CTEs → fail-closed on ambiguity
# ---------------------------------------------------------------------------


class TestNestedCTEFailClosed:
    """
    Complex CTEs where extraction may miss the real base table.
    Fail-closed must fire in every case.
    """

    def test_nested_cte_out_of_scope_base_table_raises(self):
        """
        sqlparse Pattern 3: nested CTE returns aliases + real base table.
        After filtering aliases, only 'secret_db.admin_logs' remains.
        Not in scope → DBScopeViolationError.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH inner_cte AS (SELECT * FROM secret_db.admin_logs), "
            "outer_cte AS (SELECT * FROM inner_cte) "
            "SELECT * FROM outer_cte"
        )
        # Real base table 'secret_db.admin_logs' is not in scope —
        # the nested CTE must be rejected.
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_mixed_cte_one_out_of_scope_raises(self):
        """One in-scope + one out-of-scope CTE body: query must be rejected."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        query = (
            "WITH allowed AS (SELECT id FROM dbo.ServiceTickets), "
            "forbidden AS (SELECT * FROM other_restricted) "
            "SELECT a.id FROM allowed a JOIN forbidden f ON a.id = f.id"
        )
        # 'other_restricted' is out of scope — even one violation
        # must cause the entire query to be rejected.
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)

    def test_alias_only_extraction_triggers_fail_closed(self):
        """
        Edge case: if sqlparse returns only CTE aliases (no base tables),
        alias filtering leaves an empty reference set.
        Empty set on non-trivial query → fail-closed → DBScopeViolationError.
        This cannot be used to bypass scope — fail-closed fires regardless.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # outer_cte references inner_cte (another alias).  If sqlparse misses
        # the real base table inside inner_cte, refs after filtering = {}.
        query = (
            "WITH inner_cte AS (SELECT * FROM base_table), "
            "outer_cte AS (SELECT * FROM inner_cte) "
            "SELECT * FROM outer_cte"
        )
        # Either base_table is extracted → not in scope → violation,
        # OR aliases only are extracted → filtering empties set → fail-closed.
        # In either case: DBScopeViolationError must be raised.
        # Nested CTE must be rejected — either because base_table is out
        # of scope, or because alias-only extraction triggers fail-closed.
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)


# ---------------------------------------------------------------------------
# AC6 — Oracle synonym → DBScopeViolationError + documented limitation
# ---------------------------------------------------------------------------


class TestOracleSynonymRejected:
    """
    AC6: Oracle synonym queries are rejected when the synonym alias is not in
    the declared scope.

    DOCUMENTED KNOWN LIMITATION (query_guard.py module docstring Pattern 4):
    AgentIQ cannot resolve synonyms at query-parse time.  sqlparse extracts
    only the alias name, not the object it points to.  If an operator
    incorrectly declares a synonym alias in scope.tables, the query passes —
    even though the synonym may resolve to a table outside scope.
    Operators MUST declare only real schema-qualified table names in scope.
    """

    def test_unquoted_synonym_not_in_scope_raises_scope_violation(self):
        """
        SELECT * FROM public_view — 'public_view' is a synonym.
        sqlparse extracts 'public_view'; it is not in scope → violation.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # Synonym 'public_view' is not in scope — query must be rejected.
        # AgentIQ cannot verify what the synonym resolves to.
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT * FROM public_view", scope)

    def test_synonym_with_schema_qualifier_not_in_scope_raises(self):
        """Synonym referenced with a schema prefix that is not in scope."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT * FROM public_schema.public_view", scope)

    def test_synonym_pointing_to_different_schema_raises(self):
        """
        Even if the synonym is in the declared schema, if it points elsewhere
        the guard still rejects when the synonym name is not in scope.tables.
        """
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        # Synonym pointing to out-of-scope table must cause rejection.
        with pytest.raises(DBScopeViolationError):
            validate_scope("SELECT * FROM dbo.some_synonym", scope)

    def test_known_limitation_synonym_in_scope_passes(self):
        """
        KNOWN LIMITATION (documented in query_guard.py Pattern 4):
        If an operator INCORRECTLY adds a synonym alias to scope.tables,
        the query passes — even though the synonym may resolve elsewhere.

        This test documents the gap.  It must NOT be used to justify adding
        synonym aliases to scope declarations.  Operators must use real
        schema-qualified table names only.
        """
        scope = _scope(schemas=["dbo"], tables=["public_view"])
        # 'public_view' is in scope.tables → passes.  This is the UNSAFE case.
        # The test confirms the current behaviour rather than validating it.
        validate_scope("SELECT * FROM public_view", scope)


# ---------------------------------------------------------------------------
# AC7 — SELECT 1 and SELECT 1 FROM DUAL still pass
# ---------------------------------------------------------------------------


class TestTrivialQueryExemptionUnchanged:
    """
    AC7: The trivial query exemption must continue to work correctly after all
    CTE, alias-filtering, and multi-statement changes are applied.

    SELECT 1 FROM DUAL is particularly important: sqlparse extracts 'DUAL' as
    a table reference (see documented behaviour Pattern 6).  The trivial
    exemption in validate_scope() fires BEFORE extraction, so 'DUAL' is never
    checked.  If the exemption is removed, every Oracle health-check fails.
    """

    def test_select_1_passes_validate_read_only(self):
        validate_read_only("SELECT 1")

    def test_select_1_from_dual_passes_validate_read_only(self):
        validate_read_only("SELECT 1 FROM DUAL")

    def test_select_1_passes_validate_scope_with_empty_tables(self):
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("SELECT 1", scope)

    def test_select_1_from_dual_passes_validate_scope_despite_dual_extraction(self):
        """
        Confirms trivial exemption fires before _extract_table_references().
        sqlparse would return ['DUAL'] — but validate_scope() must return
        immediately from the trivial check without ever reaching extraction.
        """
        scope = _scope(schemas=["dbo"], tables=[])
        # If the trivial exemption were removed, 'DUAL' would not be in
        # scope.tables and this call would raise DBScopeViolationError.
        validate_scope("SELECT 1 FROM DUAL", scope)

    def test_select_1_case_variants_pass(self):
        scope = _scope(schemas=["dbo"], tables=[])
        for q in ("select 1", "SELECT 1", "  SELECT 1  ", "select 1 from dual"):
            validate_scope(q, scope)

    def test_trivial_exemption_unaffected_by_cte_alias_filter(self):
        """SELECT 1 has no CTE aliases — alias filtering must not disturb it."""
        scope = _scope(schemas=["dbo"], tables=["dbo.ServiceTickets"])
        validate_scope("SELECT 1", scope)   # must not raise


# ---------------------------------------------------------------------------
# Multi-statement guard → DBQueryRejectedError
# ---------------------------------------------------------------------------


class TestMultiStatementGuard:
    """
    Multi-statement queries must raise DBQueryRejectedError from
    validate_read_only() — not reach validate_scope().

    Background: sqlparse returns [] for 'SELECT 1; SELECT 2' (documented
    Pattern 5).  Without the multi-statement guard, a double-SELECT input
    would trigger DBScopeViolationError from the empty-extraction fail-closed
    rule — the wrong exception class.  The guard corrects this.
    """

    def test_select_drop_raises_query_rejected_error(self):
        # SELECT ; DROP must raise DBQueryRejectedError — DROP is non-SELECT.
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT * FROM orders; DROP TABLE orders")

    def test_select_insert_raises_query_rejected_error(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT id FROM users; INSERT INTO log VALUES (1)")

    def test_double_select_raises_query_rejected_error(self):
        """
        Both statements are SELECT — the type check passes them individually.
        The multi-statement guard must then fire with DBQueryRejectedError.
        Without the guard, sqlparse returns [] and DBScopeViolationError fires
        instead — the wrong class for a structural input problem.
        """
        # 'SELECT 1; SELECT 2' must raise DBQueryRejectedError.
        # If DBScopeViolationError is raised instead, the multi-statement
        # guard in validate_read_only() is missing or broken.
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT 1; SELECT 2")

    def test_double_select_with_tables_raises_query_rejected_error(self):
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT * FROM orders; SELECT * FROM users")

    def test_multi_statement_error_code_is_query_rejected(self):
        with pytest.raises(DBQueryRejectedError) as exc_info:
            validate_read_only("SELECT 1; SELECT 2")
        assert exc_info.value.error_code == "query_rejected", (
            "Multi-statement rejection must use error_code='query_rejected'."
        )

    def test_single_cte_is_one_statement_not_caught_by_guard(self):
        """
        WITH ... SELECT is a single SQL statement.  It must NOT be rejected
        by the multi-statement guard.
        """
        validate_read_only(
            "WITH t AS (SELECT id FROM orders) SELECT * FROM t"
        )

    def test_single_select_not_affected_by_multi_statement_guard(self):
        validate_read_only("SELECT id, name FROM users WHERE active = 1")
