"""
T2-S11-A  |  DB_TICKET_VOLUME_SURGE Detector — Contract Tests
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Covers acceptance criteria for the DB_TICKET_VOLUME_SURGE detector (Task T2)
and related pack infrastructure (Tasks T5, T6, T7).

AC coverage map
---------------
AC5   TestDetectorFiring  — fires at ratio >= 1.5, not below, not when degraded
AC8   TestSignalMetrics   — SIGNAL_METRICS defined, all numeric, <= 8 entries
AC9   TestPackRegistration — sqlserver_opsignal in PACK_REGISTRY
AC10  TestUILabels        — ui labels loaded, required keys present
AC12  TestScorer          — MEDIUM confidence, correct tier/roadmap_stage
AC18  TestGuardClauses    — empty list on None, empty dict, degraded=True

Run:
  cd backend
  pytest tests/contract/test_db_ticket_volume_surge.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from discovery.detectors.db_ticket_volume_surge import (
    DETECTOR_ID,
    SIGNAL_METRICS,
    SURGE_THRESHOLD,
    detect,
    evaluate,
)
from discovery.models import DetectorResult
from discovery.packs.pack_config import (
    get_pack,
    get_ui_labels,
    is_sqlserver_opsignal_pack,
    list_packs,
)
from discovery.packs.sqlserver_opsignal_scorer import (
    get_score,
    is_sqlserver_opsignal_detector,
    score_opportunity,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tv(
    recent_vs_baseline: float = 2.0,
    recent_7d_avg: float = 140.0,
    avg_daily: float = 70.0,
    peak_daily: int = 210,
    peak_date: str = "2025-06-01",
    total_90d: int = 6300,
    degraded_signal: bool = False,
) -> Dict[str, Any]:
    """Build a ticket_volume section with sensible defaults."""
    return {
        "recent_vs_baseline": recent_vs_baseline,
        "recent_7d_avg": recent_7d_avg,
        "avg_daily": avg_daily,
        "peak_daily": peak_daily,
        "peak_date": peak_date,
        "total_90d": total_90d,
        "degraded_signal": degraded_signal,
    }


def _db_data(tv: Dict[str, Any] | None = None, **kwargs) -> Dict[str, Any]:
    """Build a minimal ingestor return dict."""
    return {
        "ticket_volume": tv if tv is not None else _tv(**kwargs),
        "connector_id": "sqlserver",
        "org_id": "org-test",
        "run_id": "run-001",
        "schema_name": "dbo",
        "table_name": "incidents",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Constant verification
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_detector_id(self):
        assert DETECTOR_ID == "DB_TICKET_VOLUME_SURGE"

    def test_surge_threshold(self):
        assert SURGE_THRESHOLD == 1.5

    def test_signal_source_is_sqlserver(self):
        result = detect(_db_data())
        assert result[0].signal_source == "sqlserver"


# ─────────────────────────────────────────────────────────────────────────────
# AC5  |  Firing conditions
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectorFiring:
    """AC5 — fires at ratio >= 1.5 when not degraded; does not fire otherwise."""

    def test_fires_when_ratio_exactly_at_threshold(self):
        """AC5 — fires when recent_vs_baseline == 1.5 (inclusive threshold)."""
        results = detect(_db_data(recent_vs_baseline=1.5))
        assert len(results) == 1
        assert results[0].detector_id == DETECTOR_ID

    def test_fires_when_ratio_above_threshold(self):
        results = detect(_db_data(recent_vs_baseline=2.3))
        assert len(results) == 1

    def test_fires_when_ratio_well_above_threshold(self):
        results = detect(_db_data(recent_vs_baseline=5.0))
        assert len(results) == 1

    def test_does_not_fire_when_ratio_below_threshold(self):
        """AC5 — ratio < 1.5 → no opportunity."""
        results = detect(_db_data(recent_vs_baseline=1.49))
        assert results == []

    def test_does_not_fire_when_ratio_is_zero(self):
        results = detect(_db_data(recent_vs_baseline=0.0))
        assert results == []

    def test_does_not_fire_when_ratio_is_one(self):
        """ratio = 1.0 means no change vs baseline — should not fire."""
        results = detect(_db_data(recent_vs_baseline=1.0))
        assert results == []

    def test_does_not_fire_when_degraded_signal_true(self):
        """AC5 — degraded_signal=True suppresses firing even above threshold."""
        results = detect(_db_data(
            tv=_tv(recent_vs_baseline=3.0, degraded_signal=True)
        ))
        assert results == []

    def test_fired_result_is_detector_result_instance(self):
        results = detect(_db_data(recent_vs_baseline=2.0))
        assert len(results) == 1
        assert isinstance(results[0], DetectorResult)

    def test_metric_value_equals_ratio(self):
        """metric_value in result must be the recent_vs_baseline ratio."""
        results = detect(_db_data(recent_vs_baseline=2.3))
        assert abs(results[0].metric_value - 2.3) < 0.001

    def test_threshold_in_result_equals_surge_threshold(self):
        results = detect(_db_data(recent_vs_baseline=2.0))
        assert results[0].threshold == SURGE_THRESHOLD

    def test_signal_source_is_sqlserver(self):
        """Doc spec: signal_source='sqlserver' — data comes from SQL Server DB."""
        results = detect(_db_data(recent_vs_baseline=2.0))
        assert results[0].signal_source == "sqlserver"

    def test_detect_never_raises_on_valid_input(self):
        """detect() must return a list, never raise."""
        result = detect(_db_data(recent_vs_baseline=2.0))
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# AC18  |  Guard clauses — empty / None / degraded
# ─────────────────────────────────────────────────────────────────────────────

class TestGuardClauses:
    """AC18 — empty list on None, missing section, degraded=True."""

    def test_returns_empty_list_when_db_data_is_none(self):
        assert detect(None) == []

    def test_returns_empty_list_when_db_data_is_empty_dict(self):
        assert detect({}) == []

    def test_returns_empty_list_when_ticket_volume_missing(self):
        assert detect({"connector_id": "sqlserver"}) == []

    def test_returns_empty_list_when_ticket_volume_is_none(self):
        assert detect({"ticket_volume": None}) == []

    def test_returns_empty_list_when_ticket_volume_is_empty(self):
        assert detect({"ticket_volume": {}}) == []

    def test_returns_empty_list_when_degraded_regardless_of_ratio(self):
        """AC18 — degraded_signal=True always suppresses the detector."""
        for ratio in [1.5, 2.0, 10.0]:
            result = detect(_db_data(tv=_tv(recent_vs_baseline=ratio, degraded_signal=True)))
            assert result == [], f"Expected [] for ratio={ratio} with degraded=True"

    def test_detect_returns_list_not_none(self):
        """detect() contract: always return a list, never None."""
        assert detect(None) is not None
        assert isinstance(detect(None), list)


# ─────────────────────────────────────────────────────────────────────────────
# AC8  |  SIGNAL_METRICS
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalMetrics:
    """AC8 — SIGNAL_METRICS defined; all keys numeric in raw_evidence; <= 8 entries."""

    def test_signal_metrics_is_defined(self):
        assert SIGNAL_METRICS is not None
        assert isinstance(SIGNAL_METRICS, list)

    def test_signal_metrics_not_empty(self):
        assert len(SIGNAL_METRICS) > 0

    def test_signal_metrics_does_not_exceed_8_entries(self):
        """Track 3 constraint: snapshot storage caps at 8 metrics per detector."""
        assert len(SIGNAL_METRICS) <= 8

    def test_signal_metrics_contains_required_keys(self):
        required = {"recent_vs_baseline", "recent_7d_avg", "avg_daily_90d", "peak_daily", "total_90d"}
        assert required.issubset(set(SIGNAL_METRICS))

    def test_all_signal_metrics_are_numeric_in_raw_evidence(self):
        """AC8 — every metric in SIGNAL_METRICS must appear as a number in raw_evidence."""
        results = detect(_db_data(recent_vs_baseline=2.0))
        assert len(results) == 1
        evidence = results[0].raw_evidence
        for metric in SIGNAL_METRICS:
            assert metric in evidence, f"SIGNAL_METRICS key '{metric}' missing from raw_evidence"
            assert isinstance(evidence[metric], (int, float)), (
                f"SIGNAL_METRICS key '{metric}' must be numeric; got {type(evidence[metric])}"
            )

    def test_recent_vs_baseline_in_signal_metrics(self):
        assert "recent_vs_baseline" in SIGNAL_METRICS

    def test_signal_metrics_are_strings(self):
        assert all(isinstance(m, str) for m in SIGNAL_METRICS)


# ─────────────────────────────────────────────────────────────────────────────
# Raw evidence content
# ─────────────────────────────────────────────────────────────────────────────

class TestRawEvidence:
    """Doc spec: raw_evidence must contain all specified keys."""

    REQUIRED_EVIDENCE_KEYS = {
        "recent_vs_baseline",
        "recent_7d_avg",
        "avg_daily_90d",
        "peak_daily",
        "total_90d",
        "peak_date",
        "schema_name",
        "table_name",
        "degraded_signal",
    }

    def test_raw_evidence_contains_all_required_keys(self):
        results = detect(_db_data(recent_vs_baseline=2.0))
        evidence = results[0].raw_evidence
        for key in self.REQUIRED_EVIDENCE_KEYS:
            assert key in evidence, f"raw_evidence missing key: {key}"

    def test_raw_evidence_recent_vs_baseline_matches_input(self):
        results = detect(_db_data(recent_vs_baseline=1.8))
        assert abs(results[0].raw_evidence["recent_vs_baseline"] - 1.8) < 0.001

    def test_raw_evidence_schema_and_table_captured(self):
        data = _db_data(recent_vs_baseline=2.0)
        data["schema_name"] = "itsm"
        data["table_name"] = "incidents"
        results = detect(data)
        assert results[0].raw_evidence["schema_name"] == "itsm"
        assert results[0].raw_evidence["table_name"] == "incidents"

    def test_raw_evidence_degraded_signal_is_false_when_clean(self):
        results = detect(_db_data(recent_vs_baseline=2.0))
        assert results[0].raw_evidence["degraded_signal"] is False

    def test_raw_evidence_peak_date_captured(self):
        data = _db_data(tv=_tv(recent_vs_baseline=2.0, peak_date="2025-05-15"))
        results = detect(data)
        assert results[0].raw_evidence["peak_date"] == "2025-05-15"

    def test_raw_evidence_total_90d_captured(self):
        data = _db_data(tv=_tv(recent_vs_baseline=2.0, total_90d=9500))
        results = detect(data)
        assert results[0].raw_evidence["total_90d"] == 9500

    def test_raw_evidence_avg_daily_90d_matches_avg_daily(self):
        """avg_daily_90d in evidence comes from tv['avg_daily']."""
        data = _db_data(tv=_tv(recent_vs_baseline=2.0, avg_daily=55.5))
        results = detect(data)
        assert abs(results[0].raw_evidence["avg_daily_90d"] - 55.5) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# evaluate() — temporal option A
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluate:
    """evaluate() always returns; fired flag determines opportunity creation."""

    def test_evaluate_returns_non_none(self):
        ev = evaluate(_db_data(recent_vs_baseline=2.0))
        assert ev is not None

    def test_evaluate_fired_true_above_threshold(self):
        ev = evaluate(_db_data(recent_vs_baseline=2.0))
        assert ev.fired is True

    def test_evaluate_fired_false_below_threshold(self):
        ev = evaluate(_db_data(recent_vs_baseline=1.0))
        assert ev.fired is False

    def test_evaluate_fired_false_when_degraded(self):
        ev = evaluate(_db_data(tv=_tv(recent_vs_baseline=3.0, degraded_signal=True)))
        assert ev.fired is False

    def test_evaluate_fired_false_on_none_data(self):
        ev = evaluate(None)
        assert ev.fired is False

    def test_evaluate_detector_id_matches(self):
        ev = evaluate(_db_data(recent_vs_baseline=2.0))
        assert ev.detector_id == DETECTOR_ID

    def test_evaluate_signal_source_is_sqlserver(self):
        ev = evaluate(_db_data(recent_vs_baseline=2.0))
        assert ev.signal_source == "sqlserver"


# ─────────────────────────────────────────────────────────────────────────────
# AC9  |  Pack registration
# ─────────────────────────────────────────────────────────────────────────────

class TestPackRegistration:
    """AC9 — sqlserver_opsignal registered; get_pack and is_sqlserver_opsignal_pack work."""

    def test_sqlserver_opsignal_in_list_packs(self):
        assert "sqlserver_opsignal" in list_packs()

    def test_get_pack_returns_correct_config(self):
        pack = get_pack("sqlserver_opsignal")
        assert pack["packId"] == "sqlserver_opsignal"
        assert pack["domain"] == "sqlserver_opsignal"

    def test_is_sqlserver_opsignal_pack_returns_true(self):
        assert is_sqlserver_opsignal_pack("sqlserver_opsignal") is True

    def test_is_sqlserver_opsignal_pack_returns_false_for_other_packs(self):
        assert is_sqlserver_opsignal_pack("service_cloud") is False
        assert is_sqlserver_opsignal_pack("ncino") is False

    def test_pack_contains_db_ticket_volume_surge_detector(self):
        pack = get_pack("sqlserver_opsignal")
        detectors = pack["detectors"]
        assert any("db_ticket_volume_surge" in d for d in detectors)

    def test_pack_contains_all_three_detectors(self):
        pack = get_pack("sqlserver_opsignal")
        detectors = pack["detectors"]
        assert len(detectors) == 3
        labels = [d.split(".")[-1] for d in detectors]
        assert "db_ticket_volume_surge" in labels
        assert "db_sla_breach_rate" in labels
        assert "db_queue_depth_elevated" in labels

    def test_pack_has_ui_labels_path(self):
        pack = get_pack("sqlserver_opsignal")
        assert pack.get("ui_labels_path") is not None

    def test_pack_has_llm_context(self):
        pack = get_pack("sqlserver_opsignal")
        assert "sql server" in pack.get("llm_context", "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# AC10  |  UI labels
# ─────────────────────────────────────────────────────────────────────────────

class TestUILabels:
    """AC10 — labels file loads; all required keys present for DB_TICKET_VOLUME_SURGE."""

    REQUIRED_KEYS = {"s6_title", "agentType", "s6_why", "s6_action"}

    def test_ui_labels_load_without_error(self):
        labels = get_ui_labels("sqlserver_opsignal")
        assert labels is not None
        assert isinstance(labels, dict)

    def test_db_ticket_volume_surge_has_all_required_keys(self):
        labels = get_ui_labels("sqlserver_opsignal")
        surge_labels = labels.get("DB_TICKET_VOLUME_SURGE", {})
        for key in self.REQUIRED_KEYS:
            assert key in surge_labels, f"UI labels missing key: {key}"

    def test_db_sla_breach_rate_has_all_required_keys(self):
        labels = get_ui_labels("sqlserver_opsignal")
        breach_labels = labels.get("DB_SLA_BREACH_RATE", {})
        for key in self.REQUIRED_KEYS:
            assert key in breach_labels, f"DB_SLA_BREACH_RATE labels missing key: {key}"

    def test_db_queue_depth_elevated_has_all_required_keys(self):
        labels = get_ui_labels("sqlserver_opsignal")
        queue_labels = labels.get("DB_QUEUE_DEPTH_ELEVATED", {})
        for key in self.REQUIRED_KEYS:
            assert key in queue_labels, f"DB_QUEUE_DEPTH_ELEVATED labels missing key: {key}"

    def test_s6_title_is_non_empty_string(self):
        labels = get_ui_labels("sqlserver_opsignal")
        title = labels["DB_TICKET_VOLUME_SURGE"]["s6_title"]
        assert isinstance(title, str) and len(title) > 0

    def test_agent_type_is_non_empty_string(self):
        labels = get_ui_labels("sqlserver_opsignal")
        agent_type = labels["DB_TICKET_VOLUME_SURGE"]["agentType"]
        assert isinstance(agent_type, str) and len(agent_type) > 0


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Scorer
# ─────────────────────────────────────────────────────────────────────────────

class TestScorer:
    """AC12 — MEDIUM confidence, correct tier, roadmap_stage matches spec."""

    def test_db_ticket_volume_surge_confidence_is_medium(self):
        score = get_score("DB_TICKET_VOLUME_SURGE")
        assert score["confidence"] == "MEDIUM"

    def test_db_ticket_volume_surge_tier_is_quick_win(self):
        score = get_score("DB_TICKET_VOLUME_SURGE")
        assert score["tier"] == "Quick Win"

    def test_db_ticket_volume_surge_roadmap_stage(self):
        score = get_score("DB_TICKET_VOLUME_SURGE")
        assert score["roadmap_stage"] == "quick_win"

    def test_db_ticket_volume_surge_impact_and_effort(self):
        score = get_score("DB_TICKET_VOLUME_SURGE")
        assert score["impact"] == 6
        assert score["effort"] == 2

    def test_db_sla_breach_rate_confidence_is_medium(self):
        score = get_score("DB_SLA_BREACH_RATE")
        assert score["confidence"] == "MEDIUM"

    def test_db_queue_depth_elevated_confidence_is_medium(self):
        score = get_score("DB_QUEUE_DEPTH_ELEVATED")
        assert score["confidence"] == "MEDIUM"

    def test_db_queue_depth_elevated_tier_is_strategic(self):
        score = get_score("DB_QUEUE_DEPTH_ELEVATED")
        assert score["tier"] == "Strategic"

    def test_is_sqlserver_opsignal_detector_true_for_all_three(self):
        assert is_sqlserver_opsignal_detector("DB_TICKET_VOLUME_SURGE") is True
        assert is_sqlserver_opsignal_detector("DB_SLA_BREACH_RATE") is True
        assert is_sqlserver_opsignal_detector("DB_QUEUE_DEPTH_ELEVATED") is True

    def test_is_sqlserver_opsignal_detector_false_for_other(self):
        assert is_sqlserver_opsignal_detector("HANDOFF_FRICTION") is False

    def test_score_opportunity_includes_metric_value(self):
        result = score_opportunity("DB_TICKET_VOLUME_SURGE", metric_value=2.3)
        assert result["metric_value"] == 2.3
        assert result["confidence"] == "MEDIUM"
