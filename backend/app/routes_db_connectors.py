"""
T2-S10-A — DB Connector API Routes

Implements four HTTP routes exposing database connectivity features:

  GET  /api/db-connectors/{connector_id}/schema
       Discovers the DB schema (system catalogue only, no customer data).
       Returns SchemaDiscoveryResult.  Requires Analyst role.

  POST /api/db-connectors/{connector_id}/scope
       Saves the approved table scope for a connector.
       Returns 201 Created.  Requires Analyst role.

  GET  /api/db-connectors/{connector_id}/scope
       Returns the previously saved scope, or 404 if not declared.
       Requires Analyst role.

  GET  /api/db-connectors/{connector_id}/connection-test
       Opens a pool connection (honours connect_timeout_s for TCP) and runs
       SELECT 1 (honours QUERY_TIMEOUT_S).
       Returns {connected: bool, error_code: str | null}.  Requires Analyst role.

All routes require Depends(require_auth) + Depends(require_role("analyst")).
401 is returned for missing/invalid tokens; 403 for insufficient role.

KV storage layout
-----------------
  db_connector_config:{org_id}:{connector_id}  →  serialised DBConnectorConfig dict
  db_connector_scope:{org_id}:{connector_id}   →  serialised ScopeDeclaration dict

Registration
------------
Call register_db_connector_routes(app) once from main.py.
The function is idempotent — a module-level flag prevents double registration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import db as _db
from .db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
    ScopeDeclaration,
)
from .middleware.audit import SCHEMA_DISCOVERED, SCOPE_DECLARED, log_event
from .security import require_auth, require_role

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# KV key prefixes for connector-scoped storage
_KV_CONFIG_PREFIX: str = "db_connector_config"
_KV_SCOPE_PREFIX: str = "db_connector_scope"

# Default org_id used in dev/single-tenant mode.
# Production: derive from authenticated JWT claims.
_DEV_ORG_ID: str = "default"

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class ScopeRequest(BaseModel):
    """Body for POST /api/db-connectors/{connector_id}/scope."""

    org_id: str = _DEV_ORG_ID
    schemas: List[str]
    tables: List[str] = []


class ScopeResponse(BaseModel):
    """Response for GET /api/db-connectors/{connector_id}/scope."""

    org_id: str
    connector_id: str
    schemas: List[str]
    tables: List[str]
    declared_at: str
    declared_by: str


class ConnectionTestResponse(BaseModel):
    """Response for GET /api/db-connectors/{connector_id}/connection-test."""

    connected: bool
    error_code: Optional[str] = None


class SchemaDiscoveryResponse(BaseModel):
    """Serialisable form of SchemaDiscoveryResult."""

    schemas: List[str]
    tables: List[Dict[str, str]]
    columns: List[Dict[str, str]]
    estimated_row_counts: Optional[Dict[str, int]] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _kv_config_key(org_id: str, connector_id: str) -> str:
    return f"{_KV_CONFIG_PREFIX}:{org_id}:{connector_id}"


def _kv_scope_key(org_id: str, connector_id: str) -> str:
    return f"{_KV_SCOPE_PREFIX}:{org_id}:{connector_id}"


def _load_config(org_id: str, connector_id: str) -> Optional[DBConnectorConfig]:
    """Load a DBConnectorConfig from the KV store, or None if not found."""
    raw: Optional[Dict[str, Any]] = _db.kv_get(_kv_config_key(org_id, connector_id))
    if raw is None:
        return None
    try:
        return DBConnectorConfig(**raw)
    except (TypeError, KeyError):
        return None


def _schema_discovery_result_to_response(
    result: SchemaDiscoveryResult,
) -> SchemaDiscoveryResponse:
    return SchemaDiscoveryResponse(
        schemas=result.schemas,
        tables=[{"schema": t.schema, "table": t.table} for t in result.tables],
        columns=[
            {"schema": c.schema, "table": c.table, "column": c.column}
            for c in result.columns
        ],
        estimated_row_counts=result.estimated_row_counts,
    )


def _discover_schema_for_connector(conn: Any, connector_id: str) -> SchemaDiscoveryResult:
    """Dispatch schema discovery to the correct driver implementation.

    SQL Server, Oracle DB, and PostgreSQL are implemented.
    Raises DBConnectionError if schema discovery fails.
    """
    if connector_id == "sqlserver":
        try:
            from backend.connectors.db.sqlserver import discover_schema_sqlserver  # noqa: PLC0415
        except ModuleNotFoundError:
            from connectors.db.sqlserver import discover_schema_sqlserver  # type: ignore[no-redef] # noqa: PLC0415
        return discover_schema_sqlserver(conn)

    elif connector_id == "oracle_db":
        try:
            from backend.connectors.db.oracle import discover_schema_oracle  # noqa: PLC0415
        except ModuleNotFoundError:
            from connectors.db.oracle import discover_schema_oracle  # type: ignore[no-redef] # noqa: PLC0415
        return discover_schema_oracle(conn)

    elif connector_id == "postgresql":
        try:
            from backend.connectors.db.postgresql import discover_schema_postgresql  # noqa: PLC0415
        except ModuleNotFoundError:
            from connectors.db.postgresql import discover_schema_postgresql  # type: ignore[no-redef] # noqa: PLC0415
        return discover_schema_postgresql(conn)

    raise DBConnectionError(
        f"Unknown connector type: {connector_id!r}",
        error_code="unknown_connector",
    )


def _run_test_query(conn: Any, connector_id: str) -> None:
    """Execute a trivial SELECT 1 (or dialect equivalent) to verify query path.

    Raises DBConnectionError on failure.  The connection object's own
    query_timeout_s (set by the driver on connect) bounds execution time.
    """
    test_sql = "SELECT 1 FROM DUAL" if connector_id == "oracle_db" else "SELECT 1"
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(test_sql)
            cursor.fetchone()
        finally:
            cursor.close()
    except DBConnectionError:
        raise
    except Exception as exc:
        raise DBConnectionError(
            f"Test query failed on connector '{connector_id}': {type(exc).__name__}",
            error_code="query_failed",
        ) from exc


# ---------------------------------------------------------------------------
# Router definition
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/db-connectors",
    tags=["db-connectors"],
)


# ── 1. GET schema ────────────────────────────────────────────────────────────


@router.get(
    "/{connector_id}/schema",
    response_model=SchemaDiscoveryResponse,
    summary="Discover database schema for a connector",
    description=(
        "Reads system catalogue tables only — no customer data is accessed. "
        "Emits a schema_discovered audit event on success. "
        "Requires Analyst role."
    ),
)
def get_schema(
    connector_id: str,
    token: str = Depends(require_auth),
    _role: str = Depends(require_role("analyst")),
) -> SchemaDiscoveryResponse:
    org_id = _DEV_ORG_ID

    config = _load_config(org_id, connector_id)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Connector '{connector_id}' is not registered for org '{org_id}'.",
        )

    # Lazy-import pool factory to keep the route file testable without live DB
    try:
        from backend.connectors.db.connection_pool import get_or_create_pool  # noqa: PLC0415
    except ModuleNotFoundError:  # Runtime inside backend/ where connectors is top-level.
        from connectors.db.connection_pool import get_or_create_pool  # type: ignore[no-redef] # noqa: PLC0415

    try:
        pool = get_or_create_pool(config)
        conn = pool.acquire(timeout=config.connect_timeout_s)
    except DBConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not connect to '{connector_id}': {exc.error_code}",
        ) from exc

    try:
        result = _discover_schema_for_connector(conn, connector_id)
    except DBConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Schema discovery failed for '{connector_id}': {exc.error_code}",
        ) from exc
    finally:
        pool.release(conn)

    # Emit audit event — fail-silent (log_event swallows exceptions)
    log_event(
        SCHEMA_DISCOVERED,
        org_id=org_id,
        connector_id=connector_id,
        schema_count=len(result.schemas),
        table_count=len(result.tables),
    )

    return _schema_discovery_result_to_response(result)


# ── 2. POST scope ────────────────────────────────────────────────────────────


@router.post(
    "/{connector_id}/scope",
    status_code=201,
    summary="Save approved table scope for a connector",
    description=(
        "Declares which schemas and tables are approved for query execution. "
        "scope.tables=[] means any table in the declared schemas is permitted; "
        "scope enforcement still verifies schema membership. "
        "Emits a scope_declared audit event. "
        "Requires Analyst role."
    ),
)
def post_scope(
    connector_id: str,
    body: ScopeRequest,
    response: Response,
    token: str = Depends(require_auth),
    _role: str = Depends(require_role("analyst")),
) -> Dict[str, Any]:
    org_id = body.org_id
    now_iso = datetime.now(timezone.utc).isoformat()

    scope = ScopeDeclaration(
        org_id=org_id,
        connector_id=connector_id,
        schemas=body.schemas,
        tables=body.tables,
        declared_at=datetime.now(timezone.utc),
        declared_by="analyst",  # Production: extract identity from JWT
    )

    _db.kv_set(
        _kv_scope_key(org_id, connector_id),
        {
            "org_id": scope.org_id,
            "connector_id": scope.connector_id,
            "schemas": scope.schemas,
            "tables": scope.tables,
            "declared_at": now_iso,
            "declared_by": scope.declared_by,
        },
    )

    # Emit audit event — fail-silent
    log_event(
        SCOPE_DECLARED,
        org_id=org_id,
        connector_id=connector_id,
        schemas=scope.schemas,
        tables=scope.tables,
    )

    response.status_code = 201
    return {"status": "scope_saved", "connector_id": connector_id, "org_id": org_id}


# ── 3. GET scope ─────────────────────────────────────────────────────────────


@router.get(
    "/{connector_id}/scope",
    response_model=ScopeResponse,
    summary="Retrieve the saved scope for a connector",
    description=(
        "Returns the most recently saved scope declaration, or 404 if no scope "
        "has been declared for this connector. "
        "Requires Analyst role."
    ),
)
def get_scope(
    connector_id: str,
    token: str = Depends(require_auth),
    _role: str = Depends(require_role("analyst")),
) -> ScopeResponse:
    org_id = _DEV_ORG_ID

    raw: Optional[Dict[str, Any]] = _db.kv_get(_kv_scope_key(org_id, connector_id))
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"No scope declared for connector '{connector_id}' in org '{org_id}'.",
        )

    return ScopeResponse(
        org_id=raw["org_id"],
        connector_id=raw["connector_id"],
        schemas=raw["schemas"],
        tables=raw["tables"],
        declared_at=raw["declared_at"],
        declared_by=raw["declared_by"],
    )


# ── 4. GET connection-test ───────────────────────────────────────────────────


@router.get(
    "/{connector_id}/connection-test",
    response_model=ConnectionTestResponse,
    summary="Test live database connection for a connector",
    description=(
        "Attempts a TCP connection (bounded by connect_timeout_s from the stored "
        "connector config) then runs SELECT 1 (bounded by QUERY_TIMEOUT_S). "
        "Always returns a 200 response — the connected field signals success or "
        "failure, and error_code provides a machine-readable failure reason. "
        "Requires Analyst role."
    ),
)
def get_connection_test(
    connector_id: str,
    token: str = Depends(require_auth),
    _role: str = Depends(require_role("analyst")),
) -> ConnectionTestResponse:
    org_id = _DEV_ORG_ID

    config = _load_config(org_id, connector_id)
    if config is None:
        return ConnectionTestResponse(connected=False, error_code="config_not_found")

    try:
        from backend.connectors.db.connection_pool import get_or_create_pool  # noqa: PLC0415
    except ModuleNotFoundError:  # Runtime inside backend/ where connectors is top-level.
        from connectors.db.connection_pool import get_or_create_pool  # type: ignore[no-redef] # noqa: PLC0415

    conn = None
    pool = None
    try:
        pool = get_or_create_pool(config)
        conn = pool.acquire(timeout=config.connect_timeout_s)
        _run_test_query(conn, connector_id)
        return ConnectionTestResponse(connected=True, error_code=None)
    except DBConnectionError as exc:
        return ConnectionTestResponse(connected=False, error_code=exc.error_code)
    except Exception as exc:
        return ConnectionTestResponse(connected=False, error_code="connection_failed")
    finally:
        if conn is not None and pool is not None:
            pool.release(conn)


# ---------------------------------------------------------------------------
# Idempotent registration guard
# ---------------------------------------------------------------------------

_ROUTES_REGISTERED: bool = False


def register_db_connector_routes(app: FastAPI) -> None:
    """Register the DB connector router with *app*.

    Idempotent: calling this function more than once is safe — duplicate
    registration is detected via a module-level flag and silently skipped.
    This matches the pattern used by other route modules in main.py.
    """
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return
    _ROUTES_REGISTERED = True
    app.include_router(router)
