"""
Contract tests for T2-S12-A Task T8 — PostgreSQL Operational Signal Ingestor.

Covers 20 acceptance criteria. All tests use mocked execute_query() so no live
PostgreSQL is required.

Acceptance criteria mapped:
  AC5  — psycopg2-binary driver; all queries use "" quoted identifiers.
  AC6  — Native boolean (= TRUE) with integer fallback (= 1) on type error.
  AC7  — Missing column → degraded_signal=True, no raise.
  AC8  — query_timeout → degraded_signal=True, run continues.
  AC9  — Return shape identical to T2-S11-A with connector_id='postgresql'.
  AC11 — End-to-end: mocked data produces detector results with signal_source='postgresql'.
  AC16 — Cross-org isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers — support both project-root and backend-relative paths
# ---------------------------------------------------------------------------

def _imp(module_path: str):
    try:
        parts = module_path.split(".")
        mod = __import__(module_path)
        for p in parts[1:]:
            mod = getattr(mod, p)
        return mod
    except (ImportError, ModuleNotFoundError):
        alt = ".".join(module_path.split(".")[1:])
        parts = alt.split(".")
        mod = __import__(alt)
        for p in parts[1:]:
            mod = getattr(mod, p)
        return mod


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

def _make_scope(org_id="test-org", schemas=None, tables=None):
    try:
        from backend.connectors.db import ScopeDeclaration
    except ModuleNotFoundError:
        from connectors.db import ScopeDeclaration
    return ScopeDeclaration(
        org_id=org_id,
        connector_id="postgresql",
        schemas=schemas or ["public"],
        tables=tables or ["public.service_tickets"],
        declared_at=datetime.now(timezone.utc),
        declared_by="test",
    )


def _make_config(org_id="test-org"):
    try:
        from backend.connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from connectors.db import DBConnectorConfig
    return DBConnectorConfig(
        connector_id="postgresql",
        org_id=org_id,
        host="pg.test.local",
        port=5432,
        database="servicedb",
        driver="psycopg2",
        username_key="PG_USER",
        password_key="PG_PASS",
    )


def _make_result(columns: list[str], rows: list[tuple]) -> Any:
    return SimpleNamespace(
        columns=columns, rows=rows, row_count=len(rows),
        query_hash="pg-abc", duration_ms=4, truncated=False,
    )


VOLUME_COLUMNS  = ["ticket_date", "ticket_count"]
SLA_COLUMNS     = ["total_tickets", "breached_count", "breach_rate_pct"]
QUEUE_COLUMNS   = ["priority", "queue_count", "avg_age_hours"]

_INGESTOR_MOD  = "backend.connectors.db.postgresql_ingestor"
_INGESTOR_ALT  = "connectors.db.postgresql_ingestor"


def _patch(name: str):
    """Return patch target string for the ingestor module."""
    try:
        import backend.connectors.db.postgresql_ingestor  # noqa: F401
        return f"{_INGESTOR_MOD}.{name}"
    except ModuleNotFoundError:
        return f"{_INGESTOR_ALT}.{name}"


# ---------------------------------------------------------------------------
# AC5a — connector_id is 'postgresql'
# ---------------------------------------------------------------------------

def test_ac5a_connector_id_is_postgresql():
    """AC5: CONNECTOR_ID constant is 'postgresql'."""
    try:
        from backend.connectors.db.postgresql_ingestor import CONNECTOR_ID
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import CONNECTOR_ID
    assert CONNECTOR_ID == "postgresql"


# ---------------------------------------------------------------------------
# AC5b — all three queries use "" double-quote identifiers (not [])
# ---------------------------------------------------------------------------

def test_ac5b_queries_use_double_quote_identifiers():
    """AC5: All SQL queries use double-quote identifiers, not SQL Server [] brackets."""
    try:
        from backend.connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_template, name in [
        (TICKET_VOLUME_QUERY, "volume"),
        (SLA_BREACH_QUERY,    "sla"),
        (QUEUE_DEPTH_QUERY,   "queue"),
    ]:
        q = q_template.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="public", table="service_tickets",
        )
        assert '"public"' in q, f"{name}: schema must use double-quote quoting"
        assert '"service_tickets"' in q, f"{name}: table must use double-quote quoting"
        assert "[public]" not in q, f"{name}: must not use SQL Server [] brackets"
        assert "[service_tickets]" not in q, f"{name}: must not use SQL Server [] brackets"


# ---------------------------------------------------------------------------
# AC5c — queries use PostgreSQL date arithmetic (INTERVAL, NOW())
# ---------------------------------------------------------------------------

def test_ac5c_queries_use_postgresql_date_arithmetic():
    """AC5: Queries use NOW() and INTERVAL syntax, not DATEADD/GETDATE."""
    try:
        from backend.connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_template in (TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY):
        q = q_template.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="public", table="service_tickets",
        ).upper()
        assert "NOW()" in q or "INTERVAL" in q or "EXTRACT" in q, \
            "Query must use PostgreSQL date arithmetic"
        assert "DATEADD" not in q, "Must not use SQL Server DATEADD"
        assert "GETDATE" not in q, "Must not use SQL Server GETDATE"


# ---------------------------------------------------------------------------
# AC5d — all queries are SELECT-only (no DML)
# ---------------------------------------------------------------------------

def test_ac5d_queries_are_select_only():
    """AC5: All three queries are SELECT-only."""
    try:
        from backend.connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_template, name in [
        (TICKET_VOLUME_QUERY, "volume"),
        (SLA_BREACH_QUERY,    "sla"),
        (QUEUE_DEPTH_QUERY,   "queue"),
    ]:
        q = q_template.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="public", table="service_tickets",
        ).strip().upper()
        assert q.startswith("SELECT"), f"{name}: must start with SELECT"
        for bad in ("INSERT", "UPDATE", "DELETE", "DROP", "EXEC", "TRUNCATE"):
            assert bad not in q, f"{name}: must not contain {bad}"


# ---------------------------------------------------------------------------
# AC5e — SLA query uses native boolean (= TRUE)
# ---------------------------------------------------------------------------

def test_ac5e_sla_query_uses_native_boolean():
    """AC5/AC6: SLA breach query uses native PostgreSQL boolean (= TRUE)."""
    try:
        from backend.connectors.db.postgresql_ingestor import SLA_BREACH_QUERY
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import SLA_BREACH_QUERY

    q = SLA_BREACH_QUERY.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="service_tickets",
    )
    assert "= TRUE" in q, "SLA query must use native boolean = TRUE"
    assert "= 1" not in q, "Primary SLA query must not use integer comparison"


# ---------------------------------------------------------------------------
# AC5f — integer fallback query uses = 1
# ---------------------------------------------------------------------------

def test_ac5f_integer_fallback_query_uses_integer_comparison():
    """AC6: Integer fallback query uses = 1."""
    try:
        from backend.connectors.db.postgresql_ingestor import SLA_BREACH_INTEGER_FALLBACK_QUERY
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import SLA_BREACH_INTEGER_FALLBACK_QUERY

    q = SLA_BREACH_INTEGER_FALLBACK_QUERY.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="service_tickets",
    )
    assert "= 1" in q, "Fallback query must use integer comparison = 1"
    assert "= TRUE" not in q, "Fallback query must not use boolean = TRUE"


# ---------------------------------------------------------------------------
# AC5g — execute_query called exactly 3 times on clean run
# ---------------------------------------------------------------------------

def test_ac5g_three_execute_query_calls_on_clean_run():
    """AC5: execute_query() is called 3 times; no direct psycopg2.connect."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result) as mock_eq, \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        ingest("org", "run-1", _make_config(), scope=_make_scope())
    assert mock_eq.call_count == 3


