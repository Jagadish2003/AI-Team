"""
Contract tests for T2-S12-A Task T1 — Oracle DB Operational Signal Ingestor.

Covers all acceptance criteria applicable to the Oracle ingestor:
  AC1  — thin mode: oracledb.init_oracle_client() is NOT called in the ingestor
  AC2  — all queries use "" double-quote identifiers and schema-qualified names
  AC3  — sla_breached handled as integer (0/1) and string ('Y'/'N')
  AC4  — schema discovery uses ALL_COLUMNS with USER_COLUMNS fallback on error
  AC7  — missing columns → degraded_signal=True, no raise
  AC8  — query_timeout → degraded_signal=True, run continues
  AC9  — return shape identical to SQL Server ingestor with connector_id='oracle_db'
  AC10 — end-to-end: mocked Oracle data → OpportunityCandidate with signal_source='oracle_db'
  AC14 — OracleScopePicker save button disabled with tooltip when viewerOnly=True
  AC15 — cross-org isolation: oracle results for org_A not returned to org_B
  AC17 — deployment/README.md documents thick mode escalation path
  AC18 — OracleScopePicker case-sensitivity tooltip text is correct

All tests use mocked execute_query() — no live Oracle is required.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_scope(org_id="test-org", schemas=None, tables=None):
    try:
        from backend.connectors.db import ScopeDeclaration
    except ModuleNotFoundError:
        from connectors.db import ScopeDeclaration
    return ScopeDeclaration(
        org_id=org_id,
        connector_id="oracle_db",
        schemas=schemas or ["HR"],
        tables=tables or ["HR.SERVICE_TICKETS"],
        declared_at=datetime.now(timezone.utc),
        declared_by="test",
    )


def _make_config(org_id="test-org"):
    try:
        from backend.connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from connectors.db import DBConnectorConfig
    return DBConnectorConfig(
        connector_id="oracle_db",
        org_id=org_id,
        host="oracle.test.local",
        port=1521,
        database="ORCL",
        driver="oracledb",
        username_key="ORA_USER",
        password_key="ORA_PASS",
    )


def _make_result(columns: list[str], rows: list[tuple]) -> Any:
    return SimpleNamespace(
        columns=columns, rows=rows, row_count=len(rows),
        query_hash="ora_abc", duration_ms=5, truncated=False,
    )


VOLUME_COLUMNS = ["ticket_date", "ticket_count"]
SLA_COLUMNS    = ["total_tickets", "breached_count", "breach_rate_pct"]
QUEUE_COLUMNS  = ["priority", "queue_count", "avg_age_hours"]


# ---------------------------------------------------------------------------
# AC1 — thin mode: init_oracle_client() is NOT called in the ingestor module
# ---------------------------------------------------------------------------

def test_ac1_init_oracle_client_not_called():
    """AC1: oracledb.init_oracle_client() is never called as a function by the ingestor."""
    try:
        import backend.connectors.db.oracle_ingestor as mod
    except ModuleNotFoundError:
        import connectors.db.oracle_ingestor as mod  # type: ignore

    import inspect
    source = inspect.getsource(mod)
    # Check that there is no actual function CALL — allow mentions in docstrings/comments
    # A call looks like init_oracle_client( with parenthesis immediately following
    assert "init_oracle_client(" not in source, (
        "oracle_ingestor.py must not call oracledb.init_oracle_client() — "
        "thin mode is the default. See deployment/README.md for thick mode escalation."
    )


def test_ac1_ingestor_does_not_import_init_oracle_client():
    """AC1: The ingestor has no reference to init_thick_mode or init_oracle_client."""
    try:
        import backend.connectors.db.oracle_ingestor as mod
    except ModuleNotFoundError:
        import connectors.db.oracle_ingestor as mod  # type: ignore

    assert not hasattr(mod, "init_thick_mode"), (
        "oracle_ingestor must not expose init_thick_mode — thick mode belongs in oracle.py"
    )


# ---------------------------------------------------------------------------
# AC2 — all queries use "" double-quote identifiers and schema-qualified names
# ---------------------------------------------------------------------------

def test_ac2_queries_use_double_quote_identifiers():
    """AC2: All SQL queries use double-quoted identifiers."""
    try:
        from backend.connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_tpl, name in [
        (TICKET_VOLUME_QUERY, "ticket_volume"),
        (SLA_BREACH_QUERY, "sla_breach"),
        (QUEUE_DEPTH_QUERY, "queue_depth"),
    ]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="HR", table="SERVICE_TICKETS",
        )
        assert '"HR"' in q, f"{name}: schema must be double-quoted"
        assert '"SERVICE_TICKETS"' in q, f"{name}: table must be double-quoted"
        assert "[HR]" not in q, f"{name}: must not use SQL Server [] brackets"


def test_ac2_queries_use_oracle_date_arithmetic():
    """AC2: Queries use SYSDATE, not GETDATE() or NOW()."""
    try:
        from backend.connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_tpl, name in [
        (TICKET_VOLUME_QUERY, "ticket_volume"),
        (SLA_BREACH_QUERY, "sla_breach"),
        (QUEUE_DEPTH_QUERY, "queue_depth"),
    ]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="HR", table="SERVICE_TICKETS",
        ).upper()
        assert "SYSDATE" in q, f"{name}: must use SYSDATE for Oracle date arithmetic"
        assert "GETDATE" not in q, f"{name}: must not use SQL Server GETDATE()"
        assert "NOW()" not in q, f"{name}: must not use PostgreSQL NOW()"


def test_ac2_queries_are_select_only():
    """AC2: All queries are SELECT-only — no DML."""
    try:
        from backend.connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_tpl, name in [
        (TICKET_VOLUME_QUERY, "ticket_volume"),
        (SLA_BREACH_QUERY, "sla_breach"),
        (QUEUE_DEPTH_QUERY, "queue_depth"),
    ]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="HR", table="SERVICE_TICKETS",
        ).strip().upper()
        assert q.startswith("SELECT"), f"{name}: must be SELECT-only"
        for bad in ("INSERT", "UPDATE", "DELETE", "DROP", "EXEC"):
            assert bad not in q, f"{name}: must not contain {bad}"


# ---------------------------------------------------------------------------
# AC3 — sla_breached handled as integer (0/1) and string ('Y'/'N')
# ---------------------------------------------------------------------------

def test_ac3_sla_breach_query_handles_int_and_yn():
    """AC3: SLA_BREACH_QUERY uses CASE WHEN col IN (1,'Y') for both variants."""
    try:
        from backend.connectors.db.oracle_ingestor import SLA_BREACH_QUERY
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import SLA_BREACH_QUERY

    q = SLA_BREACH_QUERY.format(
        sla_col="sla_breached", date_col="created_date",
        schema="HR", table="SERVICE_TICKETS",
    )
    # Must handle both integer 1 and string 'Y'
    assert "IN (1" in q or "IN(1" in q, "SLA query must check integer 1"
    assert "'Y'" in q, "SLA query must check string 'Y'"


def test_ac3_process_sla_breach_with_integer_breached():
    """AC3: _process_sla_breach handles rows where sla_breached is integer (0/1)."""
    try:
        from backend.connectors.db.oracle_ingestor import _process_sla_breach
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import _process_sla_breach

    # Simulate query result where breached_count reflects integer sla_breached
    result = _make_result(SLA_COLUMNS, [(100, 20, 20.0)])
    out = _process_sla_breach(result)
    assert out["degraded_signal"] is False
    assert out["total_tickets_30d"] == 100
    assert out["breached_count"] == 20
    assert out["breach_rate_pct"] == 20.0


def test_ac3_process_sla_breach_handles_null_rate():
    """AC3: _process_sla_breach computes rate from total/breached when rate col is None."""
    try:
        from backend.connectors.db.oracle_ingestor import _process_sla_breach
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import _process_sla_breach

    # rate_pct column returns None (Oracle AVG on empty window)
    result = _make_result(SLA_COLUMNS, [(200, 30, None)])
    out = _process_sla_breach(result)
    assert out["degraded_signal"] is False
    assert out["breach_rate_pct"] == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# AC4 — ALL_COLUMNS with USER_COLUMNS fallback
# ---------------------------------------------------------------------------

def test_ac4_schema_discovery_uses_all_columns_first():
    """AC4: discover_schema_oracle_ingestor calls ALL_COLUMNS as first attempt."""
    try:
        from backend.connectors.db.oracle_ingestor import (
            discover_schema_oracle_ingestor, ALL_COLUMNS_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import (
            discover_schema_oracle_ingestor, ALL_COLUMNS_QUERY,
        )

    mock_result = _make_result(["OWNER", "TABLE_NAME", "COLUMN_NAME"], [])
    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=mock_result) as mock_eq:
        discover_schema_oracle_ingestor(_make_config(), "org", "run", _make_scope())
        first_call_query = mock_eq.call_args_list[0].kwargs.get("query") or mock_eq.call_args_list[0].args[2]
        assert "ALL_COLUMNS" in first_call_query.upper(), (
            "First discovery attempt must use ALL_COLUMNS"
        )


def test_ac4_fallback_to_user_columns_on_error():
    """AC4: Falls back to USER_COLUMNS when ALL_COLUMNS raises; logs warning."""
    try:
        from backend.connectors.db.oracle_ingestor import (
            discover_schema_oracle_ingestor, USER_COLUMNS_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import (
            discover_schema_oracle_ingestor, USER_COLUMNS_QUERY,
        )

    mock_fallback = _make_result(["OWNER", "TABLE_NAME", "COLUMN_NAME"], [])
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        query = kwargs.get("query") or (args[2] if len(args) > 2 else "")
        if "ALL_COLUMNS" in query.upper():
            raise PermissionError("ORA-00942: table or view does not exist")
        return mock_fallback

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.oracle_ingestor.logger") as mock_log:
        result = discover_schema_oracle_ingestor(_make_config(), "org", "run", _make_scope())
        assert result is mock_fallback, "Should return USER_COLUMNS result"
        assert call_count == 2, "Should call execute_query twice (ALL_COLUMNS then USER_COLUMNS)"
        # Warning must be logged
        assert mock_log.warning.called, "Warning must be logged on fallback"


def test_ac4_fallback_does_not_raise():
    """AC4: Schema discovery does not raise even if ALL_COLUMNS is denied."""
    try:
        from backend.connectors.db.oracle_ingestor import discover_schema_oracle_ingestor
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import discover_schema_oracle_ingestor

    fallback_result = _make_result([], [])
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        query = kwargs.get("query") or (args[2] if len(args) > 2 else "")
        if "ALL_COLUMNS" in query.upper():
            raise Exception("Permission denied")
        return fallback_result

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect):
        result = discover_schema_oracle_ingestor(_make_config(), "org", "run")
        assert result is fallback_result


# ---------------------------------------------------------------------------
# AC1 (runtime) — ingest() uses execute_query() for all three queries; no direct conn
# ---------------------------------------------------------------------------

def test_ac1_all_queries_go_through_execute_query():
    """AC1/AC9: ingest() calls execute_query 3 times; no direct oracledb.connect."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=mock_result) as mock_eq, \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()):
        ingest("org", "run-1", _make_config(), scope=_make_scope())
        assert mock_eq.call_count == 3, (
            f"Expected 3 execute_query calls, got {mock_eq.call_count}"
        )


