"""
SQL Server connection driver — T2-S10-A Task T3.

Driver:  pyodbc + ODBC Driver 18 for SQL Server.
SSL:     Encrypt=yes; TrustServerCertificate=no  (enforced, not configurable).
Timeouts:
  connect_timeout_s → ConnectTimeout in ODBC connection string (TCP attempt).
  query_timeout_s   → conn.timeout attribute applied after connect (statement).

Quoted identifiers: use square brackets [] for all SQL Server identifiers in
queries (e.g. [schema].[table]).  See quote_identifier() below.

This module registers itself with the pool factory on import so that
get_or_create_pool() can build SQL Server pools without importing this module
explicitly from every call site — importing the driver module once is enough.
"""

from __future__ import annotations

import pyodbc

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

ODBC_DRIVER: str = "ODBC Driver 18 for SQL Server"

# System catalogue query for schema discovery (spec: T2-S10-A table 14).
# Returns schema, table, and column names excluding system schemas.
CATALOGUE_QUERY: str = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME "
    "FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA NOT IN ('sys', 'information_schema')"
)

# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Wrap a SQL Server identifier in square brackets.

    Escapes embedded ] by doubling it, per SQL Server convention.
    Always use this for schema and table names in queries.

    Example:
        quote_identifier("my schema") -> "[my schema]"
        quote_identifier("odd]name")  -> "[odd]]name]"
    """
    return "[" + name.replace("]", "]]") + "]"


# ---------------------------------------------------------------------------
# Connection string construction (never logged or surfaced in errors)
# ---------------------------------------------------------------------------

def _build_connection_string(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> str:
    """Build the pyodbc connection string.

    SSL is always enforced (Encrypt=yes; TrustServerCertificate=no).
    ConnectTimeout maps to config.connect_timeout_s (TCP connection attempt).
    The query timeout is set separately on the connection object after connect.

    Windows auth vs SQL auth:
        SQL auth:     username is non-empty → UID/PWD included.
        Windows auth: username is empty string → Trusted_Connection=yes,
                      no UID/PWD in connection string.

    This string must NEVER appear in log output or exception messages.
    """
    base = (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={config.host},{config.port};"
        f"DATABASE={config.database};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"ConnectTimeout={config.connect_timeout_s};"
    )
    if username:
        return base + f"UID={username};PWD={password};"
    # Windows auth — Trusted_Connection handles identity via Kerberos/NTLM
    return base + "Trusted_Connection=yes;"


# ---------------------------------------------------------------------------
# Driver factory — registered with the pool factory below
# ---------------------------------------------------------------------------

def create_sqlserver_connection(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> pyodbc.Connection:
    """Create and return a pyodbc connection to SQL Server.

    SSL is enforced.  connect_timeout_s bounds the TCP handshake.
    query_timeout_s is applied as conn.timeout (per-statement timeout).

    Raises DBConnectionError on any failure.  The error message includes only
    host and database — never the connection string, username, or password.
    """
    conn_str = _build_connection_string(config, username, password)
    try:
        conn = pyodbc.connect(conn_str)
    except pyodbc.Error as exc:
        # Sanitised message: host + db only, no credentials or conn string.
        raise DBConnectionError(
            f"SQL Server connection failed [{type(exc).__name__}] "
            f"host={config.host!r} db={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except Exception as exc:
        raise DBConnectionError(
            f"Unexpected error connecting to SQL Server "
            f"host={config.host!r} db={config.database!r}: {type(exc).__name__}",
            error_code="connection_failed",
        ) from exc

    # Apply query-execution timeout (distinct from TCP connect timeout).
    conn.timeout = config.query_timeout_s
    return conn


# ---------------------------------------------------------------------------
# Schema discovery helper
# ---------------------------------------------------------------------------

def discover_schema_sqlserver(conn: pyodbc.Connection) -> SchemaDiscoveryResult:
    """Run the system catalogue query and return a SchemaDiscoveryResult.

    Uses INFORMATION_SCHEMA.COLUMNS — no customer data accessed.
    estimated_row_counts is always None for SQL Server in Sprint 10
    (requires DBA-level access not available to the SELECT-only user).
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

# Importing this module is sufficient to register the SQL Server driver.
# T2-S10-A's get_or_create_pool() will then be able to build SQL Server pools.
register_driver("sqlserver", create_sqlserver_connection)
