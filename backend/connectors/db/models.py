"""
backend/connectors/db/models.py

Data models and exception classes for the AgentIQ database connectivity
framework (T2-S10-A).

INTERFACE LOCK: This file is locked after T2-S10-A merges. Adding new
optional fields is allowed; removing or renaming existing fields requires
updating all in-progress downstream stories simultaneously.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DBConnectorConfig:
    """
    Configuration for a Track 2 database connector.

    username_key and password_key store environment-variable NAMES, never
    the actual credential values. Actual credentials are resolved at pool
    creation via resolve_secret() and discarded immediately afterwards.
    """

    connector_id: str        # 'sqlserver' | 'oracle_db' | 'postgresql'
    org_id: str
    host: str
    port: int
    database: str
    driver: str
    username_key: str        # env var name — never the credential value
    password_key: str        # env var name — never the credential value
    ssl_mode: Optional[str] = None
    connect_timeout_s: int = 10   # Applied to TCP connection attempt
    query_timeout_s: int = 30     # Applied to query execution


# ---------------------------------------------------------------------------
# Scope declaration
# ---------------------------------------------------------------------------


@dataclass
class ScopeDeclaration:
    """
    Declares which schemas and tables AgentIQ may query for a given
    org + connector pair.

    tables == [] means any table in the declared schemas is permitted.
    Scope enforcement is NOT bypassed — schema membership is still verified.
    """

    org_id: str
    connector_id: str
    schemas: list[str]
    tables: list[str]        # [] = any table in declared schemas (scope still enforced)
    declared_at: datetime
    declared_by: str


# ---------------------------------------------------------------------------
# Query result
# ---------------------------------------------------------------------------


@dataclass
class DBQueryResult:
    """Result of a successful execute_query() call."""

    columns: list[str]
    rows: list[tuple]
    row_count: int
    query_hash: str          # SHA-256 of the query string
    duration_ms: int
    truncated: bool          # True when MAX_ROWS_PER_QUERY was hit


# ---------------------------------------------------------------------------
# Schema discovery result
# ---------------------------------------------------------------------------


@dataclass
class TableMeta:
    """Metadata for a single table returned by discover_schema()."""

    schema: str
    table: str


@dataclass
class ColumnMeta:
    """Metadata for a single column returned by discover_schema()."""

    schema: str
    table: str
    column: str


@dataclass
class SchemaDiscoveryResult:
    """Result of a discover_schema() call (system catalogue only)."""

    schemas: list[str]
    tables: list[TableMeta]
    columns: list[ColumnMeta]
    estimated_row_counts: Optional[dict[str, int]] = None  # null when unavailable


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class DBScopeViolationError(Exception):
    """
    Raised when execute_query() references a table outside the declared scope,
    or when the fail-closed rule applies (table extraction is ambiguous or
    returns an empty set for a non-trivial query).
    """


class DBQueryRejectedError(Exception):
    """
    Raised when the query guard detects a non-SELECT statement.
    The query is never executed. Contains an error_code field.
    """

    def __init__(self, message: str, error_code: str = "query_rejected") -> None:
        super().__init__(message)
        self.error_code = error_code


class DBConnectionError(Exception):
    """
    Raised when the connection pool cannot connect, or when a query exceeds
    QUERY_TIMEOUT_S. error_code is always present; raw exception details that
    might expose credentials are never propagated.
    """

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code