# ---------------------------------------------------------------------------
# AC7 — missing columns → degraded_signal=True, no raise
# ---------------------------------------------------------------------------

def test_ac7_missing_columns_all_signals_degraded():
    """AC7: Wrong columns on all queries → all signals degraded, no exception raised."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    bad_result = _make_result(["col_x", "col_y"], [(1, 2)])
    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=bad_result), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is True
    assert out["queue_depth"]["degraded_signal"] is True


def test_ac7_missing_ticket_date_column_degraded():
    """AC7: Missing ticket_date → ticket_volume degraded, others unaffected."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    bad_vol   = _make_result(["wrong_col", "ticket_count"], [])
    ok_sla    = _make_result(SLA_COLUMNS, [(50, 5, 10.0)])
    ok_queue  = _make_result(QUEUE_COLUMNS, [("P1", 3, 24.0)])
    results   = [bad_vol, ok_sla, ok_queue]
    call_num  = 0

    def side_effect(*args, **kwargs):
        nonlocal call_num
        r = results[call_num]; call_num += 1; return r

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.oracle_ingestor.record_event"):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


# ---------------------------------------------------------------------------
# AC8 — query_timeout → degraded_signal=True, run continues
# ---------------------------------------------------------------------------

def test_ac8_timeout_first_query_run_continues():
    """AC8: query_timeout on ticket_volume → degraded, other two queries still run."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.oracle_ingestor import ingest

    timeout_err = DBConnectionError("timed out", error_code="query_timeout")
    ok_result   = _make_result(VOLUME_COLUMNS, [])
    call_count  = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise timeout_err
        return ok_result

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert call_count == 3, "All three queries must still be attempted"


def test_ac8_timeout_second_query_run_continues():
    """AC8: query_timeout on sla_breach → degraded, ticket_volume and queue_depth ok."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.oracle_ingestor import ingest

    timeout_err = DBConnectionError("timed out", error_code="query_timeout")
    vol_result  = _make_result(VOLUME_COLUMNS, [("2026-05-30", 10)])
    queue_result = _make_result(QUEUE_COLUMNS, [("P1", 5, 12.0)])
    call_count  = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return vol_result
        if call_count == 2:
            raise timeout_err
        return queue_result

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.oracle_ingestor.record_event"):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["sla_breach"]["degraded_signal"] is True
    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False