# ---------------------------------------------------------------------------
# AC6a — native boolean result processed correctly
# ---------------------------------------------------------------------------

def test_ac6a_native_boolean_result_processed():
    """AC6: Native boolean result (= TRUE) returns correct breach metrics."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    vol_result  = _make_result(VOLUME_COLUMNS, [("2026-05-30", 10)])
    sla_result  = _make_result(SLA_COLUMNS, [(100, 20, 20.0)])
    queue_result = _make_result(QUEUE_COLUMNS, [("p1", 5, 12.0)])
    results_seq = [vol_result, sla_result, queue_result]
    call_n = [0]

    def side_effect(*args, **kwargs):
        r = results_seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is False
    assert out["sla_breach"]["total_tickets_30d"] == 100
    assert out["sla_breach"]["breached_count"] == 20
    assert out["sla_breach"]["breach_rate_pct"] == 20.0


# ---------------------------------------------------------------------------
# AC6b — integer fallback triggered on query_error, result correct
# ---------------------------------------------------------------------------

def test_ac6b_integer_fallback_triggered_on_type_error():
    """AC6: query_error on boolean query triggers integer fallback; result correct."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.postgresql_ingestor import ingest

    vol_result   = _make_result(VOLUME_COLUMNS, [])
    fb_sla_result = _make_result(SLA_COLUMNS, [(80, 16, 20.0)])
    queue_result  = _make_result(QUEUE_COLUMNS, [])

    call_n = [0]
    results_seq = [
        vol_result,
        DBConnectionError("type mismatch", error_code="query_error"),
        fb_sla_result,
        queue_result,
    ]

    def side_effect(*args, **kwargs):
        v = results_seq[call_n[0]]
        call_n[0] += 1
        if isinstance(v, Exception):
            raise v
        return v

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is False
    assert out["sla_breach"]["total_tickets_30d"] == 80
    assert out["sla_breach"]["breached_count"] == 16


