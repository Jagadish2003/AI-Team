"""Public DB connectivity service for T2-S10-A.

This module is the framework-level data access path described by the
T2_S10_A_DBConnectivity contract. It composes the query guard, isolated pool
factory, driver schema discovery helpers, audit logging, and telemetry.
"""

from __future__ import annotations

import hashlib
import importlib
import time
from typing import Any

from .connection_pool import (
    MAX_ROWS_PER_QUERY,
    _driver_factories,
    get_or_create_pool,
    register_driver,
)
from .models import (
    DBConnectionError,
    DBConnectorConfig,
    DBQueryResult,
    SchemaDiscoveryResult,
    ScopeDeclaration,
)
from .query_guard import validate_read_only, validate_scope

try:
    from backend.app.middleware.audit import (
        CONNECTOR_QUERIED,
        SCHEMA_DISCOVERED,
        log_event,
    )
    from backend.app.telemetry import record_event
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.middleware.audit import (
        CONNECTOR_QUERIED,
        SCHEMA_DISCOVERED,
        log_event,
    )
    from app.telemetry import record_event


_DRIVER_MODULES: dict[str, str] = {
    "sqlserver": "sqlserver",
    "oracle_db": "oracle",
    "postgresql": "postgresql",
}


def execute_query(
    config: DBConnectorConfig,
    scope: ScopeDeclaration,
    query: str,
    org_id: str,
    run_id: str,
) -> DBQueryResult:
    """Validate and execute a read-only, in-scope database query.

    The guard runs before pool access, so rejected write or out-of-scope
    queries never open a database connection.
    """
    validate_read_only(query)
    validate_scope(query, scope)

    _ensure_driver_imported(config.connector_id)

    query_hash = hashlib.sha256(query.encode()).hexdigest()
    start = time.monotonic()
    pool = None
    conn = None
    cursor = None

    try:
        pool = get_or_create_pool(config)
        conn = pool.acquire()
        cursor = conn.cursor()
        cursor.execute(query)

        raw_rows = _fetch_limited_rows(cursor)
        truncated = len(raw_rows) > MAX_ROWS_PER_QUERY
        rows = [_coerce_row(row) for row in raw_rows[:MAX_ROWS_PER_QUERY]]
        duration_ms = max(0, int((time.monotonic() - start) * 1000))
        result = DBQueryResult(
            columns=_cursor_columns(cursor),
            rows=rows,
            row_count=len(rows),
            query_hash=query_hash,
            duration_ms=duration_ms,
            truncated=truncated,
        )
    except DBConnectionError:
        raise
    except Exception as exc:
        error_code = "query_timeout" if _looks_like_timeout(exc) else "query_failed"
        raise DBConnectionError(
            f"DB query failed for connector '{config.connector_id}': {type(exc).__name__}",
            error_code=error_code,
        ) from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None and pool is not None:
            pool.release(conn)

    log_event(
        CONNECTOR_QUERIED,
        org_id=org_id,
        run_id=run_id,
        connector_id=config.connector_id,
        query_hash=query_hash,
        row_count=result.row_count,
        duration_ms=result.duration_ms,
    )

    try:
        record_event(
            "db.query_executed",
            {
                "connector_id": config.connector_id,
                "query_hash": query_hash,
                "row_count": result.row_count,
                "duration_ms": result.duration_ms,
                "driver": config.driver,
                "truncated": result.truncated,
            },
        )
    except Exception:
        pass

    return result


def discover_schema(config: DBConnectorConfig, org_id: str) -> SchemaDiscoveryResult:
    """Discover schema metadata using driver system-catalogue queries only."""
    _ensure_driver_imported(config.connector_id)

    pool = None
    conn = None
    try:
        pool = get_or_create_pool(config)
        conn = pool.acquire(timeout=config.connect_timeout_s)
        result = _discover_schema_for_connector(conn, config.connector_id)
    finally:
        if conn is not None and pool is not None:
            pool.release(conn)

    log_event(
        SCHEMA_DISCOVERED,
        org_id=org_id,
        connector_id=config.connector_id,
        schema_count=len(result.schemas),
        table_count=len(result.tables),
    )
    try:
        from .scope import save_discovered_schema

        save_discovered_schema(org_id, config.connector_id, result)
    except Exception:
        pass
    return result


def _ensure_driver_imported(connector_id: str) -> None:
    if connector_id in _driver_factories:
        return

    module_name = _DRIVER_MODULES.get(connector_id)
    if module_name is None:
        return

    try:
        module = importlib.import_module(f"backend.connectors.db.{module_name}")
    except ModuleNotFoundError:
        module = importlib.import_module(f"connectors.db.{module_name}")

    if connector_id == "sqlserver" and connector_id not in _driver_factories:
        register_driver(connector_id, module.create_sqlserver_connection)
    elif connector_id == "oracle_db" and connector_id not in _driver_factories:
        register_driver(connector_id, module.create_oracle_connection)
    elif connector_id == "postgresql" and connector_id not in _driver_factories:
        register_driver(connector_id, module.create_postgresql_connection)


def _discover_schema_for_connector(conn: Any, connector_id: str) -> SchemaDiscoveryResult:
    if connector_id == "sqlserver":
        try:
            from backend.connectors.db.sqlserver import discover_schema_sqlserver
        except ModuleNotFoundError:
            from connectors.db.sqlserver import discover_schema_sqlserver

        return discover_schema_sqlserver(conn)

    if connector_id == "oracle_db":
        try:
            from backend.connectors.db.oracle import discover_schema_oracle
        except ModuleNotFoundError:
            from connectors.db.oracle import discover_schema_oracle

        return discover_schema_oracle(conn)

    if connector_id == "postgresql":
        try:
            from backend.connectors.db.postgresql import discover_schema_postgresql
        except ModuleNotFoundError:
            from connectors.db.postgresql import discover_schema_postgresql

        return discover_schema_postgresql(conn)

    raise DBConnectionError(
        f"Schema discovery is not registered for connector_id='{connector_id}'.",
        error_code="driver_not_registered",
    )


def _fetch_limited_rows(cursor: Any) -> list[Any]:
    limit = MAX_ROWS_PER_QUERY + 1
    if hasattr(cursor, "fetchmany"):
        return list(cursor.fetchmany(limit))
    return list(cursor.fetchall())[:limit]


def _cursor_columns(cursor: Any) -> list[str]:
    description = getattr(cursor, "description", None) or []
    columns: list[str] = []
    for col in description:
        if isinstance(col, (tuple, list)) and col:
            columns.append(str(col[0]))
        else:
            columns.append(str(getattr(col, "name", col)))
    return columns


def _coerce_row(row: Any) -> tuple:
    if isinstance(row, tuple):
        return row
    if isinstance(row, list):
        return tuple(row)
    try:
        return tuple(row)
    except TypeError:
        return (row,)


def _looks_like_timeout(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" in text or "timed out" in text
