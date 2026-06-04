"""Contract tests for T2-S12-A Task T2 - PostgreSQL operational ingestor."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import psycopg2

from backend.app.db_connectors.models import (
    DBConnectionError,
    DBConnectorConfig,
    ScopeDeclaration,
)
from backend.connectors.db import postgresql


def _make_config() -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id="postgresql",
        org_id="test-org",
        host="postgresql.test.local",
        port=5432,
        database="servicedb",
        driver="psycopg2",
        username_key="POSTGRESQL_USERNAME",
        password_key="POSTGRESQL_PASSWORD",
    )


def _make_scope(tables: list[str] | None = None) -> ScopeDeclaration:
    return ScopeDeclaration(
        org_id="test-org",
        connector_id="postgresql",
        schemas=["public"],
        tables=tables if tables is not None else ["public.ServiceTickets"],
        declared_at=datetime.now(timezone.utc),
        declared_by="test",
    )


def _make_result(columns: list[str], rows: list[tuple]) -> Any:
    return SimpleNamespace(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        query_hash="hash",
        duration_ms=5,
        truncated=False,
    )


VOLUME_RESULT = _make_result(
    ["ticket_date", "ticket_count"],
    [("2026-05-30", 20), ("2026-05-29", 10), ("2026-05-28", None)],
)
SLA_RESULT = _make_result(
    ["total_tickets", "breached_count", "breach_rate_pct"],
    [(100, 16, 16.0)],
)
QUEUE_RESULT = _make_result(
    ["priority", "queue_count", "avg_age_hours"],
    [("P1", 12, 48.0), ("P2", 9, None), (None, None, None)],
)


def test_postgresql_driver_is_psycopg2_binary_path():
    assert psycopg2 is postgresql.psycopg2
    assert postgresql.CONNECTOR_ID == "postgresql"


def test_signal_queries_use_postgresql_double_quotes_and_date_arithmetic():
    queries = [
        postgresql.TICKET_VOLUME_QUERY.format(
            date_col="created_date", schema="public", table="ServiceTickets"
        ),
        postgresql.SLA_BREACH_QUERY.format(
            sla_col="sla_breached", date_col="created_date",
            schema="public", table="ServiceTickets",
        ),
        postgresql.QUEUE_DEPTH_QUERY.format(
            priority_col="priority", status_col="status", date_col="created_date",
            schema="public", table="ServiceTickets",
        ),
    ]

    for query in queries:
        assert '"public"."ServiceTickets"' in query
        assert "[" not in query and "]" not in query

    assert "NOW() - INTERVAL '90 days'" in queries[0]
    assert "NOW() - INTERVAL '30 days'" in queries[1]
    assert "EXTRACT(EPOCH FROM AVG(NOW() -" in queries[2]


def test_sla_query_uses_native_boolean_and_integer_fallback_query_uses_one():
    native = postgresql.SLA_BREACH_QUERY.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="ServiceTickets",
    )
    fallback = postgresql.SLA_BREACH_INTEGER_FALLBACK_QUERY.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="ServiceTickets",
    )

    assert '"sla_breached" = TRUE' in native
    assert '"sla_breached" = 1' in fallback


def test_ingest_uses_execute_query_for_all_three_queries_and_no_direct_connect():
    calls: list[str] = []
    results = [VOLUME_RESULT, SLA_RESULT, QUEUE_RESULT]

    def fake_execute(*args, **kwargs):
        calls.append(kwargs["query"])
        return results[len(calls) - 1]

    with patch("backend.connectors.db.postgresql._execute_query", side_effect=fake_execute), \
         patch("backend.connectors.db.postgresql._record_event"), \
         patch("backend.connectors.db.postgresql.psycopg2.connect") as connect:
        out = postgresql.ingest(
            "test-org", "run-1", _make_config(), scope=_make_scope()
        )

    assert len(calls) == 3
    connect.assert_not_called()
    assert out["connector_id"] == "postgresql"
    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


def test_native_boolean_type_error_retries_integer_fallback(caplog):
    calls: list[str] = []
    type_error = DBConnectionError(
        "operator does not exist: integer = boolean",
        error_code="query_failed",
    )

    def fake_execute(*args, **kwargs):
        calls.append(kwargs["query"])
        if len(calls) == 1:
            return VOLUME_RESULT
        if len(calls) == 2:
            raise type_error
        if len(calls) == 3:
            return SLA_RESULT
        return QUEUE_RESULT

    with patch("backend.connectors.db.postgresql._execute_query", side_effect=fake_execute), \
         patch("backend.connectors.db.postgresql._record_event"):
        out = postgresql.ingest(
            "test-org", "run-1", _make_config(), scope=_make_scope()
        )

    assert len(calls) == 4
    assert '"sla_breached" = TRUE' in calls[1]
    assert '"sla_breached" = 1' in calls[2]
    assert "retrying integer fallback" in caplog.text
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["sla_breach"]["breach_rate_pct"] == 16.0


def test_missing_columns_mark_only_affected_signals_degraded():
    bad_result = _make_result(["wrong_a", "wrong_b"], [(1, 2)])

    with patch("backend.connectors.db.postgresql._execute_query", return_value=bad_result), \
         patch("backend.connectors.db.postgresql._record_event"):
        out = postgresql.ingest(
            "test-org", "run-1", _make_config(), scope=_make_scope()
        )

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is True
    assert out["queue_depth"]["degraded_signal"] is True


def test_timeout_marks_signal_degraded_and_run_continues():
    calls = 0
    timeout = DBConnectionError("statement timeout", error_code="query_timeout")

    def fake_execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise timeout
        if calls == 2:
            return SLA_RESULT
        return QUEUE_RESULT

    with patch("backend.connectors.db.postgresql._execute_query", side_effect=fake_execute), \
         patch("backend.connectors.db.postgresql._record_event"):
        out = postgresql.ingest(
            "test-org", "run-1", _make_config(), scope=_make_scope()
        )

    assert calls == 3
    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


def test_return_shape_matches_sqlserver_ingestor_shape_with_postgresql_id():
    with patch(
        "backend.connectors.db.postgresql._execute_query",
        side_effect=[VOLUME_RESULT, SLA_RESULT, QUEUE_RESULT],
    ), patch("backend.connectors.db.postgresql._record_event"):
        out = postgresql.ingest(
            "test-org", "run-99", _make_config(), scope=_make_scope()
        )

    for key in (
        "ticket_volume", "sla_breach", "queue_depth", "connector_id",
        "org_id", "run_id", "schema_name", "table_name",
    ):
        assert key in out

    assert out["connector_id"] == "postgresql"
    assert out["org_id"] == "test-org"
    assert out["run_id"] == "run-99"
    assert out["schema_name"] == "public"
    assert out["table_name"] == "ServiceTickets"


def test_scope_tables_empty_still_uses_schema_qualified_query():
    captured: list[str] = []

    def fake_execute(*args, **kwargs):
        captured.append(kwargs["query"])
        return [VOLUME_RESULT, SLA_RESULT, QUEUE_RESULT][len(captured) - 1]

    with patch("backend.connectors.db.postgresql._execute_query", side_effect=fake_execute), \
         patch("backend.connectors.db.postgresql._record_event"):
        postgresql.ingest(
            "test-org", "run-1", _make_config(), scope=_make_scope(tables=[])
        )

    assert captured
    assert all('"public"."ServiceTickets"' in query for query in captured)
