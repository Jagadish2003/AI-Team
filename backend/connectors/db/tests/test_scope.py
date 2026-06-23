"""Unit tests for T2-S10-A scope management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from backend.app.db_connectors.models import (
    ColumnMeta,
    SchemaDiscoveryResult,
    ScopeDeclaration,
    TableMeta,
)
from backend.connectors.db import scope as scope_module


def _schema(
    schemas: list[str] | None = None,
    tables: list[tuple[str, str]] | None = None,
) -> SchemaDiscoveryResult:
    table_pairs = tables or [("dbo", "orders"), ("dbo", "accounts")]
    return SchemaDiscoveryResult(
        schemas=schemas or sorted({schema for schema, _table in table_pairs}),
        tables=[
            TableMeta(schema=schema, table=table)
            for schema, table in table_pairs
        ],
        columns=[
            ColumnMeta(schema=schema, table=table, column="id")
            for schema, table in table_pairs
        ],
        estimated_row_counts=None,
    )


def _scope(
    schemas: list[str] | None = None,
    tables: list[str] | None = None,
    org_id: str = "org_a",
    connector_id: str = "sqlserver",
) -> ScopeDeclaration:
    return ScopeDeclaration(
        org_id=org_id,
        connector_id=connector_id,
        schemas=schemas or ["dbo"],
        tables=tables if tables is not None else ["orders"],
        declared_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        declared_by="analyst@example.com",
    )


@pytest.fixture()
def kv_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[str, Any] = {}
    audit_calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(scope_module._db, "kv_get", lambda key: store.get(key))
    monkeypatch.setattr(scope_module._db, "kv_set", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(
        scope_module,
        "log_event",
        lambda event_type, **fields: audit_calls.append((event_type, fields)),
    )
    store["_audit_calls"] = audit_calls
    return store


class TestSaveScope:
    def test_save_scope_persists_known_table_and_writes_audit(
        self, kv_store: dict[str, Any]
    ) -> None:
        saved = scope_module.save_scope(_scope(tables=["orders"]), _schema())

        loaded = scope_module.get_scope("org_a", "sqlserver")
        assert saved.tables == ["orders"]
        assert loaded.tables == ["orders"]
        assert loaded.schemas == ["dbo"]
        assert kv_store["_audit_calls"] == [
            (
                "scope_declared",
                {
                    "org_id": "org_a",
                    "connector_id": "sqlserver",
                    "schemas": ["dbo"],
                    "tables": ["orders"],
                },
            )
        ]

    def test_save_scope_accepts_schema_qualified_declared_table(
        self, kv_store: dict[str, Any]
    ) -> None:
        scope_module.save_scope(_scope(tables=["dbo.orders"]), _schema())

        loaded = scope_module.get_scope("org_a", "sqlserver")
        assert loaded.tables == ["dbo.orders"]

    def test_save_scope_rejects_unknown_table_and_does_not_save(
        self, kv_store: dict[str, Any]
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            scope_module.save_scope(_scope(tables=["payments"]), _schema())

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "unknown_table"
        assert exc_info.value.detail["unknown_tables"] == ["payments"]
        assert scope_module._scope_key("org_a", "sqlserver") not in kv_store
        assert kv_store["_audit_calls"] == []

    def test_save_scope_rejects_table_outside_declared_schema(
        self, kv_store: dict[str, Any]
    ) -> None:
        discovered = _schema(
            schemas=["dbo", "finance"],
            tables=[("dbo", "orders"), ("finance", "ledger")],
        )

        with pytest.raises(HTTPException) as exc_info:
            scope_module.save_scope(
                _scope(schemas=["dbo"], tables=["finance.ledger"]),
                discovered,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "unknown_table"
        assert scope_module._scope_key("org_a", "sqlserver") not in kv_store

    def test_save_scope_requires_previous_schema_discovery(
        self, kv_store: dict[str, Any]
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            scope_module.save_scope(_scope(tables=["orders"]))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "schema_not_discovered"


class TestEmptyTablesList:
    def test_empty_tables_list_is_saved_for_known_schema(
        self, kv_store: dict[str, Any]
    ) -> None:
        saved = scope_module.save_scope(_scope(tables=[]), _schema())

        assert saved.tables == []
        assert scope_module.get_scope("org_a", "sqlserver").tables == []

    def test_empty_tables_list_still_rejects_unknown_schema(
        self, kv_store: dict[str, Any]
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            scope_module.save_scope(
                _scope(schemas=["staging"], tables=[]),
                _schema(schemas=["dbo"], tables=[("dbo", "orders")]),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "unknown_schema"
        assert scope_module._scope_key("org_a", "sqlserver") not in kv_store


class TestGetScope:
    def test_get_scope_returns_current_org_connector_scope(
        self, kv_store: dict[str, Any]
    ) -> None:
        scope_module.save_scope(_scope(tables=["accounts"]), _schema())

        loaded = scope_module.get_scope("org_a", "sqlserver")
        assert loaded.org_id == "org_a"
        assert loaded.connector_id == "sqlserver"
        assert loaded.tables == ["accounts"]

    def test_get_scope_isolated_by_org_and_connector(
        self, kv_store: dict[str, Any]
    ) -> None:
        scope_module.save_scope(
            _scope(org_id="org_a", connector_id="sqlserver", tables=["orders"]),
            _schema(),
        )
        scope_module.save_scope(
            _scope(org_id="org_b", connector_id="sqlserver", tables=["accounts"]),
            _schema(),
        )

        assert scope_module.get_scope("org_a", "sqlserver").tables == ["orders"]
        assert scope_module.get_scope("org_b", "sqlserver").tables == ["accounts"]

    def test_get_scope_missing_raises_404(self, kv_store: dict[str, Any]) -> None:
        with pytest.raises(HTTPException) as exc_info:
            scope_module.get_scope("missing_org", "sqlserver")

        assert exc_info.value.status_code == 404


class TestDiscoveredSchemaCache:
    def test_save_scope_uses_previously_discovered_schema(
        self, kv_store: dict[str, Any]
    ) -> None:
        scope_module.save_discovered_schema("org_a", "sqlserver", _schema())

        scope_module.save_scope(_scope(tables=["orders"]))

        assert scope_module.get_scope("org_a", "sqlserver").tables == ["orders"]

    def test_get_discovered_schema_round_trips_schema_result(
        self, kv_store: dict[str, Any]
    ) -> None:
        expected = _schema(
            schemas=["dbo"],
            tables=[("dbo", "orders"), ("dbo", "accounts")],
        )
        scope_module.save_discovered_schema("org_a", "sqlserver", expected)

        result = scope_module.get_discovered_schema("org_a", "sqlserver")

        assert result is not None
        assert result.schemas == ["dbo"]
        assert [(t.schema, t.table) for t in result.tables] == [
            ("dbo", "orders"),
            ("dbo", "accounts"),
        ]
