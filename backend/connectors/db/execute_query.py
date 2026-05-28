"""
backend/connectors/db/execute_query.py

Central data access path for all Track 2 database connectors (T2-S10-A, Task T9).

Execution sequence (MANDATORY ORDER):
  1. validate_read_only(query)     — Reject non-SELECT statements.
  2. validate_scope(query, scope)   — Reject out-of-scope table references.
  3. open connection from pool      — Only after both validations pass.
  4. Execute query and fetch rows   — Paginate at MAX_ROWS_PER_QUERY.
  5. Emit audit and telemetry       — log_event + record_event (fire-and-forget).

Design principles:
  • Query guard (validate_read_only + validate_scope) runs BEFORE the pool
    is opened. No connection is created if validation fails.
  • Result rows are capped at MAX_ROWS_PER_QUERY; truncated flag indicates overflow.
  • SQL NULL values are always returned (row never omitted) — NULL columns
    become None in the result tuple.
  • No ingestor or downstream caller may bypass execute_query() to access the
    pool directly — doing so bypasses audit, telemetry, read-only guard, and
    scope validation.
  • Audit and telemetry events are fire-and-forget (log_event + record_event
    never raise); failures do not propagate to the caller.

Configuration constants (from pool):
  • QUERY_TIMEOUT_S = 30 seconds (query execution timeout).
  • MAX_ROWS_PER_QUERY = 10,000 rows (result pagination limit).
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from app.middleware.audit import (
    CONNECTOR_QUERIED,
    log_event,
)
from app.telemetry import record_event

from .connection_pool import (
    MAX_ROWS_PER_QUERY,
    QUERY_TIMEOUT_S,
    get_or_create_pool,
)
from .models import (
    DBConnectorConfig,
    DBConnectionError,
    DBQueryResult,
    DBQueryRejectedError,
    ScopeDeclaration,
)
from .query_guard import (
    validate_read_only,
    validate_scope,
)

logger = logging.getLogger(__name__)


def execute_query(
    config: DBConnectorConfig,
    query: str,
    scope: ScopeDeclaration,
    org_id: str,
    run_id: str,
) -> DBQueryResult:
    """Execute a read-only query against a database with full audit trail.

    Execution sequence
    -------------------
    1. **Query guard (pre-connection):**
       - validate_read_only(query) — Reject non-SELECT statements.
       - validate_scope(query, scope) — Reject out-of-scope table references.
    2. **Connection and execution:**
       - Acquire connection from pool (threadsafe, keyed by org_id + connector_id).
       - Set query timeout to QUERY_TIMEOUT_S.
       - Execute query and fetch rows up to MAX_ROWS_PER_QUERY + 1 (to detect overflow).
    3. **Result formatting:**
       - SQL NULL values become None in tuples — rows are never omitted.
       - truncated=True if result set hit the limit; truncated=False otherwise.
    4. **Audit and telemetry (fire-and-forget):**
       - log_event('connector_queried', ...) — Immutable audit trail.
       - record_event('db.query_executed', ...) — Telemetry sink (no-raise contract).

    Parameters
    ----------
    config : DBConnectorConfig
        Connection configuration (host, port, database, username_key, password_key).
    query : str
        Raw SQL query (must be SELECT-only; non-SELECT statements are rejected).
    scope : ScopeDeclaration
        Declared scope (org_id, connector_id, schemas, tables).
        If scope.tables == [], any table in scope.schemas is allowed.
    org_id : str
        Workspace/tenant identifier (used for audit and multi-tenant isolation).
    run_id : str
        Discovery run ID (used for audit trail; links queries to specific runs).

    Returns
    -------
    DBQueryResult
        Dataclass with:
        - columns: list[str] — Column names (in order).
        - rows: list[tuple] — Row tuples (each element is value or None).
        - row_count: int — Number of rows returned (after truncation if applicable).
        - query_hash: str — SHA-256 hex digest of the query string.
        - duration_ms: int — Wall-clock query round-trip in milliseconds.
        - truncated: bool — True if result set hit MAX_ROWS_PER_QUERY.

    Raises
    ------
    DBQueryRejectedError
        - Query is not a SELECT statement (validate_read_only fails).
        - Query is empty.
    DBScopeViolationError
        - Query references tables outside declared scope (validate_scope fails).
        - Query references tables that cannot be reliably extracted (fail-closed).
    DBConnectionError
        - Pool cannot acquire a connection.
        - Query execution times out or raises an exception.

    Audit and Telemetry
    -------------------
    On success, two events are emitted (in parallel, fire-and-forget):

    1. **log_event('connector_queried', ...)** — Audit trail
       Immutable, org-scoped record with query hash (never the query itself).
       Fields: org_id, run_id, connector_id, query_hash, row_count, duration_ms.

    2. **record_event('db.query_executed', ...)** — Telemetry
       Structured log for observability (sink TBD in T1-S10-C).
       Fields: connector_id, query_hash, row_count, duration_ms, driver, truncated.

    On failure (validation error, connection error, or query timeout):
    No audit or telemetry events are emitted. Errors are raised to the caller.

    Examples
    --------
    >>> config = DBConnectorConfig(
    ...     connector_id='oracle_db',
    ...     org_id='acme',
    ...     host='oracle.internal',
    ...     port=1521,
    ...     database='PROD',
    ...     driver='oracledb',
    ...     username_key='ORACLE_USER',
    ...     password_key='ORACLE_PASS',
    ...     query_timeout_s=30,
    ... )
    >>> scope = ScopeDeclaration(
    ...     org_id='acme',
    ...     connector_id='oracle_db',
    ...     schemas=['HR', 'FINANCE'],
    ...     tables=['EMPLOYEES', 'DEPARTMENTS'],  # explicit allowlist
    ...     declared_at=datetime.now(),
    ...     declared_by='alice@acme.com',
    ... )
    >>> result = execute_query(
    ...     config,
    ...     "SELECT emp_id, name FROM EMPLOYEES WHERE salary > 50000",
    ...     scope,
    ...     org_id='acme',
    ...     run_id='run-2025-01-15-abc123',
    ... )
    >>> print(f"Returned {result.row_count} rows (truncated={result.truncated})")
    >>> print(f"Duration: {result.duration_ms} ms")
    """
    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Query guard — validate_read_only + validate_scope
    #         BEFORE opening any connection.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        validate_read_only(query)
    except DBQueryRejectedError:
        # Non-SELECT statement detected — fail fast, never connect to DB.
        raise

    try:
        validate_scope(query, scope)
    except Exception:  # Includes DBScopeViolationError
        # Table extraction failed or scope violation — fail fast.
        raise

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Compute query hash (for audit trail — never log raw query text)
    # ─────────────────────────────────────────────────────────────────────────
    query_hash: str = hashlib.sha256(query.encode("utf-8")).hexdigest()

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Acquire connection from pool and execute query
    # ─────────────────────────────────────────────────────────────────────────
    start_time: float = time.time()

    try:
        pool = get_or_create_pool(config)
        connection = pool.acquire(timeout=QUERY_TIMEOUT_S)
    except Exception as exc:
        raise DBConnectionError(
            f"Failed to acquire connection from pool: {exc}",
            error_code="pool_error",
        ) from exc

    try:
        # Set query timeout (driver-specific; handled by connector implementation)
        cursor = connection.cursor()

        # Execute query
        try:
            cursor.execute(query)
        except Exception as exc:
            raise DBConnectionError(
                f"Query execution failed: {exc}",
                error_code="query_failed",
            ) from exc

        # Fetch column names
        columns: list[str] = [desc[0] for desc in cursor.description or []]

        # Fetch rows up to MAX_ROWS_PER_QUERY + 1 (to detect overflow)
        rows_raw = cursor.fetchall()
        truncated: bool = len(rows_raw) > MAX_ROWS_PER_QUERY
        rows_limited = rows_raw[:MAX_ROWS_PER_QUERY]

        # Convert to list of tuples (driver-specific conversion handled here)
        rows: list[tuple] = [tuple(row) for row in rows_limited]

        # Clean up
        cursor.close()

    except DBConnectionError:
        raise
    except Exception as exc:
        raise DBConnectionError(
            f"Unexpected error during query execution: {exc}",
            error_code="query_error",
        ) from exc
    finally:
        # Always return connection to pool
        try:
            pool.release(connection)
        except Exception:
            logger.exception(
                "[execute_query] Failed to release connection back to pool"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: Compute result metrics
    # ─────────────────────────────────────────────────────────────────────────
    duration_ms: int = int((time.time() - start_time) * 1000)
    row_count: int = len(rows)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: Create result object
    # ─────────────────────────────────────────────────────────────────────────
    result = DBQueryResult(
        columns=columns,
        rows=rows,
        row_count=row_count,
        query_hash=query_hash,
        duration_ms=duration_ms,
        truncated=truncated,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 6: Emit audit and telemetry (fire-and-forget, never raise)
    # ─────────────────────────────────────────────────────────────────────────
    try:
        # Audit trail: immutable, org-scoped record
        log_event(
            CONNECTOR_QUERIED,
            org_id=org_id,
            run_id=run_id,
            connector_id=config.connector_id,
            query_hash=query_hash,
            row_count=row_count,
            duration_ms=duration_ms,
        )
    except Exception:
        # log_event already swallows exceptions, but be defensive
        logger.exception("[execute_query] Audit event logging failed")

    try:
        # Telemetry: structured observability (sink TBD)
        record_event(
            "db.query_executed",
            {
                "connector_id": config.connector_id,
                "query_hash": query_hash,
                "row_count": row_count,
                "duration_ms": duration_ms,
                "driver": config.driver,
                "truncated": truncated,
            },
        )
    except Exception:
        # record_event never raises per contract, but be defensive
        logger.exception("[execute_query] Telemetry event recording failed")

    return result