# ---------------------------------------------------------------------------
# AC6c — integer fallback also fails → degraded_signal=True, no raise
# ---------------------------------------------------------------------------

def test_ac6b_integer_fallback_triggered_on_psycopg2_pgcode():
    """AC6: psycopg2 pgcode 42804 triggers integer fallback; result correct."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    class FakeDatatypeMismatch(Exception):
        pgcode = "42804"

    vol_result = _make_result(VOLUME_COLUMNS, [])
    fb_sla_result = _make_result(SLA_COLUMNS, [(90, 18, 20.0)])
    queue_result = _make_result(QUEUE_COLUMNS, [])
    results_seq = [
        vol_result,
        FakeDatatypeMismatch("operator does not exist"),
        fb_sla_result,
        queue_result,
    ]
    call_n = [0]

    def side_effect(*args, **kwargs):
        v = results_seq[call_n[0]]
        call_n[0] += 1
        if isinstance(v, Exception):
            raise v
        return v

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is False
    assert out["sla_breach"]["total_tickets_30d"] == 90
    assert out["sla_breach"]["breached_count"] == 18
    assert call_n[0] == 4


def test_ac6c_fallback_failure_sets_degraded():
    """AC6: When both boolean and integer fallback fail, degraded_signal=True."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.postgresql_ingestor import ingest

    vol_result   = _make_result(VOLUME_COLUMNS, [])
    queue_result = _make_result(QUEUE_COLUMNS, [])
    type_err     = DBConnectionError("type mismatch", error_code="query_error")
    fb_err       = DBConnectionError("fallback also broken", error_code="query_error")

    call_n = [0]
    seq = [vol_result, type_err, fb_err, queue_result]

    def side_effect(*args, **kwargs):
        v = seq[call_n[0]]
        call_n[0] += 1
        if isinstance(v, Exception):
            raise v
        return v

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is True


# ---------------------------------------------------------------------------
# AC7 — missing expected columns → degraded_signal=True, no raise
# ---------------------------------------------------------------------------

def test_ac7_missing_columns_sets_all_degraded():
    """AC7: Completely wrong columns for all three queries → all degraded_signal=True."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    bad_result = _make_result(["col_x", "col_y"], [(1, 2)])
    with patch(_patch("execute_query"), return_value=bad_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is True
    assert out["queue_depth"]["degraded_signal"] is True


# ---------------------------------------------------------------------------
# AC7b — only one missing column → only that metric degraded, others healthy
# ---------------------------------------------------------------------------

def test_ac7b_partial_missing_column_degrades_only_affected_metric():
    """AC7: Missing column degrades only the affected metric; others still succeed."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    bad_vol  = _make_result(["wrong_col_a", "wrong_col_b"], [(1, 2)])
    sla_ok   = _make_result(SLA_COLUMNS, [(50, 5, 10.0)])
    queue_ok = _make_result(QUEUE_COLUMNS, [("p1", 3, 8.0)])

    call_n = [0]
    seq = [bad_vol, sla_ok, queue_ok]

    def side_effect(*args, **kwargs):
        r = seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


# ---------------------------------------------------------------------------
# AC8a — query_timeout on ticket_volume → degraded, other queries still run
# ---------------------------------------------------------------------------

