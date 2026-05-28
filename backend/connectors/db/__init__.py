# backend/connectors/db/__init__.py
"""
Database connector framework — AgentIQ T2-S10-A.

Public surface area (locked after T2-S10-A merges):
    DBConnectorConfig, ScopeDeclaration, DBQueryResult, SchemaDiscoveryResult
    DBScopeViolationError, DBQueryRejectedError, DBConnectionError
    validate_read_only, validate_scope, execute_query
"""

from .models import (
    DBConnectorConfig,
    ScopeDeclaration,
    DBQueryResult,
    SchemaDiscoveryResult,
    TableMeta,
    ColumnMeta,
    DBScopeViolationError,
    DBQueryRejectedError,
    DBConnectionError,
)
from .query_guard import validate_read_only, validate_scope
from .execute_query import execute_query

__all__ = [
    # Data models
    "DBConnectorConfig",
    "ScopeDeclaration",
    "DBQueryResult",
    "SchemaDiscoveryResult",
    "TableMeta",
    "ColumnMeta",
    # Exceptions
    "DBScopeViolationError",
    "DBQueryRejectedError",
    "DBConnectionError",
    # Query guard
    "validate_read_only",
    "validate_scope",
    # Central data access path
    "execute_query",
]
