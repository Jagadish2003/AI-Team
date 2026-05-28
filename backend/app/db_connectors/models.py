from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


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
    connect_timeout_s: int = 10
    query_timeout_s: int = 30


@dataclass
class ScopeDeclaration:
    org_id: str
    connector_id: str
    schemas: list[str]
    tables: list[str]
    declared_at: datetime
    declared_by: str


@dataclass
class DBQueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    query_hash: str
    duration_ms: int
    truncated: bool


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
    estimated_row_counts: Optional[dict[str, int]] = None


class DBScopeViolationError(Exception):
    """Raised on scope violation or fail-closed table extraction failure."""


class DBQueryRejectedError(Exception):
    def __init__(self, message: str, error_code: str = "query_rejected") -> None:
        super().__init__(message)
        self.error_code = error_code


class DBConnectionError(Exception):
    """Raised on connection or timeout failure. Never exposes credentials."""

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code