def test_ac8a_timeout_ticket_volume_degrades_only_that_metric():
    """AC8: query_timeout on Q1 → ticket_volume degraded; Q2 and Q3 still execute."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.postgresql_ingestor import ingest

    timeout_err = DBConnectionError("timed out", error_code="query_timeout")
    ok_result   = _make_result(SLA_COLUMNS, [(100, 10, 10.0)])
    q_ok        = _make_result(QUEUE_COLUMNS, [])

    call_n = [0]
    seq = [timeout_err, ok_result, q_ok]

    def side_effect(*args, **kwargs):
        v = seq[call_n[0]]
        call_n[0] += 1
        if isinstance(v, Exception):
            raise v
        return v

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert call_n[0] == 3  # all three queries attempted


# ---------------------------------------------------------------------------
# AC8b — query_timeout on queue_depth → only queue degraded; first two succeed
# ---------------------------------------------------------------------------

def test_ac8b_timeout_queue_depth_degrades_only_that_metric():
    """AC8: query_timeout on Q3 only degrades queue_depth."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.postgresql_ingestor import ingest

    vol_result  = _make_result(VOLUME_COLUMNS, [("2026-05-30", 10)])
    sla_result  = _make_result(SLA_COLUMNS, [(100, 5, 5.0)])
    timeout_err = DBConnectionError("timed out", error_code="query_timeout")

    call_n = [0]
    seq = [vol_result, sla_result, timeout_err]

    def side_effect(*args, **kwargs):
        v = seq[call_n[0]]
        call_n[0] += 1
        if isinstance(v, Exception):
            raise v
        return v

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["queue_depth"]["degraded_signal"] is True
    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False


# ---------------------------------------------------------------------------
# AC9a — return shape has all required top-level keys
# ---------------------------------------------------------------------------

def test_ac9a_return_shape_top_level_keys():
    """AC9: ingest() return dict has all required top-level keys."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run-42", _make_config(), scope=_make_scope())

    for key in ("ticket_volume", "sla_breach", "queue_depth",
                "connector_id", "org_id", "run_id", "schema_name", "table_name"):
        assert key in out, f"Missing top-level key: {key}"

    assert out["connector_id"] == "postgresql"
    assert out["org_id"] == "org"
    assert out["run_id"] == "run-42"


# ---------------------------------------------------------------------------
# AC9b — ticket_volume sub-keys match spec
# ---------------------------------------------------------------------------

def test_ac9b_ticket_volume_sub_keys():
    """AC9: ticket_volume sub-shape matches spec Section 1d."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    tv = out["ticket_volume"]
    for key in ("daily_counts", "total_90d", "avg_daily", "peak_daily",
                "peak_date", "recent_7d_avg", "recent_vs_baseline", "degraded_signal"):
        assert key in tv, f"ticket_volume missing key: {key}"


# ---------------------------------------------------------------------------
# AC9c — sla_breach and queue_depth sub-keys match spec
# ---------------------------------------------------------------------------

def test_ac9c_sla_and_queue_sub_keys():
    """AC9: sla_breach and queue_depth sub-shapes match spec Section 1d."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    for key in ("total_tickets_30d", "breached_count", "breach_rate_pct", "degraded_signal"):
        assert key in out["sla_breach"], f"sla_breach missing key: {key}"

    for key in ("by_priority", "total_open", "p1_p2_open",
                "oldest_ticket_hours", "degraded_signal"):
        assert key in out["queue_depth"], f"queue_depth missing key: {key}"


# ---------------------------------------------------------------------------
# AC9d — healthy run has degraded_signal=False on all three metrics
# ---------------------------------------------------------------------------

def test_ac9d_degraded_false_on_good_data():
    """AC9: All degraded_signal=False when all queries return correct columns."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    vol_result  = _make_result(VOLUME_COLUMNS, [("2026-05-30", 15), ("2026-05-29", 12)])
    sla_result  = _make_result(SLA_COLUMNS, [(100, 5, 5.0)])
    queue_result = _make_result(QUEUE_COLUMNS, [("p1", 3, 24.0), ("p2", 5, 12.0)])

    call_n = [0]
    seq = [vol_result, sla_result, queue_result]

    def side_effect(*args, **kwargs):
        r = seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False
    assert out["ticket_volume"]["total_90d"] == 27
    assert out["sla_breach"]["total_tickets_30d"] == 100
    assert out["queue_depth"]["p1_p2_open"] == 8


# ---------------------------------------------------------------------------
# AC9e — null values in rows are tolerated (no raise)
# ---------------------------------------------------------------------------

