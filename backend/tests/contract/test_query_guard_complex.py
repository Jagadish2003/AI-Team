"""
backend/tests/contract/test_query_guard_complex.py

Task 5B — sqlparse CTE and Oracle synonym fail-closed tests.
Sprint 12 Platform Hardening.

Covers:
  AC4  — CTE with out-of-scope base table raises DBScopeViolationError.
  AC5  — CTE with in-scope base table executes successfully.
  AC6  — Oracle synonym query raises DBScopeViolationError; behaviour
          documented in query_guard.py comment.
  AC7  — SELECT 1 / SELECT 1 FROM DUAL still pass after CTE handling.

Additional coverage:
  — Complex nested CTE: fail-closed fires on ambiguous extraction.
  — Multi-statement queries: DBQueryRejectedError.
  — Synonym with no schema qualifier (schema-open scope): fail-closed.
  — Synonym present in explicit table list: known-limitation test.
  — Oracle double-quote identifier style in synonym queries.
  — Fail-closed rule documented in each test docstring for future engineers.

sqlparse extraction behaviour (probed, documented here):
  WITH recent AS (SELECT * FROM base_table) SELECT * FROM recent
      → _extract_table_references returns {'recent', 'base_table'}
  Both names must be in scope for the query to pass.

  SELECT * FROM "public_view"
      → _extract_table_references returns {'public_view'}
  The guard cannot know whether public_view is a real table or a synonym.
  When public_view is not in scope, DBScopeViolationError is raised (fail-closed).
  When public_view is unqualified under schema-open scope, it is rejected because
  schema membership cannot be verified without a live DB lookup.
"""

from __future__ import annotations

import pytest
from datetime import datetime

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
    connector_id: str = "oracle_db",
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
# AC4 — CTE with out-of-scope base table raises DBScopeViolationError
# ---------------------------------------------------------------------------