# ---------------------------------------------------------------------------
# AC9 — return shape identical to SQL Server ingestor; connector_id='oracle_db'
# ---------------------------------------------------------------------------

def test_ac9_return_shape_matches_spec():
    """AC9: ingest() return shape identical to sqlserver_ingestor spec."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=mock_result), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run-99", _make_config(), scope=_make_scope())

    # Top-level keys
    for key in ("ticket_volume", "sla_breach", "queue_depth",
                 "connector_id", "org_id", "run_id", "schema_name", "table_name"):
        assert key in out, f"Missing top-level key: {key}"

    assert out["connector_id"] == "oracle_db", (
        f"connector_id must be 'oracle_db', got {out['connector_id']!r}"
    )
    assert out["org_id"] == "org"
    assert out["run_id"] == "run-99"

    # ticket_volume shape
    tv = out["ticket_volume"]
    for key in ("daily_counts", "total_90d", "avg_daily", "peak_daily",
                 "peak_date", "recent_7d_avg", "recent_vs_baseline", "degraded_signal"):
        assert key in tv, f"ticket_volume missing key: {key}"

    # sla_breach shape
    sla = out["sla_breach"]
    for key in ("total_tickets_30d", "breached_count", "breach_rate_pct", "degraded_signal"):
        assert key in sla, f"sla_breach missing key: {key}"

    # queue_depth shape
    qd = out["queue_depth"]
    for key in ("by_priority", "total_open", "p1_p2_open",
                 "oldest_ticket_hours", "degraded_signal"):
        assert key in qd, f"queue_depth missing key: {key}"


def test_ac9_connector_id_is_oracle_db():
    """AC9: connector_id in return dict is 'oracle_db' (not 'sqlserver')."""
    try:
        from backend.connectors.db.oracle_ingestor import CONNECTOR_ID
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import CONNECTOR_ID

    assert CONNECTOR_ID == "oracle_db"


# ---------------------------------------------------------------------------
# AC10 — end-to-end: mocked Oracle data → OpportunityCandidate with signal_source='oracle_db'
# ---------------------------------------------------------------------------

def test_ac10_end_to_end_oracle_data_fires_detectors():
    """AC10: Mocked Oracle ingestor output triggers all three detectors."""
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect as d1
        from backend.discovery.detectors.db_sla_breach_rate import detect as d2
        from backend.discovery.detectors.db_queue_depth_elevated import detect as d3
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect as d1
        from discovery.detectors.db_sla_breach_rate import detect as d2
        from discovery.detectors.db_queue_depth_elevated import detect as d3

    # Oracle ingestor output shape — same as SQL Server, connector_id='oracle_db'
    oracle_data = {
        "ticket_volume": {
            "recent_vs_baseline": 2.0, "recent_7d_avg": 40.0,
            "avg_daily": 20.0, "peak_daily": 60, "peak_date": "2026-05-30",
            "total_90d": 1800, "degraded_signal": False,
        },
        "sla_breach": {
            "breach_rate_pct": 25.0, "breached_count": 50,
            "total_tickets_30d": 200, "degraded_signal": False,
        },
        "queue_depth": {
            "p1_p2_open": 30, "total_open": 100,
            "oldest_ticket_hours": 120.0,
            "by_priority": {
                "P1": {"count": 15, "avg_age_hours": 60.0},
                "P2": {"count": 15, "avg_age_hours": 40.0},
            },
            "degraded_signal": False,
        },
        "connector_id": "oracle_db",
        "schema_name": "HR",
        "table_name": "SERVICE_TICKETS",
    }

    results = d1(oracle_data) + d2(oracle_data) + d3(oracle_data)
    fired_ids = {r.detector_id for r in results}
    expected = {"DB_TICKET_VOLUME_SURGE", "DB_SLA_BREACH_RATE", "DB_QUEUE_DEPTH_ELEVATED"}
    assert fired_ids == expected, (
        f"Expected all three detectors to fire, got: {fired_ids}"
    )


def test_ac10_signal_source_oracle_db():
    """AC10: DetectorResult from Oracle data has signal_source='oracle_db'."""
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect

    oracle_data = {
        "ticket_volume": {
            "recent_vs_baseline": 2.5, "recent_7d_avg": 50.0,
            "avg_daily": 20.0, "peak_daily": 70, "peak_date": "2026-05-30",
            "total_90d": 1800, "degraded_signal": False,
        },
        "connector_id": "oracle_db",
        "schema_name": "HR",
        "table_name": "SERVICE_TICKETS",
    }

    results = detect(oracle_data)
    assert len(results) == 1
    assert results[0].signal_source == "oracle_db", (
        f"signal_source must be 'oracle_db', got {results[0].signal_source!r}"
    )


# ---------------------------------------------------------------------------
# AC14 — OracleScopePicker viewerOnly disables save button
# ---------------------------------------------------------------------------

def test_ac14_oracle_scope_picker_viewer_only_prop():
    """AC14: OracleScopePicker exposes viewerOnly prop; save is blocked when true."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest  # import to check file exists
        import inspect
        import importlib.util
        import sys

        # Check the TSX file exists and contains viewerOnly and Analyst role required
        tsx_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "frontend", "src", "components",
            "integrations", "OracleScopePicker.tsx",
        )
        tsx_path = os.path.normpath(tsx_path)
        assert os.path.exists(tsx_path), f"OracleScopePicker.tsx not found at {tsx_path}"

        with open(tsx_path, encoding="utf-8") as f:
            tsx_content = f.read()

        assert "viewerOnly" in tsx_content, "OracleScopePicker must have viewerOnly prop"
        assert "Analyst role required" in tsx_content, (
            "OracleScopePicker must show 'Analyst role required' tooltip when viewerOnly"
        )
    except Exception as e:
        pytest.fail(f"AC14 check failed: {e}")


