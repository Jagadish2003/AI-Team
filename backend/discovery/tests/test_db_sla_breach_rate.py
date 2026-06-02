"""
Tests for DB_SLA_BREACH_RATE detector — T2-S11-A

Signal shape follows Section 1d of T2-S11-A:
  - sla_breach.degraded_signal (not 'degraded')
  - schema_name and table_name are top-level keys in db_data, not inside sla_breach

Covers:
  - fires when breach_rate_pct >= 15.0 and total_tickets_30d >= 10
  - does not fire below threshold
  - does not fire when volume guard fails (total_tickets_30d < 10)
  - does not fire when signal is missing
  - does not fire when signal is marked degraded_signal=True
  - SIGNAL_METRICS defines the three required metrics
  - raw_evidence uses degraded_signal key and reads schema/table from top level
"""
from __future__ import annotations

import pytest


def _make_signal(
    breach_rate_pct: float = 20.0,
    breached_count: int = 25,
    total_tickets_30d: int = 125,
    schema_name: str = "dbo",
    table_name: str = "ServiceTickets",
    degraded_signal: bool = False,
) -> dict:
    """Build a db_data dict matching the T2-S11-A Section 1d ingestor return shape.

    schema_name and table_name are top-level; degraded_signal is inside sla_breach.
    """
    return {
        "sla_breach": {
            "breach_rate_pct": breach_rate_pct,
            "breached_count": breached_count,
            "total_tickets_30d": total_tickets_30d,
            "degraded_signal": degraded_signal,
        },
        "schema_name": schema_name,
        "table_name": table_name,
        "connector_id": "sqlserver",
    }


class TestDbSlaBreachRateDetector:

    def test_fires_above_threshold_with_sufficient_volume(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(breach_rate_pct=20.0, total_tickets_30d=125))
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "DB_SLA_BREACH_RATE"
        assert r.signal_source == "sqlserver"
        assert r.metric_value == 20.0
        assert r.threshold == 15.0

    def test_fires_at_exact_threshold(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(breach_rate_pct=15.0, total_tickets_30d=10))
        assert len(results) == 1

    def test_does_not_fire_below_threshold(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(breach_rate_pct=14.9, total_tickets_30d=100))
        assert results == []

    def test_does_not_fire_when_volume_guard_fails(self):
        from discovery.detectors.db_sla_breach_rate import detect
        # breach_rate is high but total_tickets_30d < 10 (volume guard)
        results = detect(_make_signal(breach_rate_pct=80.0, total_tickets_30d=9))
        assert results == []

    def test_does_not_fire_at_minimum_volume_minus_one(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(breach_rate_pct=50.0, total_tickets_30d=9))
        assert results == []

    def test_does_not_fire_when_signal_missing(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect({})
        assert results == []

    def test_does_not_fire_when_sla_breach_key_absent(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect({"other_signal": {"foo": 1}, "schema_name": "dbo", "table_name": "T"})
        assert results == []

    def test_does_not_fire_when_degraded_signal_true(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(breach_rate_pct=99.0, total_tickets_30d=500, degraded_signal=True))
        assert results == []

    def test_raw_evidence_contains_required_fields(self):
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(
            breach_rate_pct=25.0,
            breached_count=50,
            total_tickets_30d=200,
            schema_name="ops",
            table_name="Tickets",
        ))
        assert len(results) == 1
        ev = results[0].raw_evidence
        assert ev["breach_rate_pct"] == 25.0
        assert ev["breached_count"] == 50
        assert ev["total_tickets_30d"] == 200
        assert ev["schema_name"] == "ops"
        assert ev["table_name"] == "Tickets"
        assert ev["degraded_signal"] is False

    def test_schema_table_read_from_top_level_db_data(self):
        """schema_name and table_name come from top-level db_data, not sla_breach."""
        from discovery.detectors.db_sla_breach_rate import detect
        results = detect(_make_signal(schema_name="finance", table_name="Incidents"))
        assert len(results) == 1
        ev = results[0].raw_evidence
        assert ev["schema_name"] == "finance"
        assert ev["table_name"] == "Incidents"

    def test_signal_metrics_defined(self):
        from discovery.detectors.db_sla_breach_rate import SIGNAL_METRICS
        assert "breach_rate_pct" in SIGNAL_METRICS
        assert "breached_count" in SIGNAL_METRICS
        assert "total_tickets_30d" in SIGNAL_METRICS

    def test_evaluate_returns_unfired_for_missing_signal(self):
        from discovery.detectors.db_sla_breach_rate import evaluate
        ev = evaluate({})
        assert ev.fired is False
        assert ev.detector_id == "DB_SLA_BREACH_RATE"

    def test_evaluate_returns_unfired_for_degraded_signal(self):
        from discovery.detectors.db_sla_breach_rate import evaluate
        ev = evaluate(_make_signal(breach_rate_pct=99.0, total_tickets_30d=500, degraded_signal=True))
        assert ev.fired is False
        assert ev.raw_evidence["degraded_signal"] is True

    def test_evaluate_fired_true_above_threshold(self):
        from discovery.detectors.db_sla_breach_rate import evaluate
        ev = evaluate(_make_signal(breach_rate_pct=30.0, total_tickets_30d=50))
        assert ev.fired is True
        assert ev.metric_value == 30.0
