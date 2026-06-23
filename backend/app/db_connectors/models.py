from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class DBConnectorConfig:
    connector_id: str          # 'sqlserver' | 'oracle_db' | 'postgresql'
    org_id: str
    host: str
    port: int
    database: str
    driver: str
    username_key: str          # env var name — never the actual credential value
    password_key: str          # env var name — never the actual credential value
    ssl_mode: Optional[str] = None
    connect_timeout_s: int = 10   # TCP connection attempt timeout
    query_timeout_s: int = 30     # query execution timeout


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@dataclass
class ScopeDeclaration:
    org_id: str
    connector_id: str
    schemas: list[str]
    tables: list[str]          # [] means any table in declared schemas; scope enforcement still applies
    declared_at: datetime
    declared_by: str


# ---------------------------------------------------------------------------
# Query result
# ---------------------------------------------------------------------------

@dataclass
class DBQueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    query_hash: str            # SHA-256 of query string
    duration_ms: int
    truncated: bool


# ---------------------------------------------------------------------------
# Schema discovery result
# ---------------------------------------------------------------------------

@dataclass
class TableMeta:
    schema: str
    table: str


@dataclass
class ColumnMeta:
    schema: str
    table: str
    column: str


@dataclass
class SchemaDiscoveryResult:
    schemas: list[str]
    tables: list[TableMeta]
    columns: list[ColumnMeta]
    estimated_row_counts: Optional[dict[str, int]] = None  # null when unavailable


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class DBScopeViolationError(Exception):
    """Raised when a query references tables outside declared scope,
    or when table extraction is ambiguous (fail-closed rule)."""


class DBQueryRejectedError(Exception):
    """Raised when a non-SELECT statement is detected before any network call."""

    def __init__(self, message: str, error_code: str = "query_rejected") -> None:
        super().__init__(message)
        self.error_code = error_code


class DBConnectionError(Exception):
    """Raised when the pool cannot connect or a query times out.
    Never exposes credential values in message or error_code."""

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# Keep the top-level ``app`` and repo-root ``backend.app`` import paths from
# creating duplicate dataclass/exception identities during local test runs.
if __name__ == "backend.app.db_connectors.models":
    sys.modules.setdefault("app.db_connectors.models", sys.modules[__name__])
elif __name__ == "app.db_connectors.models":
    sys.modules.setdefault("backend.app.db_connectors.models", sys.modules[__name__])