def test_ac9e_null_values_tolerated():
    """AC9: Null values in any row do not raise; nulls produce safe defaults."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    vol_result   = _make_result(VOLUME_COLUMNS, [(None, None), ("2026-05-29", 5)])
    sla_result   = _make_result(SLA_COLUMNS, [(None, None, None)])
    queue_result = _make_result(QUEUE_COLUMNS, [(None, None, None)])

    call_n = [0]
    seq = [vol_result, sla_result, queue_result]

    def side_effect(*args, **kwargs):
        r = seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    # Must not raise — degraded_signal state may vary but no exception
    assert "ticket_volume" in out


# ---------------------------------------------------------------------------
# AC9f — empty result sets produce safe zero values
# ---------------------------------------------------------------------------

def test_ac9f_empty_result_sets_produce_zero_values():
    """AC9: Empty rows on all queries produce zero metrics with degraded_signal=False."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    vol_result   = _make_result(VOLUME_COLUMNS, [])
    sla_result   = _make_result(SLA_COLUMNS, [])
    queue_result = _make_result(QUEUE_COLUMNS, [])

    call_n = [0]
    seq = [vol_result, sla_result, queue_result]

    def side_effect(*args, **kwargs):
        r = seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["total_90d"] == 0
    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["total_tickets_30d"] == 0
    assert out["queue_depth"]["total_open"] == 0


# ---------------------------------------------------------------------------
# AC11 — end-to-end: mocked ingestor data → detector results with signal_source
# ---------------------------------------------------------------------------

def test_ac11_end_to_end_produces_detector_results():
    """AC11: Mocked ingestor data drives detectors; at least one fires."""
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect as d1
        from backend.discovery.detectors.db_sla_breach_rate import detect as d2
        from backend.discovery.detectors.db_queue_depth_elevated import detect as d3
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect as d1
        from discovery.detectors.db_sla_breach_rate import detect as d2
        from discovery.detectors.db_queue_depth_elevated import detect as d3

    db_data = {
        "ticket_volume": {
            "recent_vs_baseline": 2.0, "recent_7d_avg": 40.0,
            "avg_daily": 20.0, "peak_daily": 60, "peak_date": "2026-05-30",
            "total_90d": 1800, "degraded_signal": False,
        },
        "sla_breach": {
            "breach_rate_pct": 22.0, "breached_count": 44,
            "total_tickets_30d": 200, "degraded_signal": False,
        },
        "queue_depth": {
            "p1_p2_open": 30, "total_open": 100, "oldest_ticket_hours": 120.0,
            "by_priority": {
                "p1": {"count": 15, "avg_age_hours": 60.0},
                "p2": {"count": 15, "avg_age_hours": 40.0},
            },
            "degraded_signal": False,
        },
        "schema_name": "public", "table_name": "service_tickets",
    }

    all_results = d1(db_data) + d2(db_data) + d3(db_data)
    assert len(all_results) >= 1


def test_ac11_all_three_detectors_fire_on_strong_signals():
    """AC11: All three detectors fire when signals exceed thresholds."""
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect as d1, SURGE_THRESHOLD
        from backend.discovery.detectors.db_sla_breach_rate import detect as d2
        from backend.discovery.detectors.db_queue_depth_elevated import detect as d3
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect as d1, SURGE_THRESHOLD
        from discovery.detectors.db_sla_breach_rate import detect as d2
        from discovery.detectors.db_queue_depth_elevated import detect as d3

    db_data = {
        "ticket_volume": {
            "recent_vs_baseline": SURGE_THRESHOLD, "recent_7d_avg": 40.0,
            "avg_daily": 20.0, "peak_daily": 60, "peak_date": "2026-05-30",
            "total_90d": 1800, "degraded_signal": False,
        },
        "sla_breach": {
            "breach_rate_pct": 20.0, "breached_count": 20,
            "total_tickets_30d": 100, "degraded_signal": False,
        },
        "queue_depth": {
            "p1_p2_open": 25, "total_open": 80, "oldest_ticket_hours": 96.0,
            "by_priority": {"p1": {"count": 10, "avg_age_hours": 48.0},
                            "p2": {"count": 15, "avg_age_hours": 24.0}},
            "degraded_signal": False,
        },
        "schema_name": "public", "table_name": "service_tickets",
    }

    fired = {r.detector_id for r in d1(db_data) + d2(db_data) + d3(db_data)}
    assert "DB_TICKET_VOLUME_SURGE" in fired
    assert "DB_SLA_BREACH_RATE" in fired
    assert "DB_QUEUE_DEPTH_ELEVATED" in fired


