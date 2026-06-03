"""
Contract tests for DB_QUEUE_DEPTH_ELEVATED detector — T2-S11-A Task T4.

Covers all acceptance criteria that belong to this detector:
  AC7  — fires at p1_p2_open >= 20; by_priority breakdown in raw_evidence
  AC8  — SIGNAL_METRICS defined: p1_p2_open, total_open, oldest_ticket_hours
  AC11 — end-to-end: mocked ingestor data produces DetectorResult with
         detector_id == DB_QUEUE_DEPTH_ELEVATED
  AC18 — returns [] when db_data is None, empty, or degraded_signal is True
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _get_detector():
    try:
        import backend.discovery.detectors.db_queue_depth_elevated as m
    except ModuleNotFoundError:
        import discovery.detectors.db_queue_depth_elevated as m
    return m


# ---------------------------------------------------------------------------
# Shared test data builders
# ---------------------------------------------------------------------------

def _queue_data(
    p1_p2_open: int = 25,
    total_open: int = 80,
    oldest_ticket_hours: float = 96.0,
    by_priority: dict | None = None,
    degraded_signal: bool = False,
) -> dict:
    return {
        "queue_depth": {
            "p1_p2_open": p1_p2_open,
            "total_open": total_open,
            "oldest_ticket_hours": oldest_ticket_hours,
            "by_priority": by_priority if by_priority is not None else {
                "P1": {"count": 10, "avg_age_hours": 72.0},
                "P2": {"count": 15, "avg_age_hours": 48.0},
            },
            "degraded_signal": degraded_signal,
        },
        "schema_name": "dbo",
        "table_name": "ServiceTickets",
    }


# ---------------------------------------------------------------------------
# AC7 — fires at p1_p2_open >= 20; by_priority in raw_evidence
# ---------------------------------------------------------------------------

class TestAC7:

    def test_fires_at_exact_threshold(self):
        """Fires when p1_p2_open equals the threshold (20)."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=20))
        assert len(results) == 1
        assert results[0].detector_id == "DB_QUEUE_DEPTH_ELEVATED"

    def test_fires_above_threshold(self):
        """Fires when p1_p2_open is clearly above threshold."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=50))
        assert len(results) == 1

    def test_does_not_fire_below_threshold(self):
        """Does not fire when p1_p2_open is 19 (one below threshold)."""
        m = _get_detector()
        assert m.detect(_queue_data(p1_p2_open=19)) == []

    def test_does_not_fire_at_zero(self):
        m = _get_detector()
        assert m.detect(_queue_data(p1_p2_open=0)) == []

    def test_by_priority_in_raw_evidence(self):
        """by_priority breakdown must appear in raw_evidence."""
        m = _get_detector()
        by_priority = {
            "P1": {"count": 12, "avg_age_hours": 100.0},
            "P2": {"count": 13, "avg_age_hours": 50.0},
        }
        results = m.detect(_queue_data(p1_p2_open=25, by_priority=by_priority))
        assert len(results) == 1
        ev = results[0].raw_evidence
        assert "by_priority" in ev
        assert ev["by_priority"] == by_priority

    def test_raw_evidence_contains_all_required_keys(self):
        """raw_evidence must include p1_p2_open, total_open, oldest_ticket_hours,
        by_priority, schema_name, table_name, degraded_signal."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=30))
        ev = results[0].raw_evidence
        required = {
            "p1_p2_open", "total_open", "oldest_ticket_hours",
            "by_priority", "schema_name", "table_name", "degraded_signal",
        }
        assert required.issubset(ev.keys())

    def test_metric_value_equals_p1_p2_open(self):
        """metric_value on DetectorResult must equal p1_p2_open."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=34))
        assert results[0].metric_value == 34.0

    def test_threshold_on_result_is_20(self):
        """threshold on DetectorResult must be 20."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25))
        assert results[0].threshold == 20.0

    def test_signal_source_is_sqlserver(self):
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25))
        assert results[0].signal_source == "sqlserver"

    def test_by_priority_empty_dict_still_fires(self):
        """Fires even when by_priority is empty — no tables in scope yet."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25, by_priority={}))
        assert len(results) == 1
        assert results[0].raw_evidence["by_priority"] == {}

    def test_oldest_ticket_hours_in_raw_evidence(self):
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25, oldest_ticket_hours=144.0))
        assert results[0].raw_evidence["oldest_ticket_hours"] == 144.0

    def test_total_open_in_raw_evidence(self):
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25, total_open=200))
        assert results[0].raw_evidence["total_open"] == 200

    def test_schema_and_table_in_raw_evidence(self):
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25))
        ev = results[0].raw_evidence
        assert ev["schema_name"] == "dbo"
        assert ev["table_name"] == "ServiceTickets"


# ---------------------------------------------------------------------------
# AC8 — SIGNAL_METRICS defined with required metrics, <= 8 entries
# ---------------------------------------------------------------------------

class TestAC8:

    def test_signal_metrics_is_defined(self):
        m = _get_detector()
        assert hasattr(m, "SIGNAL_METRICS")

    def test_signal_metrics_is_list(self):
        m = _get_detector()
        assert isinstance(m.SIGNAL_METRICS, list)

    def test_signal_metrics_contains_required_entries(self):
        m = _get_detector()
        required = {"p1_p2_open", "total_open", "oldest_ticket_hours"}
        assert required.issubset(set(m.SIGNAL_METRICS))

    def test_signal_metrics_does_not_exceed_8_entries(self):
        m = _get_detector()
        assert len(m.SIGNAL_METRICS) <= 8

    def test_signal_metrics_entries_match_raw_evidence_keys(self):
        """Every metric in SIGNAL_METRICS must appear as a key in raw_evidence."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25))
        ev = results[0].raw_evidence
        for metric in m.SIGNAL_METRICS:
            assert metric in ev, f"SIGNAL_METRICS entry '{metric}' missing from raw_evidence"

    def test_signal_metrics_values_are_numeric_in_raw_evidence(self):
        """SIGNAL_METRICS entries must map to numeric values in raw_evidence."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25))
        ev = results[0].raw_evidence
        for metric in m.SIGNAL_METRICS:
            assert isinstance(ev[metric], (int, float)), (
                f"raw_evidence['{metric}'] must be numeric, got {type(ev[metric])}"
            )

    def test_detector_id_constant(self):
        m = _get_detector()
        assert m.DETECTOR_ID == "DB_QUEUE_DEPTH_ELEVATED"

    def test_threshold_constant(self):
        m = _get_detector()
        assert m.P1_P2_THRESHOLD == 20


# ---------------------------------------------------------------------------
# AC11 — end-to-end with mocked ingestor shape produces correct DetectorResult
# ---------------------------------------------------------------------------

class TestAC11:

    def test_end_to_end_with_ingestor_output_shape(self):
        """Simulates the full ingestor return shape and verifies detector fires."""
        m = _get_detector()

        # Matches the Section 1d return shape from the spec
        ingestor_output = {
            "ticket_volume": {
                "daily_counts": [], "total_90d": 1500,
                "avg_daily": 16.7, "peak_daily": 40,
                "peak_date": "2026-05-15", "recent_7d_avg": 25.0,
                "recent_vs_baseline": 1.5, "degraded_signal": False,
            },
            "sla_breach": {
                "total_tickets_30d": 200, "breached_count": 30,
                "breach_rate_pct": 15.0, "degraded_signal": False,
            },
            "queue_depth": {
                "by_priority": {
                    "P1": {"count": 12, "avg_age_hours": 72.0},
                    "P2": {"count": 18, "avg_age_hours": 36.0},
                    "P3": {"count": 50, "avg_age_hours": 12.0},
                },
                "total_open": 80,
                "p1_p2_open": 30,
                "oldest_ticket_hours": 72.0,
                "degraded_signal": False,
            },
            "connector_id": "sqlserver",
            "org_id": "test-org",
            "run_id": "run-e2e-001",
            "schema_name": "dbo",
            "table_name": "ServiceTickets",
        }

        results = m.detect(ingestor_output)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "DB_QUEUE_DEPTH_ELEVATED"
        assert r.signal_source == "sqlserver"
        assert r.metric_value == 30.0
        assert r.threshold == 20.0

    def test_does_not_fire_when_p1_p2_below_threshold_in_full_shape(self):
        """Does not fire when p1_p2_open < 20, even with full ingestor output."""
        m = _get_detector()

        ingestor_output = {
            "queue_depth": {
                "by_priority": {"P1": {"count": 3, "avg_age_hours": 10.0},
                                 "P2": {"count": 5, "avg_age_hours": 5.0}},
                "total_open": 20,
                "p1_p2_open": 8,
                "oldest_ticket_hours": 10.0,
                "degraded_signal": False,
            },
            "schema_name": "dbo",
            "table_name": "ServiceTickets",
        }

        assert m.detect(ingestor_output) == []


# ---------------------------------------------------------------------------
# AC18 — returns [] when db_data is None, empty, or degraded_signal is True
# ---------------------------------------------------------------------------

class TestAC18:

    def test_returns_empty_for_none(self):
        m = _get_detector()
        assert m.detect(None) == []

    def test_returns_empty_for_empty_dict(self):
        m = _get_detector()
        assert m.detect({}) == []

    def test_returns_empty_when_queue_depth_key_missing(self):
        m = _get_detector()
        assert m.detect({"ticket_volume": {}, "sla_breach": {}}) == []

    def test_returns_empty_when_degraded_signal_true(self):
        m = _get_detector()
        data = _queue_data(p1_p2_open=100, degraded_signal=True)
        assert m.detect(data) == []

    def test_degraded_overrides_high_p1_p2(self):
        """Even 1000 P1/P2 tickets must not fire when degraded."""
        m = _get_detector()
        data = _queue_data(p1_p2_open=1000, degraded_signal=True)
        assert m.detect(data) == []

    def test_returns_empty_for_none_sn_jira(self):
        """sn_data and jira_data being None must not affect firing."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25), sn_data=None, jira_data=None)
        assert len(results) == 1

    def test_evaluate_returns_not_fired_for_none(self):
        """evaluate() must handle None gracefully without raising."""
        m = _get_detector()
        ev = m.evaluate(None)
        assert ev.fired is False

    def test_degraded_false_in_evidence_when_not_degraded(self):
        """degraded_signal in raw_evidence must be False for a clean result."""
        m = _get_detector()
        results = m.detect(_queue_data(p1_p2_open=25, degraded_signal=False))
        assert results[0].raw_evidence["degraded_signal"] is False

    def test_degraded_true_in_evidence_when_degraded(self):
        """Even when not fired, evaluate() should preserve degraded_signal=True."""
        m = _get_detector()
        ev = m.evaluate(_queue_data(p1_p2_open=25, degraded_signal=True))
        assert ev.fired is False
        assert ev.raw_evidence["degraded_signal"] is True
