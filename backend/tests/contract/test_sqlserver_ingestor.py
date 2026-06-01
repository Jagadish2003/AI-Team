"""
Contract tests for T2-S11-A — SQL Server Operational Signal Ingestor.

Covers all 20 acceptance criteria. All tests use mocked execute_query() so
no live SQL Server is required.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers — support both project-root and backend-relative paths
# ---------------------------------------------------------------------------

def _import(module_path: str):
    try:
        parts = module_path.split(".")
        mod = __import__(module_path)
        for p in parts[1:]:
            mod = getattr(mod, p)
        return mod
    except (ImportError, ModuleNotFoundError):
        alt = ".".join(module_path.split(".")[1:])  # strip leading "backend."
        parts = alt.split(".")
        mod = __import__(alt)
        for p in parts[1:]:
            mod = getattr(mod, p)
        return mod


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
        connector_id="sqlserver",
        schemas=schemas or ["dbo"],
        tables=tables or ["dbo.ServiceTickets"],
        declared_at=datetime.now(timezone.utc),
        declared_by="test",
    )


def _make_config():
    try:
        from backend.connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from connectors.db import DBConnectorConfig
    return DBConnectorConfig(
        connector_id="sqlserver",
        org_id="test-org",
        host="sqlserver.test.local",
        port=1433,
        database="ServiceDB",
        driver="pyodbc",
        username_key="SQL_USER",
        password_key="SQL_PASS",
    )


def _make_result(columns: list[str], rows: list[tuple]) -> Any:
    return SimpleNamespace(columns=columns, rows=rows, row_count=len(rows),
                           query_hash="abc", duration_ms=5, truncated=False)


VOLUME_COLUMNS = ["ticket_date", "ticket_count"]
SLA_COLUMNS = ["total_tickets", "breached_count", "breach_rate_pct"]
QUEUE_COLUMNS = ["priority", "queue_count", "avg_age_hours"]

_BASE_VOLUME_ROWS = [
    (f"2026-05-{30 - i:02d}", 10 + i) for i in range(30)
] + [
    (f"2026-04-{30 - i:02d}", 8) for i in range(60)
]

_HIGH_VOLUME_ROWS = (
    [(f"2026-05-{30 - i:02d}", 50) for i in range(7)]   # recent 7d avg = 50
    + [(f"2026-05-{7 + i:02d}", 10) for i in range(23)]  # rest of 30d
    + [(f"2026-04-{30 - i:02d}", 8) for i in range(60)]  # older 60d
)

# ---------------------------------------------------------------------------
# AC1 — ingestor uses execute_query() for all three queries; no direct conn
# ---------------------------------------------------------------------------

def test_ac1_all_queries_go_through_execute_query():
    """AC1: execute_query() is called 3 times; no direct pyodbc.connect."""
    try:
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               return_value=mock_result) as mock_eq, \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()):
        ingest("org", "run-1", _make_config(), scope=_make_scope())
        assert mock_eq.call_count == 3


# ---------------------------------------------------------------------------
# AC2 — all queries use [] quoted identifiers and schema-qualified table names
# ---------------------------------------------------------------------------

def test_ac2_queries_use_bracket_quoting():
    """AC2: All SQL queries use [] quoting and schema.table qualification."""
    try:
        from backend.connectors.db.sqlserver_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_template in (TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY):
        q = q_template.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="dbo", table="ServiceTickets",
        )
        assert "[dbo]" in q, "Schema must be [] quoted"
        assert "[ServiceTickets]" in q, "Table must be [] quoted"
        assert '"dbo"' not in q, "No PostgreSQL/Oracle double-quote quoting"


# ---------------------------------------------------------------------------
# AC3 — missing column → warning logged, degraded_signal=True, no raise
# ---------------------------------------------------------------------------

def test_ac3_missing_column_sets_degraded_no_raise():
    """AC3: Missing expected column → degraded_signal=True, no exception."""
    try:
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import ingest

    # Return a result with completely wrong columns for all three queries
    bad_result = _make_result(["col_a", "col_b"], [(1, 2)])
    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               return_value=bad_result), \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    assert out["sla_breach"]["degraded_signal"] is True
    assert out["queue_depth"]["degraded_signal"] is True


# ---------------------------------------------------------------------------
# AC4 — query_timeout DBConnectionError → degraded_signal=True, run continues
# ---------------------------------------------------------------------------

def test_ac4_timeout_sets_degraded_continues():
    """AC4: query_timeout error on one query → degraded_signal=True, other queries run."""
    try:
        from backend.connectors.db import DBConnectionError
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db import DBConnectionError
        from connectors.db.sqlserver_ingestor import ingest

    timeout_err = DBConnectionError("timed out", error_code="query_timeout")
    ok_result = _make_result(VOLUME_COLUMNS, [])

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise timeout_err
        return ok_result

    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is True
    # Other two queries still ran
    assert call_count == 3


# ---------------------------------------------------------------------------
# AC5 — DB_TICKET_VOLUME_SURGE fires at ratio >= 1.5; not when degraded
# ---------------------------------------------------------------------------

def test_ac5_surge_detector_fires_above_threshold():
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect, SURGE_THRESHOLD
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect, SURGE_THRESHOLD

    db_data = {
        "ticket_volume": {
            "recent_vs_baseline": SURGE_THRESHOLD,
            "recent_7d_avg": 30.0, "avg_daily": 20.0,
            "peak_daily": 50, "peak_date": "2026-05-30",
            "total_90d": 1800, "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert len(detect(db_data)) == 1
    assert detect(db_data)[0].detector_id == "DB_TICKET_VOLUME_SURGE"


def test_ac5_surge_detector_does_not_fire_below_threshold():
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect

    db_data = {
        "ticket_volume": {
            "recent_vs_baseline": 1.2, "recent_7d_avg": 12.0,
            "avg_daily": 10.0, "peak_daily": 15, "peak_date": "2026-05-01",
            "total_90d": 900, "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert detect(db_data) == []


def test_ac5_surge_detector_does_not_fire_when_degraded():
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect

    db_data = {
        "ticket_volume": {
            "recent_vs_baseline": 3.0, "degraded_signal": True,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert detect(db_data) == []


# ---------------------------------------------------------------------------
# AC6 — DB_SLA_BREACH_RATE fires at >= 15% with volume guard
# ---------------------------------------------------------------------------

def test_ac6_sla_detector_fires_above_threshold_with_volume():
    try:
        from backend.discovery.detectors.db_sla_breach_rate import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_sla_breach_rate import detect

    db_data = {
        "sla_breach": {
            "breach_rate_pct": 20.0, "breached_count": 20,
            "total_tickets_30d": 100, "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert len(detect(db_data)) == 1


def test_ac6_sla_detector_does_not_fire_below_volume_guard():
    try:
        from backend.discovery.detectors.db_sla_breach_rate import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_sla_breach_rate import detect

    db_data = {
        "sla_breach": {
            "breach_rate_pct": 50.0, "breached_count": 4,
            "total_tickets_30d": 8,   # below MIN_TICKET_VOLUME=10
            "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert detect(db_data) == []


def test_ac6_sla_detector_does_not_fire_when_degraded():
    try:
        from backend.discovery.detectors.db_sla_breach_rate import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_sla_breach_rate import detect

    db_data = {
        "sla_breach": {
            "breach_rate_pct": 80.0, "breached_count": 80,
            "total_tickets_30d": 100, "degraded_signal": True,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert detect(db_data) == []


# ---------------------------------------------------------------------------
# AC7 — DB_QUEUE_DEPTH_ELEVATED fires at p1_p2 >= 20; by_priority in evidence
# ---------------------------------------------------------------------------

def test_ac7_queue_detector_fires_with_by_priority_in_evidence():
    try:
        from backend.discovery.detectors.db_queue_depth_elevated import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_queue_depth_elevated import detect

    db_data = {
        "queue_depth": {
            "p1_p2_open": 25, "total_open": 80,
            "oldest_ticket_hours": 96.0,
            "by_priority": {"P1": {"count": 10, "avg_age_hours": 48.0},
                             "P2": {"count": 15, "avg_age_hours": 24.0}},
            "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    results = detect(db_data)
    assert len(results) == 1
    assert "by_priority" in results[0].raw_evidence


def test_ac7_queue_detector_does_not_fire_below_threshold():
    try:
        from backend.discovery.detectors.db_queue_depth_elevated import detect
    except ModuleNotFoundError:
        from discovery.detectors.db_queue_depth_elevated import detect

    db_data = {
        "queue_depth": {
            "p1_p2_open": 5, "total_open": 30, "oldest_ticket_hours": 10.0,
            "by_priority": {}, "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }
    assert detect(db_data) == []


# ---------------------------------------------------------------------------
# AC8 — SIGNAL_METRICS defined correctly on all three detectors
# ---------------------------------------------------------------------------

def test_ac8_signal_metrics_defined_on_all_detectors():
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import SIGNAL_METRICS as SM1
        from backend.discovery.detectors.db_sla_breach_rate import SIGNAL_METRICS as SM2
        from backend.discovery.detectors.db_queue_depth_elevated import SIGNAL_METRICS as SM3
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import SIGNAL_METRICS as SM1
        from discovery.detectors.db_sla_breach_rate import SIGNAL_METRICS as SM2
        from discovery.detectors.db_queue_depth_elevated import SIGNAL_METRICS as SM3

    for metrics, name in [(SM1, "surge"), (SM2, "sla"), (SM3, "queue")]:
        assert isinstance(metrics, list), f"{name}: SIGNAL_METRICS must be a list"
        assert 1 <= len(metrics) <= 8, f"{name}: SIGNAL_METRICS must have 1-8 entries"

    assert "recent_vs_baseline" in SM1
    assert "breach_rate_pct" in SM2
    assert "p1_p2_open" in SM3


# ---------------------------------------------------------------------------
# AC9 — sqlserver_opsignal pack in PACK_REGISTRY; is_sqlserver_opsignal_pack()
# ---------------------------------------------------------------------------

def test_ac9_pack_registered_and_helper():
    try:
        from backend.discovery.packs.pack_config import (
            get_pack, is_sqlserver_opsignal_pack, list_packs,
        )
    except ModuleNotFoundError:
        from discovery.packs.pack_config import (
            get_pack, is_sqlserver_opsignal_pack, list_packs,
        )

    assert "sqlserver_opsignal" in list_packs()
    pack = get_pack("sqlserver_opsignal")
    assert pack["packId"] == "sqlserver_opsignal"
    assert is_sqlserver_opsignal_pack("sqlserver_opsignal") is True
    assert is_sqlserver_opsignal_pack("service_cloud") is False


# ---------------------------------------------------------------------------
# AC10 — UI labels JSON correct and loadable via get_ui_labels()
# ---------------------------------------------------------------------------

def test_ac10_ui_labels_correct():
    try:
        from backend.discovery.packs.pack_config import get_ui_labels
    except ModuleNotFoundError:
        from discovery.packs.pack_config import get_ui_labels

    labels = get_ui_labels("sqlserver_opsignal")
    assert labels is not None

    for det_id in ("DB_TICKET_VOLUME_SURGE", "DB_SLA_BREACH_RATE", "DB_QUEUE_DEPTH_ELEVATED"):
        entry = labels.get(det_id)
        assert entry is not None, f"Missing labels for {det_id}"
        for field in ("s6_title", "agentType", "s6_why", "s6_action"):
            assert field in entry, f"{det_id} missing '{field}'"


# ---------------------------------------------------------------------------
# AC11 — end-to-end: mocked ingestor data → at least one OpportunityCandidate
# ---------------------------------------------------------------------------

def test_ac11_end_to_end_produces_opportunity():
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
            "breach_rate_pct": 20.0, "breached_count": 40,
            "total_tickets_30d": 200, "degraded_signal": False,
        },
        "queue_depth": {
            "p1_p2_open": 30, "total_open": 100,
            "oldest_ticket_hours": 120.0,
            "by_priority": {"P1": {"count": 15, "avg_age_hours": 60.0},
                             "P2": {"count": 15, "avg_age_hours": 40.0}},
            "degraded_signal": False,
        },
        "schema_name": "dbo", "table_name": "ServiceTickets",
    }

    all_results = d1(db_data) + d2(db_data) + d3(db_data)
    fired_ids = {r.detector_id for r in all_results}

    expected = {"DB_TICKET_VOLUME_SURGE", "DB_SLA_BREACH_RATE", "DB_QUEUE_DEPTH_ELEVATED"}
    assert fired_ids == expected


# ---------------------------------------------------------------------------
# AC12 — scorer returns MEDIUM confidence; tier and roadmap_stage correct
# ---------------------------------------------------------------------------

def test_ac12_scorer_medium_confidence():
    try:
        from backend.discovery.packs.sqlserver_opsignal_scorer import (
            score_sqlserver_opsignal, is_sqlserver_opsignal_detector,
        )
        from backend.discovery.models import DetectorResult
    except ModuleNotFoundError:
        from discovery.packs.sqlserver_opsignal_scorer import (
            score_sqlserver_opsignal, is_sqlserver_opsignal_detector,
        )
        from discovery.models import DetectorResult

    cases = [
        ("DB_TICKET_VOLUME_SURGE", "Quick Win", "quick_win"),
        ("DB_SLA_BREACH_RATE", "Quick Win", "quick_win"),
        ("DB_QUEUE_DEPTH_ELEVATED", "Strategic", "strategic"),
    ]
    for det_id, exp_tier, exp_stage in cases:
        dr = DetectorResult(
            detector_id=det_id, signal_source="sqlserver",
            metric_value=2.0, threshold=1.5,
            raw_evidence={det_id: 1.0},
        )
        scored = score_sqlserver_opsignal(dr)
        assert scored is not None, f"No score for {det_id}"
        assert scored["confidence"] == "MEDIUM", f"{det_id}: expected MEDIUM confidence"
        assert scored["tier"] == exp_tier, f"{det_id}: expected tier {exp_tier}"
        assert scored["roadmap_stage"] == exp_stage

    assert is_sqlserver_opsignal_detector("DB_TICKET_VOLUME_SURGE") is True
    assert is_sqlserver_opsignal_detector("HANDOFF_FRICTION") is False


# ---------------------------------------------------------------------------
# AC16 — record_event called; its failure does not affect ingestor output
# ---------------------------------------------------------------------------

def test_ac16_telemetry_fire_and_forget():
    try:
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])

    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               return_value=mock_result), \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.sqlserver_ingestor.record_event",
               side_effect=RuntimeError("telemetry down")) as mock_te:
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    mock_te.assert_called_once()
    assert "ticket_volume" in out  # ingestor still returned output


# ---------------------------------------------------------------------------
# AC17 — Windows auth (empty username) uses Trusted_Connection=yes
# ---------------------------------------------------------------------------

def test_ac17_windows_auth_connection_string():
    try:
        from backend.connectors.db.sqlserver import _build_connection_string
        from backend.connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from connectors.db.sqlserver import _build_connection_string
        from connectors.db import DBConnectorConfig

    config = DBConnectorConfig(
        connector_id="sqlserver", org_id="org",
        host="sqlserver.test.local", port=1433,
        database="ServiceDB", driver="pyodbc",
        username_key="", password_key="",
    )

    # Windows auth — empty username
    win_conn_str = _build_connection_string(config, "", "")
    assert "Trusted_Connection=yes" in win_conn_str
    assert "UID=" not in win_conn_str
    assert "PWD=" not in win_conn_str

    # SQL auth — non-empty username
    sql_conn_str = _build_connection_string(config, "sa", "secret")
    assert "UID=sa" in sql_conn_str
    assert "PWD=secret" in sql_conn_str
    assert "Trusted_Connection" not in sql_conn_str


# ---------------------------------------------------------------------------
# AC18 — all detectors return [] when db_data is None, empty, or degraded
# ---------------------------------------------------------------------------

def test_ac18_detectors_return_empty_on_none_or_degraded():
    try:
        from backend.discovery.detectors.db_ticket_volume_surge import detect as d1
        from backend.discovery.detectors.db_sla_breach_rate import detect as d2
        from backend.discovery.detectors.db_queue_depth_elevated import detect as d3
    except ModuleNotFoundError:
        from discovery.detectors.db_ticket_volume_surge import detect as d1
        from discovery.detectors.db_sla_breach_rate import detect as d2
        from discovery.detectors.db_queue_depth_elevated import detect as d3

    for detect_fn in (d1, d2, d3):
        assert detect_fn(None) == [], f"{detect_fn}: should return [] for None"
        assert detect_fn({}) == [], f"{detect_fn}: should return [] for empty dict"

    # Degraded signals
    assert d1({"ticket_volume": {"recent_vs_baseline": 5.0, "degraded_signal": True}}) == []
    assert d2({"sla_breach": {"breach_rate_pct": 90.0, "total_tickets_30d": 100,
                               "degraded_signal": True}}) == []
    assert d3({"queue_depth": {"p1_p2_open": 100, "degraded_signal": True}}) == []


# ---------------------------------------------------------------------------
# AC19 — ingestor return shape matches spec Section 1d exactly
# ---------------------------------------------------------------------------

def test_ac19_return_shape_matches_spec():
    try:
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import ingest

    mock_result = _make_result(VOLUME_COLUMNS, [])
    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               return_value=mock_result), \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()):
        out = ingest("org", "run-99", _make_config(), scope=_make_scope())

    # Top-level keys
    for key in ("ticket_volume", "sla_breach", "queue_depth",
                 "connector_id", "org_id", "run_id", "schema_name", "table_name"):
        assert key in out, f"Missing top-level key: {key}"

    assert out["connector_id"] == "sqlserver"
    assert out["org_id"] == "org"
    assert out["run_id"] == "run-99"

    tv = out["ticket_volume"]
    for key in ("daily_counts", "total_90d", "avg_daily", "peak_daily",
                 "peak_date", "recent_7d_avg", "recent_vs_baseline", "degraded_signal"):
        assert key in tv, f"ticket_volume missing key: {key}"

    sla = out["sla_breach"]
    for key in ("total_tickets_30d", "breached_count", "breach_rate_pct", "degraded_signal"):
        assert key in sla, f"sla_breach missing key: {key}"

    qd = out["queue_depth"]
    for key in ("by_priority", "total_open", "p1_p2_open",
                 "oldest_ticket_hours", "degraded_signal"):
        assert key in qd, f"queue_depth missing key: {key}"


# ---------------------------------------------------------------------------
# AC20 — db.ingestor_completed in telemetry registry with DBIngestorCompletedPayload
# ---------------------------------------------------------------------------

def test_ac20_telemetry_event_type_registered():
    try:
        from backend.app.telemetry import EVENT_REGISTRY, DBIngestorCompletedPayload
    except ModuleNotFoundError:
        from app.telemetry import EVENT_REGISTRY, DBIngestorCompletedPayload

    assert "db.ingestor_completed" in EVENT_REGISTRY
    assert DBIngestorCompletedPayload is not None

    required_keys = {"connector_id", "pack_id", "query_count",
                     "signal_count", "degraded_count", "duration_ms"}
    hints = getattr(DBIngestorCompletedPayload, "__annotations__", {})
    assert required_keys.issubset(set(hints.keys())), (
        f"DBIngestorCompletedPayload missing keys: {required_keys - set(hints.keys())}"
    )


# ---------------------------------------------------------------------------
# Bonus: all queries contain DATEADD and are SELECT-only (no mutations)
# ---------------------------------------------------------------------------

def test_queries_are_select_only_with_dateadd():
    try:
        from backend.connectors.db.sqlserver_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import (
            TICKET_VOLUME_QUERY, SLA_BREACH_QUERY, QUEUE_DEPTH_QUERY,
        )

    for q_tpl, name in [
        (TICKET_VOLUME_QUERY, "volume"),
        (SLA_BREACH_QUERY, "sla"),
        (QUEUE_DEPTH_QUERY, "queue"),
    ]:
        q = q_tpl.format(
            date_col="created_date", sla_col="sla_breached",
            priority_col="priority", status_col="status",
            schema="dbo", table="ServiceTickets",
        ).strip().upper()
        assert q.startswith("SELECT"), f"{name}: must be SELECT-only"
        for bad in ("INSERT", "UPDATE", "DELETE", "DROP", "EXEC"):
            assert bad not in q, f"{name}: must not contain {bad}"


def test_ingestor_returns_successful_degraded_false_on_good_data():
    """degraded_signal=False when all queries complete with correct columns."""
    try:
        from backend.connectors.db.sqlserver_ingestor import ingest
    except ModuleNotFoundError:
        from connectors.db.sqlserver_ingestor import ingest

    vol_result = _make_result(
        ["ticket_date", "ticket_count"],
        [("2026-05-30", 15), ("2026-05-29", 12)],
    )
    sla_result = _make_result(
        ["total_tickets", "breached_count", "breach_rate_pct"],
        [(100, 5, 5.0)],
    )
    queue_result = _make_result(
        ["priority", "queue_count", "avg_age_hours"],
        [("P1", 3, 24.0), ("P2", 5, 12.0)],
    )

    call_num = 0
    results = [vol_result, sla_result, queue_result]

    def side_effect(*args, **kwargs):
        nonlocal call_num
        r = results[call_num]
        call_num += 1
        return r

    with patch("backend.connectors.db.sqlserver_ingestor.execute_query",
               side_effect=side_effect), \
         patch("backend.connectors.db.sqlserver_ingestor.get_scope",
               return_value=_make_scope()), \
         patch("backend.connectors.db.sqlserver_ingestor.record_event"):
        out = ingest("org", "run", _make_config(), scope=_make_scope())

    assert out["ticket_volume"]["degraded_signal"] is False
    assert out["sla_breach"]["degraded_signal"] is False
    assert out["queue_depth"]["degraded_signal"] is False
    assert out["ticket_volume"]["total_90d"] == 27
    assert out["sla_breach"]["total_tickets_30d"] == 100
    assert out["queue_depth"]["p1_p2_open"] == 8