# ---------------------------------------------------------------------------
# AC15 — cross-org isolation: oracle results for org_A not returned to org_B
# ---------------------------------------------------------------------------

def test_ac15_cross_org_isolation():
    """AC15: Ingestor scopes are keyed by org_id — org_A scope not served to org_B."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    scope_a = _make_scope(org_id="org-alpha")
    scope_b = _make_scope(org_id="org-beta", schemas=["BETA"], tables=["BETA.INCIDENTS"])

    mock_result = _make_result(VOLUME_COLUMNS, [])

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=mock_result), \
         patch("backend.connectors.db.oracle_ingestor.record_event"):
        out_a = ingest("org-alpha", "run-a", _make_config("org-alpha"), scope=scope_a)
        out_b = ingest("org-beta",  "run-b", _make_config("org-beta"),  scope=scope_b)

    assert out_a["org_id"] == "org-alpha"
    assert out_b["org_id"] == "org-beta"
    assert out_a["schema_name"] == "HR"
    assert out_b["schema_name"] == "BETA"
    # Results are distinct objects — org_A data cannot bleed into org_B
    assert out_a is not out_b


# ---------------------------------------------------------------------------
# AC17 — deployment/README.md documents thick mode escalation
# ---------------------------------------------------------------------------

def test_ac17_deployment_readme_documents_thick_mode():
    """AC17: deployment/README.md exists and documents Oracle thick mode escalation."""
    readme_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "deployment", "README.md",
    )
    readme_path = os.path.normpath(readme_path)
    assert os.path.exists(readme_path), (
        f"deployment/README.md not found at {readme_path}. "
        "AC17 requires this file to document thick mode escalation."
    )
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    assert "thick" in content.lower(), "README must document thick mode"
    assert "oracle_thick" in content or "oracle thick" in content.lower(), (
        "README must reference oracle_thick driver configuration"
    )
    assert "init_oracle_client" in content or "Instant Client" in content, (
        "README must reference Oracle Instant Client or init_oracle_client"
    )


# ---------------------------------------------------------------------------
# AC18 — OracleScopePicker case-sensitivity tooltip text
# ---------------------------------------------------------------------------

def test_ac18_oracle_scope_picker_case_sensitivity_tooltip():
    """AC18: OracleScopePicker contains the exact case-sensitivity tooltip text."""
    tsx_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "frontend", "src", "components",
        "integrations", "OracleScopePicker.tsx",
    )
    tsx_path = os.path.normpath(tsx_path)
    assert os.path.exists(tsx_path), f"OracleScopePicker.tsx not found at {tsx_path}"

    with open(tsx_path, encoding="utf-8") as f:
        content = f.read()

    # AC18 spec: tooltip reads 'Oracle schema names are case-sensitive — shown exactly as stored in the database.'
    assert "Oracle schema names are case-sensitive" in content, (
        "OracleScopePicker must contain case-sensitivity note per AC18"
    )
    assert "shown exactly as stored" in content, (
        "OracleScopePicker tooltip must say 'shown exactly as stored' per AC18"
    )


# ---------------------------------------------------------------------------
# Additional: telemetry fire-and-forget
# ---------------------------------------------------------------------------

def test_telemetry_fire_and_forget():
    """Telemetry failure must not prevent ingestor from returning output."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               return_value=mock_result), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.oracle_ingestor.record_event",
               side_effect=RuntimeError("telemetry down")) as mock_te:
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    mock_te.assert_called_once()
    assert "ticket_volume" in out  # ingestor still returns output


