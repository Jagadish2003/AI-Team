"""
backend/connectors/db/query_guard.py

Query guard for the AgentIQ database connectivity framework (T2-S10-A, Task T6).
Sprint 12 Platform Hardening (Task 5B): documented sqlparse behaviour and
fail-closed rules for CTEs, synonyms, and multi-statement queries.

Implements two layers of read-only / scope enforcement called by
execute_query() BEFORE any database connection is opened:

    validate_read_only(query)
        Uses sqlparse for reliable statement type detection.
        Rejects INSERT, UPDATE, DELETE, DDL, and every non-SELECT type.
        Never relies on string matching or prefix heuristics.
        Also rejects multi-statement inputs (> 1 parsed statement).

    validate_scope(query, scope)
        Fail-closed scope boundary enforcement.
        If table references cannot be reliably extracted (parse failure,
        CTEs, ambiguous aliases, or schema-qualified name ambiguity), the
        query is rejected rather than allowed.
        scope.tables == [] means any table in the declared schemas is
        permitted — scope enforcement is NOT bypassed; schema membership
        is still verified via schema qualifier inspection.

───────────────────────────────────────────────────────────────────────────────
DOCUMENTED SQLPARSE BEHAVIOUR
(verified against sqlparse 0.4.x, query patterns from Sprint 11/12 ingestors)
───────────────────────────────────────────────────────────────────────────────

The observations below were produced by calling _extract_table_references()
on each pattern and recording the exact return value.  They are intentionally
left in the source so that future engineers understand what sqlparse does —
and why the guard makes the decisions it does — before refactoring.

Pattern 1 — CTE with out-of-scope base table:
    Query : WITH recent AS (SELECT * FROM restricted_table ...) SELECT * FROM recent
    Return: ['recent', 'restricted_table']
    Note  : sqlparse extracts BOTH the CTE alias ('recent') AND the real base
            table ('restricted_table').  'recent' is the virtual alias; it is
            not a real database object.  'restricted_table' is the real table.
    Guard : Either name may fail scope validation.  If 'restricted_table' is
            out of scope → DBScopeViolationError (correct reason).  If only
            'recent' is out of scope but 'restricted_table' is in scope, the
            alias would cause a false rejection — this is why _extract_cte_aliases()
            removes alias names before scope enforcement.

Pattern 2 — CTE with in-scope base table:
    Query : WITH t AS (SELECT * FROM dbo.ServiceTickets ...) SELECT * FROM t
    Return: ['dbo.ServiceTickets', 't']
    Note  : sqlparse returns 't' (CTE alias) as a table reference alongside
            'dbo.ServiceTickets' (the real table).  Without alias filtering,
            't' would fail scope validation even though the query is safe.
    Guard : _extract_cte_aliases() removes 't' before scope check.  Only
            'dbo.ServiceTickets' is validated → passes when in scope.
    IMPORTANT: if _extract_cte_aliases() is removed or broken, in-scope
            CTE queries WILL be incorrectly rejected.  This is a regression
            guard — do not remove without updating the contract tests.

Pattern 3 — Nested CTE (two aliases, one real base table):
    Query : WITH inner_cte AS (SELECT * FROM secret_db.admin_logs),
              outer_cte AS (SELECT * FROM inner_cte) SELECT * FROM outer_cte
    Return: ['inner_cte', 'outer_cte', 'secret_db.admin_logs']
    Note  : Both aliases AND the real base table are extracted.  After alias
            filtering, only 'secret_db.admin_logs' remains.  That table is
            not in scope → DBScopeViolationError (correct).
    EDGE CASE: if sqlparse fails to recurse into the inner CTE body, it may
            return only ['inner_cte', 'outer_cte'].  After alias filtering → {}.
            Empty set on non-trivial query → fail-closed fires.  Correct.

Pattern 4 — Oracle synonym:
    Query : SELECT * FROM public_view   (synonym pointing to restricted_schema.sensitive)
    Return: ['public_view']
    Note  : sqlparse sees only the alias name.  AgentIQ cannot resolve what
            a synonym points to at query-parse time — no live DB lookup is
            performed here.
    Guard : If 'public_view' is not in scope.tables → DBScopeViolationError.
    KNOWN LIMITATION: if an operator declares 'public_view' in scope.tables,
            the query passes — even though it may resolve to an object outside
            declared scope.  Operators MUST declare real schema-qualified table
            names in scope.  Synonym aliases must NOT be added to scope.tables.
            This limitation is intentional and documented here to prevent
            silent scope bypass via synonym declarations.

Pattern 5 — Multi-statement (SELECT; SELECT):
    Query : SELECT 1; SELECT 2
    Return: []   (empty — no table references found across either statement)
    Note  : Empty set on a non-trivial query would normally trigger the
            fail-closed rule with DBScopeViolationError.  However, the correct
            error class for multiple statements is DBQueryRejectedError (the
            query structure itself is invalid, not just the scope).
    Guard : validate_read_only() detects > 1 parsed statement BEFORE scope
            validation is reached, raising DBQueryRejectedError.

Pattern 6 — SELECT 1 FROM DUAL:
    Query : SELECT 1 FROM DUAL
    Return: ['DUAL']   (sqlparse extracts DUAL as a table reference)
    Note  : DUAL is Oracle's built-in pseudo-table used for health checks.
            It has no rows and is used only to satisfy the FROM clause syntax.
    Guard : validate_scope() checks the trivial-query exemption BEFORE calling
            _extract_table_references().  'SELECT 1 FROM DUAL' is in
            _TRIVIAL_QUERIES and returns immediately — 'DUAL' is never
            checked against scope.tables.
    IMPORTANT: do NOT remove SELECT 1 FROM DUAL from _TRIVIAL_QUERIES.
            Without it, every Oracle health-check query would require 'DUAL'
            to be declared in scope, which is wrong.

───────────────────────────────────────────────────────────────────────────────
THE FAIL-CLOSED RULE — READ BEFORE REFACTORING
───────────────────────────────────────────────────────────────────────────────

The fail-closed rule is the foundation of the security boundary:

    IF AgentIQ cannot reliably extract and verify table references,
    IT REJECTS THE QUERY — it does not execute it.

This is intentional.  It is NOT a bug, NOT an overly strict check, and NOT
something to be relaxed for convenience.  The rule exists because:

  1. sqlparse is not a full SQL parser for every enterprise SQL dialect.
     Oracle synonyms, recursive CTEs, dynamic SQL, and dialect-specific
     constructs may cause incorrect or incomplete extraction.

  2. AgentIQ runs queries against live customer databases.  A wrong
     allow-decision leaks data from outside the declared scope.  A wrong
     reject-decision causes a query to fail.  Reject is always safer.

  3. T2-S16-A (normalisation layer) and future Track 3 stories build on top
     of the data returned by these queries.  Scope violations here propagate
     silently into opportunity cards, evidence, and executive reports.

If you find that the guard rejects a query that should be allowed:
  a. Write a failing test that demonstrates the case.
  b. Fix _extract_table_references() or add an alias-filter helper.
  c. Document the sqlparse behaviour you observed (as above).
  d. Do NOT weaken the fail-closed rule to make tests pass.

───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import sqlparse
from sqlparse.sql import (
    Comment,
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    Where,
)
from sqlparse.tokens import Keyword, DML, Punctuation

from .models import (
    DBQueryRejectedError,
    DBScopeViolationError,
    ScopeDeclaration,
)

logger = logging.getLogger(__name__)

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
# CTE alias helper
# ---------------------------------------------------------------------------


def _extract_cte_aliases(query: str) -> frozenset[str]:
    """Return the set of CTE alias names (lower-cased) defined in *query*.

    Example:
        WITH recent AS (...) SELECT * FROM recent  →  frozenset({'recent'})
        WITH a AS (...), b AS (...)               →  frozenset({'a', 'b'})

    Why this is needed
    ------------------
    sqlparse returns CTE alias names as regular table Identifiers alongside
    the real base tables (see module docstring Pattern 1 / Pattern 2).  If
    an alias is not in scope.tables validate_scope would raise
    DBScopeViolationError for the wrong reason — the alias is virtual and
    should never be scope-checked.

    After calling this helper, validate_scope subtracts the alias set from
    the extracted references before enforcement.  Only real base tables
    (found inside the CTE bodies during sqlparse recursion) remain.

    Fail-closed guarantee:
        If alias filtering reduces the reference set to {} and the query is
        not trivial, the empty-extraction fail-closed rule fires and the
        query is still rejected.  Alias filtering cannot be used to bypass
        scope enforcement — it can only prevent false rejections.

    Returns an empty frozenset when the query contains no WITH clause.
    """
    aliases: set[str] = set()
    for statement in sqlparse.parse(query):
        tokens = list(statement.tokens)
        try:
            with_idx = next(
                i for i, token in enumerate(tokens)
                if token.ttype is Keyword.CTE and token.normalized.upper() == "WITH"
            )
        except StopIteration:
            continue

        pending_alias: str | None = None
        i = with_idx + 1
        while i < len(tokens):
            token = tokens[i]

            if _is_ignorable_cte_token(token):
                i += 1
                continue

            if token.ttype in (DML, Keyword) and token.normalized.upper() == "SELECT":
                break

            if isinstance(token, IdentifierList):
                for ident in token.get_identifiers():
                    alias = _cte_alias_from_identifier(ident)
                    if alias:
                        aliases.add(alias)
                break

            if isinstance(token, Identifier):
                alias = _cte_alias_from_identifier(token)
                if alias:
                    aliases.add(alias)
                    pending_alias = None
                elif pending_alias and _identifier_contains_as_parenthesis(token):
                    aliases.add(pending_alias)
                    pending_alias = None
                i += 1
                continue

            if token.ttype is Keyword and token.normalized.upper() == "AS":
                if pending_alias and _next_significant_is_parenthesis(tokens, i + 1):
                    aliases.add(pending_alias)
                    pending_alias = None
                i += 1
                continue

            if token.ttype is Punctuation and token.value == ",":
                pending_alias = None
                i += 1
                continue

            if _token_can_be_cte_alias(token):
                pending_alias = _clean_identifier_name(token.value)

            i += 1

    return frozenset(a for a in aliases if a)


def _is_ignorable_cte_token(token) -> bool:
    return bool(token.is_whitespace or isinstance(token, Comment))


def _token_can_be_cte_alias(token) -> bool:
    if token.ttype in (DML, Punctuation):
        return False
    if token.ttype is Keyword and token.normalized.upper() in {"WITH", "AS", "SELECT"}:
        return False
    return bool(_clean_identifier_name(token.value))


def _identifier_contains_as_parenthesis(identifier: Identifier) -> bool:
    saw_as = False
    for token in identifier.tokens:
        if _is_ignorable_cte_token(token):
            continue
        if isinstance(token, Identifier) and _identifier_contains_as_parenthesis(token):
            return True
        if token.ttype is Keyword and token.normalized.upper() == "AS":
            saw_as = True
            continue
        if saw_as and isinstance(token, Parenthesis):
            return True
    return False


def _cte_alias_from_identifier(identifier: Identifier) -> str | None:
    """Extract the alias name from one CTE definition Identifier."""
    if not _identifier_contains_as_parenthesis(identifier):
        return None
    for token in identifier.tokens:
        if _is_ignorable_cte_token(token):
            continue
        if token.ttype is Keyword and token.normalized.upper() == "AS":
            return None
        if isinstance(token, Parenthesis):
            return None
        return _clean_identifier_name(token.value)
    return None


def _next_significant_is_parenthesis(tokens: list, start_idx: int) -> bool:
    for token in tokens[start_idx:]:
        if _is_ignorable_cte_token(token):
            continue
        return isinstance(token, Parenthesis)
    return False


def _clean_identifier_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    for quote_l, quote_r in (('"', '"'), ("'", "'"), ("`", "`"), ("[", "]")):
        if cleaned.startswith(quote_l) and cleaned.endswith(quote_r):
            cleaned = cleaned[1:-1]
            break
    return cleaned.strip().lower()


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

    # Multi-statement guard (fail-closed, Task 5B).
    # See module docstring Pattern 5 for the exact sqlparse behaviour.
    # sqlparse returns [] for "SELECT 1; SELECT 2" — both statements pass the
    # type check above because both are SELECT.  Without this guard, a
    # double-SELECT input would reach validate_scope with an empty reference
    # set and trigger DBScopeViolationError instead of the correct
    # DBQueryRejectedError.  Multiple statements increase injection risk
    # regardless of their individual types and must be rejected here.
    non_empty_stmts = [s for s in parsed if s.value.strip()]
    if len(non_empty_stmts) > 1:
        raise DBQueryRejectedError(
            f"Multi-statement queries are not permitted — "
            f"{len(non_empty_stmts)} statements detected. "
            "Only a single SELECT statement is allowed per query (fail-closed).",
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

    # CTE alias filtering (Task 5B) — see module docstring Pattern 1 / Pattern 2.
    # sqlparse returns CTE alias names (e.g. 'recent', 't') as regular table
    # references alongside the real base tables.  Aliases are virtual; they
    # are not real database objects and must not be scope-checked.
    # _extract_cte_aliases() identifies names defined in WITH ... AS (...).
    # After filtering, only real base tables remain for scope enforcement.
    #
    # Fail-closed guarantee: if filtering empties the reference set (meaning
    # sqlparse returned only aliases and missed the real base tables), the
    # empty-extraction rule below still fires and the query is rejected.
    cte_aliases = _extract_cte_aliases(query)
    if cte_aliases:
        shadowed_aliases: set[str] = set()
        if scope.tables:
            allowed_table_names = {_clean_identifier_name(_bare_name(t)) for t in scope.tables}
            shadowed_aliases = cte_aliases & allowed_table_names
            if shadowed_aliases:
                logger.warning(
                    "CTE alias shadows a declared scope table; keeping it in "
                    "scope validation: %s",
                    sorted(shadowed_aliases),
                )
        referenced = {
            ref for ref in referenced
            if _clean_identifier_name(ref) not in cte_aliases
            or _clean_identifier_name(ref) in shadowed_aliases
        }

    # Empty extraction result on a non-trivial query → fail-closed.
    # Also fires when CTE filtering removed all extracted references (i.e.
    # the parser could not reliably identify the real base tables).
    # See module docstring Pattern 3 edge-case for an example.
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
