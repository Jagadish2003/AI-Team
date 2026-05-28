"""
PostgreSQL connection driver — T2-S10-A Task T5.

Driver:  psycopg2-binary.
SSL:     Configurable via config.ssl_mode (valid: 'require', 'prefer', 'disable').
         Default: 'prefer' (autodetect).
Timeouts:
  connect_timeout_s  → connect_timeout in connection string (seconds).
  query_timeout_s    → statement_timeout on the session (milliseconds).

Quoted identifiers:  use double-quotes "" for all PostgreSQL identifiers in queries.
  See quote_identifier() below.

Registers itself with the pool factory at import time via register_driver().
"""

from __future__ import annotations

import psycopg2

from backend.app.db_connectors.models import (
    ColumnMeta,
    DBConnectionError,
    DBConnectorConfig,
    SchemaDiscoveryResult,
    TableMeta,
)
from backend.connectors.db.connection_pool import register_driver

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# System catalogue query for schema discovery (spec: T2-S10-A table 14).
# Returns schema, table, and column names excluding system schemas.
CATALOGUE_QUERY: str = (
    "SELECT table_schema, table_name, column_name "
    "FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
)

# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Wrap a PostgreSQL identifier in double-quotes.

    Escapes embedded double-quotes by doubling them, per ANSI SQL convention.
    Always use this for schema and table names in queries.

    Example:
        quote_identifier("public")     -> '"public"'
        quote_identifier('odd"name')   -> '"odd""name"'
    """
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Connection string construction (never logged or surfaced in errors)
# ---------------------------------------------------------------------------

def _build_connection_string(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> str:
    """Build the psycopg2 connection string.

    SSL mode is applied from config.ssl_mode (valid: 'require', 'prefer', 'disable').
    connect_timeout_s is applied directly.
    The query timeout is set separately on the session after connect.

    This string must NEVER appear in log output or exception messages.
    """
    ssl_mode = config.ssl_mode or "prefer"
    return (
        f"host={config.host} "
        f"port={config.port} "
        f"database={config.database} "
        f"user={username} "
        f"password={password} "
        f"connect_timeout={config.connect_timeout_s} "
        f"sslmode={ssl_mode}"
    )


# ---------------------------------------------------------------------------
# Driver factory — registered with the pool factory below
# ---------------------------------------------------------------------------

def create_postgresql_connection(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> psycopg2.extensions.connection:
    """Create and return a psycopg2 connection to PostgreSQL.

    SSL mode is configurable.  connect_timeout_s bounds the TCP handshake.
    query_timeout_s is applied as statement_timeout (per-statement timeout).

    Raises DBConnectionError on any failure.  The error message includes only
    host and database — never the connection string, username, or password.
    """
    conn_str = _build_connection_string(config, username, password)
    try:
        conn = psycopg2.connect(conn_str)
    except psycopg2.OperationalError as exc:
        # Sanitised message: host + db only, no credentials or conn string.
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

    # Apply query-execution timeout (distinct from TCP connect timeout).
    # statement_timeout expects milliseconds in PostgreSQL.
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SET statement_timeout = {config.query_timeout_s * 1000};"
        )
        conn.commit()
    finally:
        cursor.close()

    return conn


# ---------------------------------------------------------------------------
# Schema discovery helper
# ---------------------------------------------------------------------------

def discover_schema_postgresql(conn: psycopg2.extensions.connection) -> SchemaDiscoveryResult:
    """Run the information_schema.columns query and return a SchemaDiscoveryResult.

    Uses information_schema.columns — no customer data accessed.
    estimated_row_counts is always None for PostgreSQL in Sprint 10
    (row count estimation requires checking table statistics which may not be
    accurate or available depending on autovacuum settings).
    """
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
        tables=[
            TableMeta(schema=s, table=t) for s, t in sorted(tables_seen)
        ],
        columns=columns,
        estimated_row_counts=None,
    )


# ---------------------------------------------------------------------------
# Self-registration with the pool factory
# ---------------------------------------------------------------------------

# Importing this module is sufficient to register the PostgreSQL driver.
# T2-S10-A's get_or_create_pool() will then be able to build PostgreSQL pools.
register_driver("postgresql", create_postgresql_connection)