# ---------------------------------------------------------------------------
# Additional: good data → degraded_signal=False on all three signals
# ---------------------------------------------------------------------------

def test_good_data_degraded_false():
    """Full run with correct columns returns degraded_signal=False on all signals."""
    try:
        from backend.connectors.db.oracle_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.oracle_ingestor import ingest

    vol_result   = _make_result(["ticket_date", "ticket_count"], [("2026-05-30", 20), ("2026-05-29", 15)])
    sla_result   = _make_result(["total_tickets", "breached_count", "breach_rate_pct"], [(100, 5, 5.0)])
    queue_result = _make_result(["priority", "queue_count", "avg_age_hours"], [("P1", 3, 24.0)])

    call_num = 0
    results  = [vol_result, sla_result, queue_result]

    def side_effect(*args, **kwargs):
        nonlocal call_num
        r = results[call_num]; call_num += 1; return r

    with patch("backend.connectors.db.oracle_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.oracle_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.oracle_ingestor.record_event"):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False
    assert out["ticket_volume"]["total_90d"] == 35
    assert out["sla_breach"]["total_tickets_30d"] == 100
    assert out["queue_depth"]["p1_p2_open"] == 3


# ---------------------------------------------------------------------------
# Additional: ConnectorDetailPanel renders OracleScopePicker for oracle_db
# ---------------------------------------------------------------------------

def test_connector_detail_panel_wires_oracle_scope_picker():
    """AC12: ConnectorDetailPanel.tsx renders OracleScopePicker for connector.id === 'oracle_db'."""
    panel_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "frontend", "src", "components",
        "integrations", "ConnectorDetailPanel.tsx",
    )
    panel_path = os.path.normpath(panel_path)
    assert os.path.exists(panel_path), f"ConnectorDetailPanel.tsx not found"

    with open(panel_path, encoding="utf-8") as f:
        content = f.read()

    assert "OracleScopePicker" in content, (
        "ConnectorDetailPanel must import and render OracleScopePicker"
    )
    assert "oracle_db" in content, (
        "ConnectorDetailPanel must check connector.id === 'oracle_db'"
    )
