"""
backend/connectors/db/query_guard.py

Query guard for the AgentIQ database connectivity framework (T2-S10-A, Task T6).

Implements two layers of read-only / scope enforcement called by
execute_query() BEFORE any database connection is opened:

    validate_read_only(query)
        Uses sqlparse for reliable statement type detection.
        Rejects INSERT, UPDATE, DELETE, DDL, and every non-SELECT type.
        Never relies on string matching or prefix heuristics.

    validate_scope(query, scope)
        Fail-closed scope boundary enforcement.
        If table references cannot be reliably extracted (parse failure,
        CTEs, ambiguous aliases, or schema-qualified name ambiguity), the
        query is rejected rather than allowed.
        scope.tables == [] means any table in the declared schemas is
        permitted — scope enforcement is NOT bypassed; schema membership
        is still verified via schema qualifier inspection.

ARCHITECTURAL NOTE (from spec section 3b):
    sqlparse alone is not a sufficient security boundary for scope enforcement
    across all enterprise SQL dialects. The fail-closed rule compensates: any
    ambiguity in table extraction is treated as a violation.
"""

from __future__ import annotations

import sqlparse
from sqlparse.sql import (
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    Where,
)
from sqlparse.tokens import Keyword, DML

from connectors.db.models import (
    DBQueryRejectedError,
    DBScopeViolationError,
    ScopeDeclaration,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Only SELECT is permitted. All other statement types are rejected.
ALLOWED_STATEMENT_TYPES: frozenset[str] = frozenset({"SELECT"})

#: Trivial queries that are exempt from table-reference extraction.
#: SELECT 1 and SELECT 1 FROM DUAL never reference real tables and are
#: used for connection health-checks only.
_TRIVIAL_QUERIES: frozenset[str] = frozenset({
    "SELECT 1",
    "SELECT 1 FROM DUAL",
})

#: Keywords that introduce a table reference (FROM / JOIN variants).
#: Multi-word JOIN keywords are represented as single normalised tokens
#: by sqlparse (e.g. "LEFT JOIN" → one Keyword token with value "LEFT JOIN").
_TABLE_INTRODUCING_KEYWORDS: frozenset[str] = frozenset({
    "FROM",
    "JOIN",
    "INNER JOIN",
    "LEFT JOIN",
    "RIGHT JOIN",
    "FULL JOIN",
    "CROSS JOIN",
    "LEFT OUTER JOIN",
    "RIGHT OUTER JOIN",
    "FULL OUTER JOIN",
    "STRAIGHT_JOIN",
    "NATURAL JOIN",
    "NATURAL LEFT JOIN",
    "NATURAL RIGHT JOIN",
})

#: Keywords that terminate the FROM / table-reference context.
_CLAUSE_TERMINATORS: frozenset[str] = frozenset({
    "WHERE",
    "GROUP",
    "HAVING",
    "ORDER",
    "LIMIT",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "OFFSET",
    "FETCH",
    "SET",          # guard against UPDATE SET slipping through
    "INTO",         # guard against INSERT INTO slipping through
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_read_only(query: str) -> None:
    """
    Validate that *query* is a SELECT-only statement.

    Detection uses sqlparse statement type — NOT string matching or prefix
    heuristics.  sqlparse correctly identifies SELECT, INSERT, UPDATE,
    DELETE, and DDL statement types (CREATE, DROP, ALTER, TRUNCATE, etc.)
    across SQL Server, Oracle DB, and PostgreSQL.

    A statement whose type cannot be positively identified as SELECT is
    rejected (fail-closed: unknown is treated as non-SELECT).

    Args:
        query: Raw SQL string to validate.

    Raises:
        DBQueryRejectedError: For any non-SELECT statement type, including
            INSERT, UPDATE, DELETE, DDL, and unknown / unparseable types.
    """
    stripped = query.strip()
    if not stripped:
        raise DBQueryRejectedError(
            "Empty query is not permitted.",
            error_code="query_rejected",
        )

    try:
        parsed = sqlparse.parse(stripped)
    except Exception as exc:  # pragma: no cover — sqlparse rarely raises
        raise DBQueryRejectedError(
            f"Failed to parse query for statement type detection: {exc}",
            error_code="query_rejected",
        )

    if not parsed:
        raise DBQueryRejectedError(
            "Query produced no parse results — rejected (fail-closed).",
            error_code="query_rejected",
        )

    for statement in parsed:
        # sqlparse.Statement.get_type() returns None for unrecognised types.
        # None is not in ALLOWED_STATEMENT_TYPES, so it is correctly rejected.
        stmt_type = statement.get_type()
        if stmt_type not in ALLOWED_STATEMENT_TYPES:
            raise DBQueryRejectedError(
                f"Query type {stmt_type!r} is not permitted. "
                "Only SELECT statements are allowed (read-only enforcement).",
                error_code="query_rejected",
            )


def validate_scope(query: str, scope: ScopeDeclaration) -> None:
    """
    Validate that all table references in *query* lie within *scope*.

    Fail-closed rule
    ----------------
    Any of the following conditions cause DBScopeViolationError:
      - Table reference extraction raises any exception.
      - Extraction returns an empty set for a non-trivial query.
      - A referenced table is not in scope.tables (when non-empty).
      - A referenced table's schema qualifier is not in scope.schemas.
      - A reference has no schema qualifier when scope.tables == []
        (schema membership cannot be verified → fail-closed).

    scope.tables == [] semantics
    ----------------------------
    An empty tables list does NOT bypass scope enforcement. It means
    "any table in the declared schemas is permitted."  The framework
    verifies schema membership by requiring fully schema-qualified
    references (schema.table) whose schema is in scope.schemas.
    Unqualified references under an empty tables list are rejected because
    schema membership cannot be determined without a live DB lookup.

    Trivial query exemption
    -----------------------
    "SELECT 1" and "SELECT 1 FROM DUAL" are exempt (connection health checks).

    Args:
        query: Raw SQL string (must already have passed validate_read_only).
        scope: Declared scope for the org + connector.

    Raises:
        DBScopeViolationError: When the query is out of scope or when
            table extraction fails (fail-closed).
    """
    normalized_upper = query.strip().upper()

    # Trivial query exemption — no table references to check
    if normalized_upper in _TRIVIAL_QUERIES:
        return

    # ------------------------------------------------------------------
    # Step 1: extract table references (fail-closed on any exception)
    # ------------------------------------------------------------------
    try:
        referenced: set[str] = _extract_table_references(query)
    except Exception as exc:
        raise DBScopeViolationError(
            f"Table extraction failed — query rejected (fail-closed). "
            f"Reason: {exc}"
        ) from exc

    # Empty extraction result on a non-trivial query → fail-closed
    if not referenced:
        raise DBScopeViolationError(
            "No table references could be extracted from a non-trivial query "
            "— query rejected (fail-closed)."
        )

    # ------------------------------------------------------------------
    # Step 2: scope enforcement
    # ------------------------------------------------------------------
    allowed_schemas: set[str] = {s.lower() for s in scope.schemas}
    violations: set[str] = set()

    if scope.tables:
        # Explicit allowlist: match by bare table name (case-insensitive).
        # If a reference is schema-qualified, its schema must also be in
        # the declared schemas.
        allowed_tables: set[str] = {_bare_name(t).lower() for t in scope.tables}

        for ref in referenced:
            schema_part, table_part = _split_qualified(ref)
            table_lower = table_part.lower()

            if table_lower not in allowed_tables:
                # Table name not in explicit allowlist
                violations.add(ref)
            elif schema_part is not None and schema_part.lower() not in allowed_schemas:
                # Schema qualifier present but not in declared schemas
                violations.add(ref)

    else:
        # scope.tables == [] — any table in declared schemas is permitted,
        # but we MUST be able to verify schema membership.
        # Require fully-qualified references; reject unqualified ones.
        for ref in referenced:
            schema_part, _table_part = _split_qualified(ref)

            if schema_part is None:
                # Unqualified reference: cannot verify schema membership.
                # Fail-closed — reject even though the table might be in scope.
                violations.add(ref)
            elif schema_part.lower() not in allowed_schemas:
                # Schema not in the declared schemas
                violations.add(ref)

    if violations:
        tables_desc = repr(set(scope.tables)) if scope.tables else "(any in declared schemas)"
        raise DBScopeViolationError(
            f"Query references tables outside the declared scope: {violations!r}. "
            f"Declared schemas: {set(scope.schemas)!r}. "
            f"Declared tables: {tables_desc}."
        )


# ---------------------------------------------------------------------------
# Internal helpers — table reference extraction
# ---------------------------------------------------------------------------


def _extract_table_references(query: str) -> set[str]:
    """
    Extract table references from *query* using sqlparse token traversal.

    Returns a set of strings, each either a bare table name ("orders") or a
    schema-qualified name ("dbo.orders").  Subquery aliases are excluded.

    Raises:
        Exception: On any parsing anomaly.  The caller applies fail-closed.
    """
    stripped = query.strip()
    statements = sqlparse.parse(stripped)
    if not statements:
        raise ValueError("sqlparse returned no statements for the query")

    refs: set[str] = set()
    for stmt in statements:
        _collect_refs(stmt, refs, expect_table=False)
    return refs


def _collect_refs(
    token_list,
    refs: set[str],
    *,
    expect_table: bool,
) -> None:
    """
    Recursively walk *token_list* and collect table identifiers.

    *expect_table* is True immediately after a FROM / JOIN keyword, signalling
    that the next significant token should be treated as a table reference.
    """
    i = 0
    tokens = token_list.tokens
    n = len(tokens)

    while i < n:
        token = tokens[i]

        # Skip whitespace silently
        if token.is_whitespace:
            i += 1
            continue

        # ---- FROM / JOIN keyword → next non-whitespace is table context ----
        if _is_table_keyword(token):
            expect_table = True
            i += 1
            continue

        # ---- Clause terminators end the table context ----------------------
        if _is_terminator(token):
            expect_table = False
            # Still recurse into compound terminators (e.g. WHERE subqueries)
            if hasattr(token, "tokens"):
                _collect_refs(token, refs, expect_table=False)
            i += 1
            continue

        # ---- Process token in table context --------------------------------
        if expect_table:
            if isinstance(token, IdentifierList):
                # Multiple comma-separated table references
                for ident in token.get_identifiers():
                    if isinstance(ident, Identifier):
                        name = _resolve_table_name(ident)
                        if name:
                            refs.add(name)
                        # Recurse into any subqueries inside the identifier
                        _recurse_into_subqueries(ident, refs)
                expect_table = False

            elif isinstance(token, Identifier):
                name = _resolve_table_name(token)
                if name:
                    refs.add(name)
                # Recurse into any subquery inside the identifier
                _recurse_into_subqueries(token, refs)
                expect_table = False

            elif isinstance(token, Parenthesis):
                # Derived table: FROM (SELECT ...) AS alias
                _collect_refs(token, refs, expect_table=False)
                expect_table = False

            else:
                # Bare keyword token used as table name (uncommon but possible)
                # e.g. FROM dual  — dual is a Name token, not an Identifier
                from sqlparse.tokens import Name as NameTtype
                if token.ttype is NameTtype:
                    refs.add(token.normalized)
                    expect_table = False
                # Otherwise keep expect_table=True and skip non-meaningful tokens

        else:
            # Not in table context — recurse into compound tokens to find
            # nested FROM clauses (subqueries, parenthesised expressions)
            if hasattr(token, "tokens") and not isinstance(token, Function):
                _collect_refs(token, refs, expect_table=False)

        i += 1


def _recurse_into_subqueries(identifier: Identifier, refs: set[str]) -> None:
    """Recurse into any Parenthesis children of *identifier* (subqueries)."""
    for sub in identifier.tokens:
        if isinstance(sub, Parenthesis):
            _collect_refs(sub, refs, expect_table=False)


def _resolve_table_name(identifier: Identifier) -> str | None:
    """
    Resolve the real table name from an Identifier token.

    Returns:
        "schema.table" if schema-qualified, "table" for bare names, or
        None if the identifier is a subquery alias or function call.
    """
    # If the identifier wraps a parenthesised subquery, it is an alias — skip.
    for sub in identifier.tokens:
        if not sub.is_whitespace:
            if isinstance(sub, Parenthesis):
                return None   # subquery alias
            if isinstance(sub, Function):
                return None   # function call in FROM (table-valued function)
            break  # first non-whitespace token is not a subquery

    real_name = identifier.get_real_name()
    if not real_name:
        return None

    parent_name = identifier.get_parent_name()   # schema qualifier, or None
    if parent_name:
        return f"{parent_name}.{real_name}"
    return real_name


# ---------------------------------------------------------------------------
# Internal helpers — token classification
# ---------------------------------------------------------------------------


def _is_table_keyword(token) -> bool:
    """Return True if *token* is a FROM or JOIN keyword."""
    if token.ttype in (Keyword, DML):
        return token.normalized.upper().strip() in _TABLE_INTRODUCING_KEYWORDS
    return False


def _is_terminator(token) -> bool:
    """Return True if *token* marks the end of the FROM / table context."""
    if isinstance(token, Where):
        return True
    if token.ttype is Keyword:
        return token.normalized.upper().strip() in _CLAUSE_TERMINATORS
    return False


# ---------------------------------------------------------------------------
# Internal helpers — name utilities
# ---------------------------------------------------------------------------


def _split_qualified(ref: str) -> tuple[str | None, str]:
    """
    Split "schema.table" into ("schema", "table").
    Returns (None, "table") for unqualified references.
    """
    parts = ref.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0]


def _bare_name(ref: str) -> str:
    """Return just the table name, stripping any schema qualifier."""
    return ref.rsplit(".", 1)[-1]
