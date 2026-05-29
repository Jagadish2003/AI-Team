"""Scope management for T2-S10-A database connectors.

Scope declarations are keyed by ``(org_id, connector_id)`` and are validated
against the most recently discovered schema before they are saved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException

try:
    from backend.app import db as _db
    from backend.app.db_connectors.models import (
        ColumnMeta,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        TableMeta,
    )
    from backend.app.middleware.audit import SCOPE_DECLARED, log_event
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app import db as _db
    from app.db_connectors.models import (
        ColumnMeta,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        TableMeta,
    )
    from app.middleware.audit import SCOPE_DECLARED, log_event


_KV_SCOPE_PREFIX = "db_connector_scope"
_KV_DISCOVERED_SCHEMA_PREFIX = "db_connector_discovered_schema"


def save_discovered_schema(
    org_id: str,
    connector_id: str,
    result: SchemaDiscoveryResult,
) -> None:
    """Persist the latest discovered schema for later scope validation."""
    _db.kv_set(
        _discovered_schema_key(org_id, connector_id),
        _schema_result_to_payload(result),
    )


def get_discovered_schema(
    org_id: str,
    connector_id: str,
) -> SchemaDiscoveryResult | None:
    """Return the last schema discovery result for this org/connector."""
    raw = _db.kv_get(_discovered_schema_key(org_id, connector_id))
    if raw is None:
        return None
    return _schema_result_from_payload(raw)


def save_scope(
    scope: ScopeDeclaration,
    discovered_schema: SchemaDiscoveryResult | None = None,
) -> ScopeDeclaration:
    """Validate and save an approved scope declaration.

    ``scope.tables == []`` is a deliberate operator choice meaning any table in
    the declared schemas may be queried later. It is not a validation bypass:
    the declared schemas still must exist in the discovered schema.

    Raises:
        HTTPException(400): no discovered schema, unknown schema, or unknown
        table.
    """
    schema = discovered_schema or get_discovered_schema(
        scope.org_id,
        scope.connector_id,
    )
    if schema is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "schema_not_discovered",
                "message": "Discover schema before saving connector scope.",
            },
        )

    _validate_scope_against_schema(scope, schema)

    _db.kv_set(_scope_key(scope.org_id, scope.connector_id), _scope_to_payload(scope))
    log_event(
        SCOPE_DECLARED,
        org_id=scope.org_id,
        connector_id=scope.connector_id,
        schemas=scope.schemas,
        tables=scope.tables,
    )
    return scope


def get_scope(org_id: str, connector_id: str) -> ScopeDeclaration:
    """Return the current approved scope or raise HTTP 404."""
    raw = _db.kv_get(_scope_key(org_id, connector_id))
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"No scope declared for connector '{connector_id}' in org '{org_id}'.",
        )
    return _scope_from_payload(raw)


def _validate_scope_against_schema(
    scope: ScopeDeclaration,
    discovered_schema: SchemaDiscoveryResult,
) -> None:
    available_schemas = _available_schemas(discovered_schema)
    declared_schemas = {schema.lower() for schema in scope.schemas}

    unknown_schemas = [
        schema for schema in scope.schemas if schema.lower() not in available_schemas
    ]
    if unknown_schemas:
        raise _bad_scope(
            error_code="unknown_schema",
            unknown_schemas=unknown_schemas,
            unknown_tables=[],
        )

    if not scope.tables:
        return

    available_tables = _available_tables(discovered_schema)
    unknown_tables: list[str] = []
    for table in scope.tables:
        candidates = _candidate_table_keys(table, declared_schemas)
        if not candidates or not any(candidate in available_tables for candidate in candidates):
            unknown_tables.append(table)

    if unknown_tables:
        raise _bad_scope(
            error_code="unknown_table",
            unknown_schemas=[],
            unknown_tables=unknown_tables,
        )


def _bad_scope(
    *,
    error_code: str,
    unknown_schemas: list[str],
    unknown_tables: list[str],
) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error_code": error_code,
            "unknown_schemas": unknown_schemas,
            "unknown_tables": unknown_tables,
        },
    )


def _scope_key(org_id: str, connector_id: str) -> str:
    return f"{_KV_SCOPE_PREFIX}:{org_id}:{connector_id}"


def _discovered_schema_key(org_id: str, connector_id: str) -> str:
    return f"{_KV_DISCOVERED_SCHEMA_PREFIX}:{org_id}:{connector_id}"


def _available_schemas(discovered_schema: SchemaDiscoveryResult) -> set[str]:
    schemas = {schema.lower() for schema in discovered_schema.schemas}
    schemas.update(table.schema.lower() for table in discovered_schema.tables)
    schemas.update(column.schema.lower() for column in discovered_schema.columns)
    return schemas


def _available_tables(discovered_schema: SchemaDiscoveryResult) -> set[tuple[str, str]]:
    return {
        (table.schema.lower(), table.table.lower())
        for table in discovered_schema.tables
    }


def _candidate_table_keys(
    table: str,
    declared_schemas: set[str],
) -> list[tuple[str, str]]:
    raw = table.strip()
    if not raw:
        return []

    if "." in raw:
        schema_name, table_name = raw.split(".", 1)
        key = (schema_name.strip().lower(), table_name.strip().lower())
        return [key] if key[0] in declared_schemas else []

    return [(schema_name, raw.lower()) for schema_name in declared_schemas]


def _scope_to_payload(scope: ScopeDeclaration) -> dict[str, Any]:
    return {
        "org_id": scope.org_id,
        "connector_id": scope.connector_id,
        "schemas": list(scope.schemas),
        "tables": list(scope.tables),
        "declared_at": scope.declared_at.isoformat(),
        "declared_by": scope.declared_by,
    }


def _scope_from_payload(raw: dict[str, Any]) -> ScopeDeclaration:
    return ScopeDeclaration(
        org_id=raw["org_id"],
        connector_id=raw["connector_id"],
        schemas=list(raw["schemas"]),
        tables=list(raw["tables"]),
        declared_at=datetime.fromisoformat(raw["declared_at"]),
        declared_by=raw["declared_by"],
    )


def _schema_result_to_payload(result: SchemaDiscoveryResult) -> dict[str, Any]:
    return {
        "schemas": list(result.schemas),
        "tables": [
            {"schema": table.schema, "table": table.table}
            for table in result.tables
        ],
        "columns": [
            {
                "schema": column.schema,
                "table": column.table,
                "column": column.column,
            }
            for column in result.columns
        ],
        "estimated_row_counts": result.estimated_row_counts,
    }


def _schema_result_from_payload(raw: dict[str, Any]) -> SchemaDiscoveryResult:
    return SchemaDiscoveryResult(
        schemas=list(raw["schemas"]),
        tables=[
            TableMeta(schema=table["schema"], table=table["table"])
            for table in raw["tables"]
        ],
        columns=[
            ColumnMeta(
                schema=column["schema"],
                table=column["table"],
                column=column["column"],
            )
            for column in raw["columns"]
        ],
        estimated_row_counts=raw.get("estimated_row_counts"),
    )