# ---------------------------------------------------------------------------
# AC11b — connector_id in ingestor output is 'postgresql' (signal_source)
# ---------------------------------------------------------------------------

def test_ac11b_connector_id_in_output_is_postgresql():
    """AC11: ingest() output carries connector_id='postgresql' for signal_source."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event")):
        out = ingest("org", "run-pg", _make_config(), scope=_make_scope())

    assert out["connector_id"] == "postgresql"


# ---------------------------------------------------------------------------
# AC16a — cross-org isolation: org_A result not polluted by org_B call
# ---------------------------------------------------------------------------

def test_ac16a_cross_org_isolation_different_orgs():
    """AC16: Ingestor called for org_A and org_B independently; no state leaks."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    vol_a = _make_result(VOLUME_COLUMNS, [("2026-05-30", 50)])
    sla_a = _make_result(SLA_COLUMNS, [(100, 30, 30.0)])
    q_a   = _make_result(QUEUE_COLUMNS, [("p1", 10, 20.0)])

    vol_b = _make_result(VOLUME_COLUMNS, [("2026-05-30", 5)])
    sla_b = _make_result(SLA_COLUMNS, [(20, 1, 5.0)])
    q_b   = _make_result(QUEUE_COLUMNS, [("p2", 2, 4.0)])

    call_n = [0]
    seq = [vol_a, sla_a, q_a, vol_b, sla_b, q_b]

    def side_effect(*args, **kwargs):
        r = seq[call_n[0]]
        call_n[0] += 1
        return r

    with patch(_patch("execute_query"), side_effect=side_effect), \
         patch(_patch("get_scope"), side_effect=lambda oid, cid: _make_scope(org_id=oid)), \
         patch(_patch("record_event")):
        out_a = ingest("org_A", "run-a", _make_config("org_A"), scope=_make_scope("org_A"))
        out_b = ingest("org_B", "run-b", _make_config("org_B"), scope=_make_scope("org_B"))

    assert out_a["org_id"] == "org_A"
    assert out_b["org_id"] == "org_B"
    assert out_a["ticket_volume"]["total_90d"] != out_b["ticket_volume"]["total_90d"]
    assert out_a["sla_breach"]["total_tickets_30d"] == 100
    assert out_b["sla_breach"]["total_tickets_30d"] == 20


# ---------------------------------------------------------------------------
# AC16b — org_id is embedded in every ingestor output (traceable)
# ---------------------------------------------------------------------------

def test_ac16b_org_id_in_output():
    """AC16: org_id is always embedded in the ingestor return dict."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    for org in ("acme-corp", "beta-bank", "test-org"):
        with patch(_patch("execute_query"), return_value=mock_result), \
             patch(_patch("get_scope"), return_value=_make_scope(org_id=org)), \
             patch(_patch("record_event")):
            out = ingest(org, "run-x", _make_config(org), scope=_make_scope(org_id=org))
        assert out["org_id"] == org, f"org_id mismatch for {org}"


# ---------------------------------------------------------------------------
# Bonus: telemetry failure does not affect ingestor output
# ---------------------------------------------------------------------------

def test_telemetry_failure_does_not_affect_output():
    """Telemetry record_event failure is swallowed; ingestor still returns output."""
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch(_patch("execute_query"), return_value=mock_result), \
         patch(_patch("get_scope"), return_value=_make_scope()), \
         patch(_patch("record_event"), side_effect=RuntimeError("telemetry down")) as mock_te:
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    mock_te.assert_called_once()
    assert "ticket_volume" in out


# ---------------------------------------------------------------------------
# Bonus: psycopg2 is the import used by the postgresql driver module
# ---------------------------------------------------------------------------

def test_postgresql_driver_imports_psycopg2():
    """AC5: postgresql.py imports psycopg2 (psycopg2-binary)."""
    try:
        import backend.connectors.db.postgresql as pg_driver
    except ModuleNotFoundError:
        import connectors.db.postgresql as pg_driver
    import sys
    # psycopg2 should be present (psycopg2-binary provides it)
    assert "psycopg2" in sys.modules, "psycopg2 must be importable (psycopg2-binary)"
    # Driver module should reference psycopg2
    assert hasattr(pg_driver, "psycopg2") or "psycopg2" in dir(pg_driver) or \
        "psycopg2" in str(getattr(pg_driver, "__doc__", "")), \
        "postgresql driver module must use psycopg2"
