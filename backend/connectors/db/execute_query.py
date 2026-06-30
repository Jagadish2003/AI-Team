"""Central read-only query execution path for T2-S10-A DB connectors."""

from __future__ import annotations

import hashlib
import importlib
import time
from typing import Any

try:
    from backend.app.middleware.audit import CONNECTOR_QUERIED, log_event
    from backend.app.telemetry import record_event
except ModuleNotFoundError:
    from app.middleware.audit import CONNECTOR_QUERIED, log_event
    from app.telemetry import record_event

from .connection_pool import (
    MAX_ROWS_PER_QUERY,
    QUERY_TIMEOUT_S,
    _driver_factories,
    get_or_create_pool,
    register_driver,
)
from .models import (
    DBConnectionError,
    DBConnectorConfig,
    DBQueryResult,
    ScopeDeclaration,
)
from .query_guard import validate_read_only, validate_scope


_DRIVER_MODULES: dict[str, tuple[str, str]] = {
    "sqlserver": ("sqlserver", "create_sqlserver_connection"),
    "oracle_db": ("oracle", "create_oracle_connection"),
    "postgresql": ("postgresql", "create_postgresql_connection"),
}


def execute_query(
    config: DBConnectorConfig,
    *args: Any,
    query: str | None = None,
    scope: ScopeDeclaration | None = None,
    org_id: str | None = None,
    run_id: str | None = None,
) -> DBQueryResult:
    """Validate, execute, audit, and telemetry-record a read-only DB query.

    Supports both call forms used by the in-flight branches:
    ``execute_query(config, scope, query, org_id, run_id)`` and
    ``execute_query(config=config, query=query, scope=scope, ...)``.
    """
    query, scope, org_id, run_id = _normalise_call_args(
        args=args,
        query=query,
        scope=scope,
        org_id=org_id,
        run_id=run_id,
    )

    validate_read_only(query)
    validate_scope(query, scope)
    _ensure_driver_imported(config.connector_id)

    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    start = time.monotonic()
    pool = None
    connection = None
    cursor = None

    try:
        pool = get_or_create_pool(config)
        connection = pool.acquire(timeout=QUERY_TIMEOUT_S)
        cursor = connection.cursor()
        cursor.execute(query)

        raw_rows = _fetch_limited_rows(cursor)
        truncated = len(raw_rows) > MAX_ROWS_PER_QUERY
        rows = [_as_tuple(row) for row in raw_rows[:MAX_ROWS_PER_QUERY]]
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
        raise DBConnectionError(
            f"Query execution failed for connector '{config.connector_id}': {type(exc).__name__}",
            error_code="query_timeout" if _looks_like_timeout(exc) else "query_failed",
        ) from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None and pool is not None:
            pool.release(connection)

    try:
        log_event(
            CONNECTOR_QUERIED,
            org_id=org_id,
            run_id=run_id,
            connector_id=config.connector_id,
            query_hash=query_hash,
            row_count=result.row_count,
            duration_ms=result.duration_ms,
        )
    except Exception:
        pass

    try:
        record_event(
            "db.query_executed",
            {
                "org_id": org_id,
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


def _normalise_call_args(
    *,
    args: tuple[Any, ...],
    query: str | None,
    scope: ScopeDeclaration | None,
    org_id: str | None,
    run_id: str | None,
) -> tuple[str, ScopeDeclaration, str, str]:
    if args:
        if len(args) != 4:
            raise TypeError(
                "execute_query positional form requires scope/query/org_id/run_id"
            )
        first, second, arg_org_id, arg_run_id = args
        org_id = arg_org_id if org_id is None else org_id
        run_id = arg_run_id if run_id is None else run_id
        if _is_scope_declaration(first):
            scope = first if scope is None else scope
            query = second if query is None else query
        else:
            query = first if query is None else query
            scope = second if scope is None else scope

    if query is None or scope is None or org_id is None or run_id is None:
        raise TypeError("execute_query requires query, scope, org_id, and run_id")
    if not isinstance(query, str):
        raise TypeError("execute_query query must be a string")
    if not _is_scope_declaration(scope):
        raise TypeError("execute_query scope must be a ScopeDeclaration")
    return query, scope, str(org_id), str(run_id)


def _is_scope_declaration(value: Any) -> bool:
    """Accept equivalent ScopeDeclaration classes from app/backend import paths."""
    return all(
        hasattr(value, attr)
        for attr in ("org_id", "connector_id", "schemas", "tables")
    )


def _ensure_driver_imported(connector_id: str) -> None:
    if connector_id in _driver_factories:
        return
    driver = _DRIVER_MODULES.get(connector_id)
    if driver is None:
        return
    module_name, factory_name = driver
    try:
        module = importlib.import_module(f"backend.connectors.db.{module_name}")
    except ModuleNotFoundError:
        module = importlib.import_module(f"connectors.db.{module_name}")
    if connector_id not in _driver_factories:
        register_driver(connector_id, getattr(module, factory_name))


def _fetch_limited_rows(cursor: Any) -> list[Any]:
    fetchmany = getattr(cursor, "fetchmany", None)
    if callable(fetchmany):
        rows = fetchmany(MAX_ROWS_PER_QUERY + 1)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return list(cursor.fetchall())[: MAX_ROWS_PER_QUERY + 1]


def _cursor_columns(cursor: Any) -> list[str]:
    columns: list[str] = []
    for column in getattr(cursor, "description", None) or []:
        if isinstance(column, (list, tuple)) and column:
            columns.append(str(column[0]))
        else:
            columns.append(str(getattr(column, "name", column)))
    return columns


def _as_tuple(row: Any) -> tuple:
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
