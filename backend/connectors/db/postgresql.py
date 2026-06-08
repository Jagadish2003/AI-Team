"""
PostgreSQL connection driver - T2-S10-A Task T5.

Driver: psycopg2-binary.
SSL:    config.ssl_mode or POSTGRESQL_SSL_MODE, limited to require/prefer/disable.
Timeouts:
  connect_timeout_s -> psycopg2.connect(connect_timeout=...)
  query_timeout_s   -> psycopg2 connection options statement_timeout in ms.

Quoted identifiers: use double quotes for schema and table identifiers.
Registers itself with the pool factory at import time via register_driver().
"""

from __future__ import annotations

import os
from typing import Any

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection

from backend.app.db_connectors.models import (
    ColumnMeta,
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
    TableMeta,
)
from backend.connectors.db.connection_pool import register_driver


SSL_MODE_ENV_VAR: str = "POSTGRESQL_SSL_MODE"
ALLOWED_SSL_MODES: frozenset[str] = frozenset({"require", "prefer", "disable"})

TRIVIAL_QUERY: str = "SELECT 1"

CATALOGUE_QUERY: str = (
    "SELECT table_schema, table_name, column_name "
    "FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
)


def quote_identifier(name: str) -> str:
    """Wrap a PostgreSQL identifier in double quotes."""
    return '"' + name.replace('"', '""') + '"'


def qualified_table_name(schema: str, table: str) -> str:
    """Return a schema-qualified table reference with both identifiers quoted."""
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def _resolve_ssl_mode(config: DBConnectorConfig) -> str:
    """Resolve and validate the PostgreSQL sslmode value."""
    raw_mode = config.ssl_mode or os.environ.get(SSL_MODE_ENV_VAR, "prefer")
    ssl_mode = raw_mode.strip().lower()
    if ssl_mode not in ALLOWED_SSL_MODES:
        allowed = ", ".join(sorted(ALLOWED_SSL_MODES))
        raise DBConnectionError(
            f"Invalid PostgreSQL ssl_mode {ssl_mode!r}. Allowed values: {allowed}.",
            error_code="invalid_ssl_mode",
        )
    return ssl_mode


def _statement_timeout_option(config: DBConnectorConfig) -> str:
    timeout_ms = int(config.query_timeout_s) * 1000
    return f"-c statement_timeout={timeout_ms}"


def _connect_kwargs(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Build keyword arguments for psycopg2.connect().

    The returned dict contains credentials and must never be logged.
    """
    return {
        "host": config.host,
        "port": config.port,
        "dbname": config.database,
        "user": username,
        "password": password,
        "connect_timeout": config.connect_timeout_s,
        "sslmode": _resolve_ssl_mode(config),
        "options": _statement_timeout_option(config),
    }


def create_postgresql_connection(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> PsycopgConnection:
    """Create and return a psycopg2 connection to PostgreSQL."""
    kwargs = _connect_kwargs(config, username, password)
    try:
        return psycopg2.connect(**kwargs)
    except DBConnectionError:
        raise
    except psycopg2.OperationalError as exc:
        raise DBConnectionError(
            f"PostgreSQL connection failed [{type(exc).__name__}] "
            f"host={config.host!r} db={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except psycopg2.Error as exc:
        raise DBConnectionError(
            f"PostgreSQL driver error [{type(exc).__name__}] "
            f"host={config.host!r} db={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except Exception as exc:
        raise DBConnectionError(
            f"Unexpected error connecting to PostgreSQL "
            f"host={config.host!r} db={config.database!r}: {type(exc).__name__}",
            error_code="connection_failed",
        ) from exc


def discover_schema_postgresql(conn: PsycopgConnection) -> SchemaDiscoveryResult:
    """Run the information_schema catalogue query and return schema metadata."""
    cursor = conn.cursor()
    try:
        cursor.execute(CATALOGUE_QUERY)
        rows = cursor.fetchall()
    finally:
        cursor.close()

    schemas: set[str] = set()
    tables_seen: set[tuple[str, str]] = set()
    columns: list[ColumnMeta] = []

    for schema_name, table_name, column_name in rows:
        schemas.add(schema_name)
        tables_seen.add((schema_name, table_name))
        columns.append(
            ColumnMeta(schema=schema_name, table=table_name, column=column_name)
        )

    return SchemaDiscoveryResult(
        schemas=sorted(schemas),
        tables=[TableMeta(schema=s, table=t) for s, t in sorted(tables_seen)],
        columns=columns,
        estimated_row_counts=None,
    )


register_driver("postgresql", create_postgresql_connection)