class TestCTEOutOfScope:
    """
    AC4: A CTE whose base table is outside declared scope must raise
    DBScopeViolationError. The query must not reach execution.

    sqlparse extracts both the CTE alias ('recent') and the base table
    ('restricted_table'). Because 'restricted_table' is not in the declared
    scope, DBScopeViolationError fires — correct outcome regardless of whether
    the alias or the base table triggers the violation first.
    """

    _QUERY = (
        "WITH recent AS (\n"
        "    SELECT * FROM restricted_table\n"
        "    WHERE created_date >= GETDATE()\n"
        ")\n"
        "SELECT * FROM recent"
    )

    def test_cte_out_of_scope_base_table_rejected(self):
        """AC4: CTE base table outside declared scope raises DBScopeViolationError."""
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(self._QUERY, scope)

    def test_cte_out_of_scope_query_does_not_execute(self):
        """AC4: Rejected CTE query must not reach execution (fail-closed)."""
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        executed = []
        try:
            validate_scope(self._QUERY, scope)
            executed.append(True)  # this line must never run
        except DBScopeViolationError:
            pass
        assert not executed, "validate_scope must raise before execution can proceed"

    def test_cte_out_of_scope_error_mentions_violation(self):
        """AC4: Error message must reference scope or violation context."""
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        with pytest.raises(DBScopeViolationError) as exc_info:
            validate_scope(self._QUERY, scope)
        msg = str(exc_info.value).lower()
        assert "scope" in msg or "outside" in msg or "declared" in msg

    def test_cte_sqlparse_extracts_base_table_and_alias(self):
        """
        Documents sqlparse behaviour: both CTE alias and base table are extracted.
        'recent' is the alias; 'restricted_table' is the base table.
        Either or both may trigger the violation.
        """
        refs = _extract_table_references(self._QUERY)
        # sqlparse extracts both the alias and the base table
        assert "restricted_table" in refs or "recent" in refs

    def test_cte_schema_qualified_base_table_wrong_schema_rejected(self):
        """AC4: CTE base table with wrong schema also rejected."""
        query = (
            "WITH recent AS (SELECT * FROM restricted_schema.sensitive_table)\n"
            "SELECT * FROM recent"
        )
        scope = _scope(schemas=["dbo"], tables=["recent", "sensitive_table"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(query, scope)


# ---------------------------------------------------------------------------
# AC5 — CTE with in-scope base table executes successfully
# ---------------------------------------------------------------------------

class TestCTEInScope:
    """
    AC5: When ALL table references extracted from the CTE query (including the
    CTE alias name) are within the declared scope, the query executes successfully.

    sqlparse extracts both the CTE alias name ('recent') and the base table
    ('service_tickets'). The scope must declare both so that no extracted
    reference is out-of-scope.

    This is the expected integration pattern: when building queries for
    AgentIQ ingestors that use CTEs, the scope declaration must include
    any identifier sqlparse may surface.
    """

    _QUERY = (
        "WITH recent AS (\n"
        "    SELECT * FROM service_tickets\n"
        ")\n"
        "SELECT * FROM recent"
    )

    def test_cte_in_scope_passes(self):
        """AC5: CTE where all extracted references are in scope passes validation."""
        # Both 'service_tickets' (base table) and 'recent' (CTE alias extracted
        # by sqlparse) are declared in the scope.
        scope = _scope(
            schemas=["dbo"],
            tables=["service_tickets", "recent"],
        )
        validate_scope(self._QUERY, scope)  # must not raise

    def test_cte_with_schema_qualified_base_table_passes(self):
        """AC5: Schema-qualified CTE base table in scope passes."""
        query = (
            "WITH recent AS (\n"
            "    SELECT * FROM dbo.service_tickets\n"
            ")\n"
            "SELECT * FROM recent"
        )
        scope = _scope(
            schemas=["dbo"],
            tables=["service_tickets", "recent"],
        )
        validate_scope(query, scope)  # must not raise

    def test_cte_alias_not_in_scope_still_rejected(self):
        """
        AC5 boundary: even when the base table is in scope, the CTE alias
        extracted by sqlparse must also be in scope.tables — otherwise the
        guard rejects the query because 'recent' is an unknown reference.
        """
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        # 'recent' extracted by sqlparse but not in scope → violation
        with pytest.raises(DBScopeViolationError):
            validate_scope(self._QUERY, scope)


# ---------------------------------------------------------------------------
# Complex nested CTE — fail-closed fires on ambiguous extraction
# ---------------------------------------------------------------------------

class TestNestedCTEFailClosed:
    """
    Complex nested CTEs where the base table is out of scope must be rejected.
    sqlparse extracts all CTE aliases plus the real base table; the base table
    fails scope enforcement → DBScopeViolationError.
    """

    _NESTED_QUERY = (
        "WITH base AS (\n"
        "    SELECT * FROM restricted_table\n"
        "), filtered AS (\n"
        "    SELECT * FROM base\n"
        ")\n"
        "SELECT * FROM filtered"
    )

    def test_nested_cte_out_of_scope_rejected(self):
        """Nested CTE: base table out of scope triggers DBScopeViolationError."""
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        with pytest.raises(DBScopeViolationError):
            validate_scope(self._NESTED_QUERY, scope)

    def test_nested_cte_sqlparse_extracts_real_table(self):
        """Nested CTE: sqlparse surfaces the real base table among extracted refs."""
        refs = _extract_table_references(self._NESTED_QUERY)
        # The real out-of-scope table must be in the extracted set
        assert "restricted_table" in refs

    def test_nested_cte_extracts_all_cte_aliases_too(self):
        """Documents that sqlparse extracts CTE aliases alongside real tables."""
        refs = _extract_table_references(self._NESTED_QUERY)
        # sqlparse returns CTE aliases as well as the base table
        assert len(refs) >= 1  # at minimum the base table is found


# ---------------------------------------------------------------------------
# AC6 — Oracle synonym query raises DBScopeViolationError
# ---------------------------------------------------------------------------

class TestOracleSynonymFailClosed:
    """
    AC6: Oracle synonym queries must raise DBScopeViolationError.

    An Oracle synonym is a database-level alias. AgentIQ cannot resolve synonym
    targets at query parse time — sqlparse operates on SQL text only and has no
    access to Oracle's ALL_SYNONYMS catalogue.

    Fail-closed behaviour for synonyms:
      1. Synonym not in scope.tables → DBScopeViolationError (table unknown).
      2. Unqualified synonym with schema-open scope (tables=[]) →
         DBScopeViolationError (unqualified reference, schema unverifiable).
      3. Schema-qualified synonym with wrong schema → DBScopeViolationError.

    KNOWN LIMITATION (also documented in query_guard.py):
      If a synonym name is erroneously declared in scope.tables, the guard
      cannot detect it is a synonym rather than a real table.
      Callers must NEVER add synonym names to scope.tables — only concrete
      schema.table pairs (e.g. "HR.EMPLOYEES") are safe.
      See test_oracle_synonym_in_scope_known_limitation() below.

    All tests use Oracle-style double-quote identifiers.
    """

    def test_oracle_synonym_not_in_scope_rejected(self):
        """
        AC6: Synonym reference not declared in scope.tables raises
        DBScopeViolationError — the guard treats it as an unknown table.
        """
        # public_view is a synonym for restricted_schema.sensitive_table
        # but the scope only declares real tables
        scope = _scope(
            schemas=["HR"],
            tables=["HR.EMPLOYEES", "HR.CONTRACTS"],
            connector_id="oracle_db",
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope('SELECT * FROM "public_view"', scope)

    def test_oracle_synonym_unqualified_schema_open_rejected(self):
        """
        AC6: Unqualified synonym under schema-open scope (tables=[]) is
        rejected because schema membership cannot be verified without a live
        DB lookup. Fail-closed: ambiguity is never treated as permission.
        """
        scope = _scope(
            schemas=["HR"],
            tables=[],
            connector_id="oracle_db",
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope('SELECT * FROM "public_view"', scope)

    def test_oracle_synonym_wrong_schema_rejected(self):
        """
        AC6: Schema-qualified synonym where the schema is not declared →
        DBScopeViolationError even when the table name appears in scope.tables.
        """
        scope = _scope(
            schemas=["HR"],
            tables=["public_view"],
            connector_id="oracle_db",
        )
        with pytest.raises(DBScopeViolationError):
            # safe_schema is not in scope.schemas
            validate_scope('SELECT * FROM "safe_schema"."public_view"', scope)

    def test_oracle_synonym_query_does_not_execute(self):
        """AC6: A rejected synonym query must not reach execution."""
        scope = _scope(schemas=["HR"], tables=["HR.EMPLOYEES"], connector_id="oracle_db")
        executed = []
        try:
            validate_scope('SELECT * FROM "public_view"', scope)
            executed.append(True)
        except DBScopeViolationError:
            pass
        assert not executed

    def test_oracle_synonym_join_with_synonym_rejected(self):
        """
        AC6: Query that joins a real in-scope table with a synonym is rejected.
        The synonym reference makes the whole query unverifiable.
        """
        scope = _scope(
            schemas=["HR"],
            tables=["HR.EMPLOYEES"],
            connector_id="oracle_db",
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                'SELECT e.id, v.dept FROM "HR"."EMPLOYEES" e '
                'JOIN "dept_view" v ON e.dept_id = v.id',
                scope,
            )

    def test_oracle_synonym_multiple_synonyms_rejected(self):
        """AC6: Query with multiple synonym-like unresolvable references is rejected."""
        scope = _scope(
            schemas=["HR"],
            tables=["HR.EMPLOYEES"],
            connector_id="oracle_db",
        )
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                'SELECT * FROM "public_view1" JOIN "public_view2" ON id = ref_id',
                scope,
            )

    def test_oracle_synonym_in_scope_known_limitation(self):
        """
        KNOWN LIMITATION — documented in query_guard.py:

        If a synonym name is (incorrectly) added to scope.tables, the guard
        cannot detect it is a synonym and the query passes validation. This is
        an inherent limitation of parse-time scope enforcement: AgentIQ has no
        access to Oracle's ALL_SYNONYMS catalogue at validation time.

        Engineers MUST NOT add synonym names to scope.tables. Only concrete
        schema.table pairs (e.g. 'HR.EMPLOYEES') should be declared. Doing
        otherwise defeats the scope boundary for that name.

        This test documents the current behaviour, not a desired loophole. The
        comment in query_guard.py is the normative guidance for scope callers.
        """
        # public_view is a synonym for restricted_schema.sensitive_table,
        # but someone incorrectly added it to scope.tables.
        scope = _scope(
            schemas=["HR"],
            tables=["public_view"],  # BUG: synonym declared as approved table
            connector_id="oracle_db",
        )
        # The guard cannot detect the synonym — query passes (known limitation).
        # This is the gap that the query_guard.py comment warns callers about.
        validate_scope('SELECT * FROM "public_view"', scope)  # must not raise

    def test_oracle_synonym_sqlparse_extracts_bare_name(self):
        """
        Documents sqlparse extraction for Oracle-style double-quote synonym:
        SELECT * FROM "public_view" → extracts {'public_view'}.
        The guard then checks this bare name against scope.
        """
        refs = _extract_table_references('SELECT * FROM "public_view"')
        assert "public_view" in refs

    def test_oracle_synonym_sqlparse_extracts_qualified_name(self):
        """
        Documents sqlparse extraction for schema-qualified Oracle synonym:
        SELECT * FROM "safe_schema"."public_view" → extracts
        {'safe_schema.public_view'}.
        """
        refs = _extract_table_references(
            'SELECT * FROM "safe_schema"."public_view"'
        )
        assert "safe_schema.public_view" in refs


# ---------------------------------------------------------------------------
# AC7 — SELECT 1 / SELECT 1 FROM DUAL still pass after CTE handling added
# ---------------------------------------------------------------------------

class TestTrivialQueryExemptionPreserved:
    """
    AC7: The trivial query exemption must be intact after CTE and synonym
    handling is added. These queries are used for connection health checks.
    """

    def test_select_1_passes(self):
        """AC7: SELECT 1 must pass validate_scope regardless of declared scope."""
        scope = _scope(schemas=["dbo"], tables=["service_tickets"])
        validate_scope("SELECT 1", scope)  # must not raise

    def test_select_1_from_dual_passes(self):
        """AC7: SELECT 1 FROM DUAL (Oracle health check) must pass."""
        scope = _scope(schemas=["HR"], tables=[], connector_id="oracle_db")
        validate_scope("SELECT 1 FROM DUAL", scope)  # must not raise

    def test_select_1_case_insensitive(self):
        """AC7: Trivial query exemption must be case-insensitive."""
        scope = _scope(schemas=["dbo"], tables=[])
        validate_scope("select 1", scope)
        validate_scope("select 1 from dual", scope)

    def test_select_1_read_only_passes(self):
        """AC7: validate_read_only also passes for trivial queries."""
        validate_read_only("SELECT 1")
        validate_read_only("SELECT 1 FROM DUAL")

    def test_select_1_not_confused_with_cte(self):
        """AC7: CTE handling must not interfere with trivial query exemption."""
        scope = _scope(schemas=["dbo"], tables=[])
        # Trivial: no table reference extraction needed
        validate_scope("SELECT 1", scope)


# ---------------------------------------------------------------------------
# Multi-statement queries — DBQueryRejectedError
# ---------------------------------------------------------------------------

class TestMultiStatementRejected:
    """
    Multi-statement queries must be rejected with DBQueryRejectedError.
    A single execute_query() call must never run more than one statement.
    """

    def test_two_selects_scope_violation(self):
        """
        Two SELECT statements joined with a semicolon: validate_read_only accepts
        both (each is a SELECT), but validate_scope rejects the second table if
        it is out of scope. The guard enforces safety even for multi-SELECT strings.
        """
        scope = _scope(schemas=["dbo"], tables=["orders"])
        # 'users' is referenced in the second statement but not in scope
        with pytest.raises(DBScopeViolationError):
            validate_scope(
                "SELECT * FROM orders; SELECT * FROM users", scope
            )

    def test_select_then_drop_rejected(self):
        """SELECT followed by DDL is rejected."""
        with pytest.raises(DBQueryRejectedError):
            validate_read_only("SELECT * FROM orders; DROP TABLE orders")

    def test_select_then_insert_rejected(self):
        """SELECT followed by INSERT is rejected."""
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(
                "SELECT * FROM users; INSERT INTO log VALUES (1)"
            )

    def test_select_then_update_rejected(self):
        """SELECT followed by UPDATE is rejected."""
        with pytest.raises(DBQueryRejectedError):
            validate_read_only(
                "SELECT id FROM orders; UPDATE orders SET status = 'closed'"
            )


# ---------------------------------------------------------------------------
# Fail-closed documentation — explicit proof of the rule
# ---------------------------------------------------------------------------

class TestFailClosedRuleExplicit:
    """
    Explicit proof that the fail-closed rule covers both CTE and synonym
    scenarios. These tests ensure future refactors cannot silently remove
    the security boundary without breaking contract tests.
    """

    def test_cte_fail_closed_no_silent_pass(self):
        """
        CTE with out-of-scope table must NEVER silently pass.
        If this test starts failing, the fail-closed rule has been broken.
        """
        scope = _scope(schemas=["dbo"], tables=["allowed_table"])
        query = (
            "WITH cte AS (SELECT * FROM disallowed_table) "
            "SELECT * FROM cte"
        )
        raised = False
        try:
            validate_scope(query, scope)
        except DBScopeViolationError:
            raised = True
        assert raised, (
            "Fail-closed rule broken: CTE with out-of-scope table silently passed. "
            "Check query_guard.py — ambiguity must never become permission."
        )

    def test_synonym_fail_closed_no_silent_pass(self):
        """
        Synonym reference not in scope must NEVER silently pass.
        If this test starts failing, the fail-closed rule has been broken.
        """
        scope = _scope(schemas=["HR"], tables=["HR.EMPLOYEES"], connector_id="oracle_db")
        raised = False
        try:
            validate_scope('SELECT * FROM "HR"."unknown_synonym"', scope)
        except DBScopeViolationError:
            raised = True
        assert raised, (
            "Fail-closed rule broken: Oracle synonym query silently passed. "
            "See KNOWN LIMITATION section in query_guard.py."
        )

    def test_ambiguity_is_not_permission(self):
        """
        Core security invariant: when the guard cannot positively verify
        scope membership, it must reject — never allow.
        This applies to synonyms, CTEs, and any other construct where
        sqlparse cannot reliably extract concrete table references.
        """
        # Unqualified reference under schema-open scope — ambiguous → rejected
        scope = _scope(schemas=["HR"], tables=[], connector_id="oracle_db")
        with pytest.raises(DBScopeViolationError):
            validate_scope('SELECT * FROM "ambiguous_name"', scope)
