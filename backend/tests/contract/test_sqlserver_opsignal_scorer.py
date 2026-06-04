"""
T2-S11-A — SQL Server Operational Signal Pack Scorer — Contract Tests
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Covers acceptance criteria AC12 and related scorer behaviour.

AC12  Scorer returns MEDIUM confidence for all three detectors when no
      cross-system corroboration exists.  tier and roadmap_stage values
      match T2-S11-A Section 2e.

Also verifies:
  - Return shape is identical to other pack scorers (tier, impact, effort,
    confidence, roadmap_stage) so SQL Server opportunities slot into the
    existing AgentIQ opportunity and roadmap flow without changes.
  - is_sqlserver_opsignal_detector() correctly identifies pack membership.
  - score_sqlserver_opsignal() never raises on valid or unknown detector IDs.

Run:
  cd backend
  pytest tests/contract/test_sqlserver_opsignal_scorer.py -v
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from discovery.models import DetectorResult
from discovery.sqlserver_opsignal_scorer import (
    get_score,
    is_sqlserver_opsignal_detector,
    score_sqlserver_opsignal,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _result(detector_id: str, metric_value: float = 2.0) -> DetectorResult:
    """Build a minimal fired DetectorResult for a SQL Server detector."""
    return DetectorResult(
        detector_id=detector_id,
        signal_source="sqlserver",
        metric_value=metric_value,
        threshold=1.5,
        raw_evidence={"metric": metric_value},
    )


ALL_DETECTOR_IDS = [
    "DB_TICKET_VOLUME_SURGE",
    "DB_SLA_BREACH_RATE",
    "DB_QUEUE_DEPTH_ELEVATED",
]

REQUIRED_SCORE_KEYS = {"tier", "impact", "effort", "confidence", "roadmap_stage"}


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Confidence is MEDIUM for all three detectors
# ─────────────────────────────────────────────────────────────────────────────

class TestConfidence:
    """AC12 — all DB-only signals start at MEDIUM confidence."""

    def test_db_ticket_volume_surge_confidence_is_medium(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["confidence"] == "MEDIUM"

    def test_db_sla_breach_rate_confidence_is_medium(self):
        score = score_sqlserver_opsignal(_result("DB_SLA_BREACH_RATE"))
        assert score["confidence"] == "MEDIUM"

    def test_db_queue_depth_elevated_confidence_is_medium(self):
        score = score_sqlserver_opsignal(_result("DB_QUEUE_DEPTH_ELEVATED"))
        assert score["confidence"] == "MEDIUM"

    @pytest.mark.parametrize("detector_id", ALL_DETECTOR_IDS)
    def test_all_detectors_confidence_medium(self, detector_id):
        """AC12 — parametrised over all three detectors."""
        score = score_sqlserver_opsignal(_result(detector_id))
        assert score["confidence"] == "MEDIUM", (
            f"{detector_id} confidence should be MEDIUM for DB-only signal, "
            f"got {score['confidence']!r}"
        )

    def test_confidence_not_high(self):
        """No DB-only signal should start at HIGH — that requires T2-S16-A corroboration."""
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert score["confidence"] != "HIGH", (
                f"{det_id} must not be HIGH before T2-S16-A cross-system corroboration"
            )

    def test_confidence_not_low(self):
        """DB signals are meaningful — should not be LOW."""
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert score["confidence"] != "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Tier values match spec
# ─────────────────────────────────────────────────────────────────────────────

class TestTier:
    """AC12 — tier values match T2-S11-A Section 2e."""

    def test_db_ticket_volume_surge_tier_is_quick_win(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["tier"] == "Quick Win"

    def test_db_sla_breach_rate_tier_is_quick_win(self):
        score = score_sqlserver_opsignal(_result("DB_SLA_BREACH_RATE"))
        assert score["tier"] == "Quick Win"

    def test_db_queue_depth_elevated_tier_is_strategic(self):
        score = score_sqlserver_opsignal(_result("DB_QUEUE_DEPTH_ELEVATED"))
        assert score["tier"] == "Strategic"


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Impact values match spec
# ─────────────────────────────────────────────────────────────────────────────

class TestImpact:
    """AC12 — impact values match T2-S11-A Section 2e."""

    def test_db_ticket_volume_surge_impact_is_6(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["impact"] == 6

    def test_db_sla_breach_rate_impact_is_7(self):
        score = score_sqlserver_opsignal(_result("DB_SLA_BREACH_RATE"))
        assert score["impact"] == 7

    def test_db_queue_depth_elevated_impact_is_8(self):
        score = score_sqlserver_opsignal(_result("DB_QUEUE_DEPTH_ELEVATED"))
        assert score["impact"] == 8

    def test_impact_in_valid_range(self):
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert 1 <= score["impact"] <= 10, (
                f"{det_id} impact {score['impact']} is outside 1–10 range"
            )


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Effort values match spec
# ─────────────────────────────────────────────────────────────────────────────

class TestEffort:
    """AC12 — effort values match T2-S11-A Section 2e."""

    def test_db_ticket_volume_surge_effort_is_2(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["effort"] == 2

    def test_db_sla_breach_rate_effort_is_2(self):
        score = score_sqlserver_opsignal(_result("DB_SLA_BREACH_RATE"))
        assert score["effort"] == 2

    def test_db_queue_depth_elevated_effort_is_3(self):
        score = score_sqlserver_opsignal(_result("DB_QUEUE_DEPTH_ELEVATED"))
        assert score["effort"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# AC12  |  Roadmap stage
# ─────────────────────────────────────────────────────────────────────────────

class TestRoadmapStage:
    """AC12 — roadmap_stage matches spec."""

    def test_db_ticket_volume_surge_roadmap_stage_is_quick_win(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["roadmap_stage"] == "quick_win"

    def test_db_sla_breach_rate_roadmap_stage_is_quick_win(self):
        score = score_sqlserver_opsignal(_result("DB_SLA_BREACH_RATE"))
        assert score["roadmap_stage"] == "quick_win"

    def test_db_queue_depth_elevated_roadmap_stage_is_strategic(self):
        score = score_sqlserver_opsignal(_result("DB_QUEUE_DEPTH_ELEVATED"))
        assert score["roadmap_stage"] == "strategic"


# ─────────────────────────────────────────────────────────────────────────────
# Return shape compatibility with existing scorers
# ─────────────────────────────────────────────────────────────────────────────

class TestReturnShape:
    """score_sqlserver_opsignal() returns same field set as other pack scorers."""

    @pytest.mark.parametrize("detector_id", ALL_DETECTOR_IDS)
    def test_all_required_keys_present(self, detector_id):
        score = score_sqlserver_opsignal(_result(detector_id))
        for key in REQUIRED_SCORE_KEYS:
            assert key in score, (
                f"Score for {detector_id} missing required key '{key}'"
            )

    def test_score_debug_present(self):
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert "score_debug" in score

    def test_score_debug_contains_detector_id(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["score_debug"]["detector_id"] == "DB_TICKET_VOLUME_SURGE"

    def test_score_debug_contains_scorer_name(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert score["score_debug"]["scorer"] == "sqlserver_opsignal"

    def test_effort_label_present(self):
        """effort_label is returned for UI display — matches lending_scorer pattern."""
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert "effort_label" in score
            assert isinstance(score["effort_label"], str)

    def test_tier_is_string(self):
        score = score_sqlserver_opsignal(_result("DB_TICKET_VOLUME_SURGE"))
        assert isinstance(score["tier"], str)

    def test_impact_is_int(self):
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert isinstance(score["impact"], int)

    def test_effort_is_int(self):
        for det_id in ALL_DETECTOR_IDS:
            score = score_sqlserver_opsignal(_result(det_id))
            assert isinstance(score["effort"], int)

    def test_score_is_deterministic(self):
        """Same input always produces same output — scorer is a pure function."""
        dr = _result("DB_SLA_BREACH_RATE")
        score_a = score_sqlserver_opsignal(dr)
        score_b = score_sqlserver_opsignal(dr)
        assert score_a == score_b


# ─────────────────────────────────────────────────────────────────────────────
# is_sqlserver_opsignal_detector() helper
# ─────────────────────────────────────────────────────────────────────────────

class TestIsDetectorHelper:
    """is_sqlserver_opsignal_detector() correctly identifies pack membership."""

    @pytest.mark.parametrize("detector_id", ALL_DETECTOR_IDS)
    def test_returns_true_for_all_three_sql_server_detectors(self, detector_id):
        assert is_sqlserver_opsignal_detector(detector_id) is True

    def test_returns_false_for_service_cloud_detector(self):
        assert is_sqlserver_opsignal_detector("HANDOFF_FRICTION") is False

    def test_returns_false_for_ncino_detector(self):
        assert is_sqlserver_opsignal_detector("LOAN_ORIGINATION_ROUTING_FRICTION") is False

    def test_returns_false_for_strs_detector(self):
        assert is_sqlserver_opsignal_detector("APPLICATION_STALL") is False

    def test_returns_false_for_empty_string(self):
        assert is_sqlserver_opsignal_detector("") is False

    def test_returns_false_for_unknown_id(self):
        assert is_sqlserver_opsignal_detector("UNKNOWN_DETECTOR") is False

    def test_returns_false_for_lowercase_variant(self):
        """Detector IDs are case-sensitive."""
        assert is_sqlserver_opsignal_detector("db_ticket_volume_surge") is False


# ─────────────────────────────────────────────────────────────────────────────
# Robustness — unknown detectors, never raises
# ─────────────────────────────────────────────────────────────────────────────

class TestRobustness:
    """Scorer never raises; unknown detectors get safe defaults."""

    def test_unknown_detector_returns_defaults_not_raises(self):
        dr = DetectorResult(
            detector_id="UNKNOWN_DB_DETECTOR",
            signal_source="sqlserver",
            metric_value=1.0,
            threshold=1.0,
            raw_evidence={"value": 1},
        )
        score = score_sqlserver_opsignal(dr)
        assert isinstance(score, dict)
        for key in REQUIRED_SCORE_KEYS:
            assert key in score

    def test_unknown_detector_confidence_is_medium(self):
        """Default score for unknown detectors should still be safe (MEDIUM)."""
        dr = DetectorResult(
            detector_id="FUTURE_DETECTOR",
            signal_source="sqlserver",
            metric_value=1.0,
            threshold=1.0,
            raw_evidence={"value": 1},
        )
        score = score_sqlserver_opsignal(dr)
        assert score["confidence"] == "MEDIUM"

    def test_get_score_supports_all_expected_ids(self):
        """Public score lookup supports every expected SQL Server detector."""
        scores = [get_score(detector_id) for detector_id in ALL_DETECTOR_IDS]
        assert all(REQUIRED_SCORE_KEYS.issubset(score) for score in scores)

    def test_get_score_returns_an_independent_copy(self):
        score = get_score("DB_TICKET_VOLUME_SURGE")
        score["impact"] = 999
        assert get_score("DB_TICKET_VOLUME_SURGE")["impact"] == 6
