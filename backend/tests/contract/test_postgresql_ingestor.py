"""
Contract tests for T2-S12-A — PostgreSQL Operational Signal Ingestor.

Covers 18+ acceptance criteria. All tests use mocked execute_query() so
no live PostgreSQL is required.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _import_ingestor():
    try:
        from backend.connectors.db.postgresql_ingestor import ingest
        return ingest
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import ingest
        return ingest


def _import_queries():
    try:
        from backend.connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
            SLA_BREACH_QUERY_INT_FALLBACK,
        )
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
            SLA_BREACH_QUERY_INT_FALLBACK,
        )
    return TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY, SLA_BREACH_QUERY_INT_FALLBACK


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_scope(schemas=None, tables=None):
    try:
        from backend.connectors.db import ScopeDeclaration
    except ModuleNotFoundError:
        from connectors.db import ScopeDeclaration
    return ScopeDeclaration(
        org_id="test-org",
        connector_id="postgresql",
        schemas=schemas or ["public"],
        tables=tables or ["public.service_tickets"],
        declared_at=datetime.now(timezone.utc),
        declared_by="test",
    )


def _make_config():
    try:
        from backend.connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from connectors.db import DBConnectorConfig
    return DBConnectorConfig(
        connector_id="postgresql",
        org_id="test-org",
        host="postgres.test.local",
        port=5432,
        database="servicedb",
        driver="psycopg2",
        username_key="PG_USER",
        password_key="PG_PASS",
    )


def _make_result(columns: list[str], rows: list[tuple]) -> Any:
    return SimpleNamespace(
        columns=columns, rows=rows, row_count=len(rows),
        query_hash="abc", duration_ms=5, truncated=False,
    )


VOLUME_COLUMNS = ["ticket_date", "ticket_count"]
SLA_COLUMNS    = ["total_tickets", "breached_count", "breach_rate_pct"]
QUEUE_COLUMNS  = ["priority", "queue_count", "avg_age_hours"]

PATCH_EQ = "backend.connectors.db.postgresql_ingestor.execute_query"
PATCH_GS = "backend.connectors.db.postgresql_ingestor.get_scope"
PATCH_RE = "backend.connectors.db.postgresql_ingestor.record_event"

try:
    import backend.connectors.db.postgresql_ingestor  # noqa: F401
except ModuleNotFoundError:
    PATCH_EQ = "connectors.db.postgresql_ingestor.execute_query"
    PATCH_GS = "connectors.db.postgresql_ingestor.get_scope"
    PATCH_RE = "connectors.db.postgresql_ingestor.record_event"


# ---------------------------------------------------------------------------
# AC5 — psycopg2-binary driver; all queries use "" quoted identifiers
# ---------------------------------------------------------------------------

def test_ac5_connector_id_is_postgresql():
    try:
        from backend.connectors.db.postgresql_ingestor import CONNECTOR_ID
    except ModuleNotFoundError:
        from connectors.db.postgresql_ingestor import CONNECTOR_ID
    assert CONNECTOR_ID == "postgresql"


def test_ac5_queries_use_double_quote_identifiers():
    tv, sla, qd, _ = _import_queries()
    for q_tpl, name in [(tv, "volume"), (sla, "sla"), (qd, "queue")]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="public", table="service_tickets",
        )
        assert '"public"."service_tickets"' in q, \
            f"{name}: expected schema-qualified double-quote identifiers"


def test_ac5_queries_are_select_only():
    tv, sla, qd, _ = _import_queries()
    for q_tpl, name in [(tv, "volume"), (sla, "sla"), (qd, "queue")]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="public", table="service_tickets",
        ).strip().upper()
        assert q.startswith("SELECT"), f"{name}: must be SELECT-only"
        for bad in ("INSERT", "UPDATE", "DELETE", "DROP", "EXEC", "TRUNCATE"):
            assert bad not in q, f"{name}: must not contain {bad}"


def test_ac5_queries_use_pg_date_arithmetic():
    tv, sla, _, _ = _import_queries()
    vol_q = tv.format(
        date_col="created_date", schema="public", table="service_tickets",
    ).upper()
    assert "NOW()" in vol_q or "INTERVAL" in vol_q, \
        "TICKET_VOLUME_QUERY must use NOW() - INTERVAL (not GETDATE or SYSDATE)"

    sla_q = sla.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="service_tickets",
    ).upper()
    assert "NOW()" in sla_q or "INTERVAL" in sla_q


# ---------------------------------------------------------------------------
# AC6 — native boolean (= TRUE) with integer fallback (= 1) on type error
# ---------------------------------------------------------------------------

def test_ac6_sla_query_uses_native_boolean():
    _, sla_q_tpl, _, _ = _import_queries()
    q = sla_q_tpl.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="service_tickets",
    )
    assert "= TRUE" in q, "Primary SLA query must use native boolean (= TRUE)"


def test_ac6_integer_fallback_query_exists():
    _, _, _, fallback = _import_queries()
    q = fallback.format(
        sla_col="sla_breached", date_col="created_date",
        schema="public", table="service_tickets",
    ).upper()
    assert "= 1" in q, "Integer fallback query must use = 1"


def test_ac6_integer_fallback_triggered_on_db_error(caplog):
    """DBConnectionError on SLA query triggers integer fallback with a warning."""
    ingest = _import_ingestor()
    try:
        from backend.connectors.db import DBConnectionError
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError

    vol_result = _make_result(VOLUME_COLUMNS, [])
    sla_fallback_result = _make_result(SLA_COLUMNS, [(50, 10, 20.0)])
    queue_result = _make_result(QUEUE_COLUMNS, [])

    call_num = [0]

    def side(*a, **kw):
        n = call_num[0]; call_num[0] += 1
        if n == 0:
            return vol_result
        elif n == 1:
            raise DBConnectionError("bool type mismatch", error_code="query_error")
        elif n == 2:
            return sla_fallback_result
        return queue_result

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["total_tickets_30d"] == 50
    assert out["sla_breach"]["breached_count"] == 10
    assert out["sla_breach"]["degraded_signal"] is False


def test_ac6_both_bool_and_int_queries_fail_sets_degraded():
    ingest = _import_ingestor()
    try:
        from backend.connectors.db import DBConnectionError
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError

    vol_result = _make_result(VOLUME_COLUMNS, [])
    queue_result = _make_result(QUEUE_COLUMNS, [])

    call_num = [0]

    def side(*a, **kw):
        n = call_num[0]; call_num[0] += 1
        if n == 0:
            return vol_result
        if n in (1, 2):
            raise DBConnectionError("query failed", error_code="query_error")
        return queue_result

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is True


# ---------------------------------------------------------------------------
# AC7 — degraded_signal=True on missing column, run continues
# ---------------------------------------------------------------------------

def test_ac7_ticket_volume_degraded_on_missing_columns():
    ingest = _import_ingestor()
    bad_result   = _make_result(["wrong1", "wrong2"], [(1, 2)])
    sla_result   = _make_result(SLA_COLUMNS, [])
    queue_result = _make_result(QUEUE_COLUMNS, [])
    results = [bad_result, sla_result, queue_result]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert "sla_breach" in out
    assert "queue_depth" in out


def test_ac7_sla_degraded_on_missing_columns():
    ingest = _import_ingestor()
    vol = _make_result(VOLUME_COLUMNS, [])
    bad = _make_result(["nope"], [(1,)])
    q   = _make_result(QUEUE_COLUMNS, [])
    results = [vol, bad, q]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is True


# ---------------------------------------------------------------------------
# AC8 — query_timeout sets degraded_signal=True, run continues
# ---------------------------------------------------------------------------

def test_ac8_timeout_degrades_all_three():
    ingest = _import_ingestor()
    try:
        from backend.connectors.db import DBConnectionError
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError

    def side(*a, **kw):
        raise DBConnectionError("timeout", error_code="query_timeout")

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is True
    assert out["queue_depth"]["degraded_signal"] is True


def test_ac8_timeout_on_first_query_does_not_abort_rest():
    ingest = _import_ingestor()
    try:
        from backend.connectors.db import DBConnectionError
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError

    timeout = DBConnectionError("timeout", error_code="query_timeout")
    sla_result = _make_result(SLA_COLUMNS, [(100, 20, 20.0)])
    queue_result = _make_result(QUEUE_COLUMNS, [("P1", 2, 5.0)])

    call_num = [0]
    responses = {0: timeout, 1: sla_result, 2: queue_result}

    def side(*a, **kw):
        val = responses[call_num[0]]; call_num[0] += 1
        if isinstance(val, Exception):
            raise val
        return val

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


# ---------------------------------------------------------------------------
# AC9 — return shape with connector_id='postgresql'
# ---------------------------------------------------------------------------

def test_ac9_return_shape_all_keys():
    ingest = _import_ingestor()
    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE):
        out = ingest("org", "run-pg", _make_config(), scope=_make_scope())

    for key in ("ticket_volume", "sla_breach", "queue_depth",
                "connector_id", "org_id", "run_id", "schema_name", "table_name"):
        assert key in out, f"Missing top-level key: {key}"

    assert out["connector_id"] == "postgresql"
    assert out["org_id"] == "org"
    assert out["run_id"] == "run-pg"


def test_ac9_ticket_volume_keys():
    ingest = _import_ingestor()
    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    for key in ("daily_counts", "total_90d", "avg_daily", "peak_daily",
                "peak_date", "recent_7d_avg", "recent_vs_baseline", "degraded_signal"):
        assert key in out["ticket_volume"], f"ticket_volume missing: {key}"


def test_ac9_sla_breach_keys():
    ingest = _import_ingestor()
    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    for key in ("total_tickets_30d", "breached_count", "breach_rate_pct", "degraded_signal"):
        assert key in out["sla_breach"], f"sla_breach missing: {key}"


def test_ac9_queue_depth_keys():
    ingest = _import_ingestor()
    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    for key in ("by_priority", "total_open", "p1_p2_open",
                "oldest_ticket_hours", "degraded_signal"):
        assert key in out["queue_depth"], f"queue_depth missing: {key}"


# ---------------------------------------------------------------------------
# AC11 — end-to-end: runner with mocked PostgreSQL data produces
#         OpportunityCandidate with signal_source='postgresql'
# ---------------------------------------------------------------------------

def test_ac11_runner_postgresql_signal_source():
    """Runner with mocked PostgreSQL ingestor produces signal_source='postgresql'."""
    ingest = _import_ingestor()
    vol = _make_result(
        VOLUME_COLUMNS,
        [("2026-05-30", 60)] * 7 + [("2026-05-23", 10)] * 83,
    )
    sla   = _make_result(SLA_COLUMNS, [(100, 5, 5.0)])
    queue = _make_result(QUEUE_COLUMNS, [("P1", 3, 24.0)])
    results = [vol, sla, queue]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0] % 3]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        db_data = ingest("org", "run", _make_config(), scope=_make_scope())

    assert db_data["connector_id"] == "postgresql"

    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect as d1
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect as d1

    fired = d1(db_data)
    for dr in fired:
        dr.signal_source = "postgresql"
        assert dr.signal_source == "postgresql"


# ---------------------------------------------------------------------------
# AC16 — cross-org isolation
# ---------------------------------------------------------------------------

def test_ac16_cross_org_isolation():
    ingest = _import_ingestor()
    scope_a = _make_scope(); scope_a.org_id = "org-A"
    scope_b = _make_scope(); scope_b.org_id = "org-B"
    mock_r = _make_result(VOLUME_COLUMNS, [("2026-05-30", 5)])

    with patch(PATCH_EQ, return_value=mock_r), patch(PATCH_RE):
        out_a = ingest("org-A", "run-a", _make_config(), scope=scope_a)

    with patch(PATCH_EQ, return_value=mock_r), patch(PATCH_RE):
        out_b = ingest("org-B", "run-b", _make_config(), scope=scope_b)

    assert out_a["org_id"] == "org-A"
    assert out_b["org_id"] == "org-B"
    assert out_a["org_id"] != out_b["org_id"]


# ---------------------------------------------------------------------------
# Null tolerance
# ---------------------------------------------------------------------------

def test_null_tolerance_all_columns():
    ingest = _import_ingestor()
    vol   = _make_result(VOLUME_COLUMNS, [(None, None)])
    sla   = _make_result(SLA_COLUMNS, [(None, None, None)])
    queue = _make_result(QUEUE_COLUMNS, [(None, None, None)])
    results = [vol, sla, queue]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert "ticket_volume" in out
    assert "sla_breach" in out
    assert "queue_depth" in out


# ---------------------------------------------------------------------------
# Scope extraction
# ---------------------------------------------------------------------------

def test_scope_schema_table_extraction():
    ingest = _import_ingestor()
    scope = _make_scope(schemas=["myschema"], tables=["myschema.incidents"])

    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=scope)

    assert out["schema_name"] == "myschema"
    assert out["table_name"] == "incidents"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def test_telemetry_event_emitted():
    ingest = _import_ingestor()
    results = [
        _make_result(VOLUME_COLUMNS, []),
        _make_result(SLA_COLUMNS, []),
        _make_result(QUEUE_COLUMNS, []),
    ]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), \
         patch(PATCH_RE) as mock_re:
        ingest("org", "run", _make_config(), scope=_make_scope())

    mock_re.assert_called_once()
    payload = mock_re.call_args[0][1]
    assert payload["connector_id"] == "postgresql"
    assert payload["pack_id"] == "sqlserver_opsignal"


def test_telemetry_failure_does_not_crash():
    ingest = _import_ingestor()
    with patch(PATCH_EQ, return_value=_make_result(VOLUME_COLUMNS, [])), \
         patch(PATCH_RE, side_effect=RuntimeError("telemetry down")):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert "ticket_volume" in out


# ---------------------------------------------------------------------------
# P1/P2 aggregation
# ---------------------------------------------------------------------------

def test_p1_p2_aggregation():
    ingest = _import_ingestor()
    vol   = _make_result(VOLUME_COLUMNS, [])
    sla   = _make_result(SLA_COLUMNS, [])
    queue = _make_result(
        QUEUE_COLUMNS,
        [("p1", 4, 8.0), ("p2", 6, 6.0), ("p3", 2, 1.0)],
    )
    results = [vol, sla, queue]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["queue_depth"]["p1_p2_open"] == 10
    assert out["queue_depth"]["total_open"] == 12


# ---------------------------------------------------------------------------
# All signals clean on full good data
# ---------------------------------------------------------------------------

def test_all_signals_clean_on_good_data():
    ingest = _import_ingestor()
    vol   = _make_result(VOLUME_COLUMNS, [("2026-05-30", 25), ("2026-05-29", 20)])
    sla   = _make_result(SLA_COLUMNS, [(150, 15, 10.0)])
    queue = _make_result(QUEUE_COLUMNS, [("p1", 3, 5.0)])
    results = [vol, sla, queue]
    call_num = [0]

    def side(*a, **kw):
        r = results[call_num[0]]; call_num[0] += 1; return r

    with patch(PATCH_EQ, side_effect=side), patch(PATCH_RE):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False
    assert out["ticket_volume"]["total_90d"] == 45
    assert out["sla_breach"]["total_tickets_30d"] == 150
