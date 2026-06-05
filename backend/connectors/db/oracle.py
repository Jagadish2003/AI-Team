"""
Oracle DB connection driver — T2-S10-A Task T4.

Driver:  python-oracledb (modern replacement for cx_Oracle).
         Thin mode works without Oracle Instant Client (dev/CI).
         Thick mode requires Oracle Instant Client 21 in container (production).
         Call init_thick_mode() once at container startup to activate thick mode.

Connection formats (auto-detected from config.database):
  Direct / EZConnect:  config.database is a plain service name  →  host:port/service
  TNS descriptor:      config.database starts with '('          →  used verbatim as DSN

Timeouts:
  connect_timeout_s  → tcp_connect_timeout (seconds) — TCP handshake only.
  query_timeout_s    → conn.callTimeout (milliseconds) — per-statement limit.

Quoted identifiers:  use double-quotes "" for all Oracle identifiers in queries.
  See quote_identifier() below.

Trivial test query:  SELECT 1 FROM DUAL

Registers itself with the pool factory at import time via register_driver().
"""

from __future__ import annotations

try:
    import oracledb
except ModuleNotFoundError:  # Optional DB driver may be absent in mocked tests.
    oracledb = None

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

# Trivial connectivity test query — valid on all Oracle versions.
TRIVIAL_QUERY: str = "SELECT 1 FROM DUAL"

# System catalogue query for schema discovery (spec: T2-S10-A table 14).
# Uses ALL_COLUMNS — does not require DBA access.
CATALOGUE_QUERY: str = (
    "SELECT OWNER, TABLE_NAME, COLUMN_NAME "
    "FROM ALL_COLUMNS "
    "WHERE OWNER NOT IN ('SYS', 'SYSTEM', 'INFORMATION_SCHEMA')"
)

# ---------------------------------------------------------------------------
# Thick-mode initialisation (container startup — call once)
# ---------------------------------------------------------------------------

def init_thick_mode(lib_dir: str | None = None) -> None:
    """Activate Oracle thick mode via Oracle Instant Client 21.

    Call this once at container startup BEFORE any connection is made.
    In development/CI thin mode is used automatically when this is not called.

    lib_dir:  path to Oracle Instant Client libraries, or None to let
              oracledb locate them via LD_LIBRARY_PATH / PATH.
    """
    if oracledb is None:
        raise DBConnectionError(
            "Oracle DB driver package 'oracledb' is not installed.",
            error_code="driver_not_installed",
        )

    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    except oracledb.ProgrammingError:
        # Already initialised — harmless if called more than once.
        pass


# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Wrap an Oracle identifier in double-quotes.

    Escapes embedded double-quotes by doubling them, per ANSI SQL convention.
    Always use this for schema (OWNER) and table names in dynamic queries.

    Example:
        quote_identifier("HR")         -> '"HR"'
        quote_identifier('odd"name')   -> '"odd""name"'
    """
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# DSN / connection parameter construction
# ---------------------------------------------------------------------------

def _is_tns(database: str) -> bool:
    """Return True when database looks like a TNS descriptor."""
    return database.strip().startswith("(")


def _connect_kwargs(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> dict:
    """Build keyword arguments for oracledb.connect().

    Two supported formats:
      Direct (EZConnect): database is a service name → host + port + service_name
      TNS descriptor:     database starts with '('   → passed verbatim as dsn

    SSL/wallet settings can be added to the returned dict by the caller if needed.
    The returned dict MUST NOT be logged or surfaced in error messages
    because it contains username and password.
    """
    kwargs: dict = dict(
        user=username,
        password=password,
        tcp_connect_timeout=config.connect_timeout_s,
    )
    if _is_tns(config.database):
        # TNS format: the full descriptor is the DSN.
        kwargs["dsn"] = config.database.strip()
    else:
        # Direct format: separate host/port/service_name.
        kwargs["host"] = config.host
        kwargs["port"] = config.port
        kwargs["service_name"] = config.database
    return kwargs


# ---------------------------------------------------------------------------
# Driver factory — registered with the pool factory below
# ---------------------------------------------------------------------------

def create_oracle_connection(
    config: DBConnectorConfig,
    username: str,
    password: str,
) -> oracledb.Connection:
    """Create and return an oracledb connection to Oracle DB.

    Supports both TNS and direct (EZConnect) connection formats.
    connect_timeout_s bounds the TCP handshake.
    query_timeout_s is applied as conn.callTimeout (in milliseconds).

    Raises DBConnectionError on any failure.  Error messages include only
    host and service name — never the DSN, username, or password.
    """
    if oracledb is None:
        raise DBConnectionError(
            "Oracle DB driver package 'oracledb' is not installed.",
            error_code="driver_not_installed",
        )

    kwargs = _connect_kwargs(config, username, password)
    try:
        conn = oracledb.connect(**kwargs)
    except oracledb.DatabaseError as exc:
        raise DBConnectionError(
            f"Oracle DB connection failed [{type(exc).__name__}] "
            f"host={config.host!r} service={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except oracledb.Error as exc:
        raise DBConnectionError(
            f"Oracle DB driver error [{type(exc).__name__}] "
            f"host={config.host!r} service={config.database!r}",
            error_code="connection_failed",
        ) from exc
    except Exception as exc:
        raise DBConnectionError(
            f"Unexpected error connecting to Oracle DB "
            f"host={config.host!r} service={config.database!r}: {type(exc).__name__}",
            error_code="connection_failed",
        ) from exc

    # query_timeout_s → callTimeout (oracledb uses milliseconds).
    conn.callTimeout = config.query_timeout_s * 1000
    return conn


# ---------------------------------------------------------------------------
# Schema discovery helper
# ---------------------------------------------------------------------------

def discover_schema_oracle(conn: oracledb.Connection) -> SchemaDiscoveryResult:
    """Run the ALL_COLUMNS catalogue query and return a SchemaDiscoveryResult.

    Uses ALL_COLUMNS — no DBA access required.
    estimated_row_counts is always None for Oracle in Sprint 10
    (row count estimation requires DBA-level access not granted to SELECT users).
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

    for owner, table_name, column_name in rows:
        schemas.add(owner)
        tables_seen.add((owner, table_name))
        columns.append(
            ColumnMeta(schema=owner, table=table_name, column=column_name)
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

register_driver("oracle_db", create_oracle_connection)
