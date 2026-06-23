"""Compatibility exports for the T2 database connector model contract.

The canonical model definitions live in ``backend.app.db_connectors.models``.
This module keeps the ``connectors.db.models`` import path working for query
guard code and backend tests without creating a second set of model classes.
"""

from __future__ import annotations

try:
    from backend.app.db_connectors.models import (
        ColumnMeta,
        DBConnectionError,
        DBConnectorConfig,
        DBQueryRejectedError,
        DBQueryResult,
        DBScopeViolationError,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        TableMeta,
    )
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.db_connectors.models import (
        ColumnMeta,
        DBConnectionError,
        DBConnectorConfig,
        DBQueryRejectedError,
        DBQueryResult,
        DBScopeViolationError,
        SchemaDiscoveryResult,
        ScopeDeclaration,
        TableMeta,
    )

__all__ = [
    "ColumnMeta",
    "DBConnectionError",
    "DBConnectorConfig",
    "DBQueryRejectedError",
    "DBQueryResult",
    "DBScopeViolationError",
    "SchemaDiscoveryResult",
    "ScopeDeclaration",
    "TableMeta",
]
