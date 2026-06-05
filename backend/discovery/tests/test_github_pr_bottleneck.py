"""
AT-186 / T1-S12 Task 2 — GITHUB_PR_REVIEW_BOTTLENECK detector unit tests.

Acceptance criteria coverage:
  AC1 — fires when prs_over_threshold >= 5 AND degraded_signal is False
  AC2 — does not fire when prs_over_threshold < 5 (even if avg_days_open is high)
  AC3 — does not fire when degraded_signal is True
  AC4 — SIGNAL_METRICS == ['prs_over_threshold', 'avg_days_open', 'max_days_open', 'open_pr_count']
  AC5 — each SIGNAL_METRICS key is numeric and present in raw_evidence
"""
from __future__ import annotations

import pytest
from discovery.detectors.github_pr_bottleneck import (
    DETECTOR_ID,
    SIGNAL_METRICS,
    detect,
    evaluate,
)


def _github_data(
    prs_over_threshold: int = 5,
    avg_days_open: float = 4.5,
    max_days_open: float = 9.0,
    open_pr_count: int = 8,
    degraded_signal: bool = False,
) -> dict:
    return {
        "pr_review": {
            "prs_over_threshold": prs_over_threshold,
            "avg_days_open": avg_days_open,
            "max_days_open": max_days_open,
            "open_pr_count": open_pr_count,
            "degraded_signal": degraded_signal,
        }
    }


# ─── AC4: SIGNAL_METRICS shape ────────────────────────────────────────────────

class TestSignalMetrics:
    def test_signal_metrics_exact_keys(self):
        """AC4 — exact list, exact order, no extras."""
        assert SIGNAL_METRICS == [
            "prs_over_threshold",
            "avg_days_open",
            "max_days_open",
            "open_pr_count",
        ]

    def test_signal_metrics_length(self):
        assert len(SIGNAL_METRICS) == 4


# ─── AC1: fires when prs_over_threshold >= 5, degraded=False ─────────────────

class TestFiresOnThreshold:
    def test_fires_at_exactly_5(self):
        """AC1 — boundary: prs_over_threshold == 5."""
        results = detect(_github_data(prs_over_threshold=5))
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == DETECTOR_ID
        assert r.signal_source == "github"
        assert r.metric_value == 5.0
        assert r.threshold == 5.0

    def test_fires_above_threshold(self):
        """AC1 — prs_over_threshold well above threshold."""
        results = detect(_github_data(prs_over_threshold=12))
        assert len(results) == 1
        assert results[0].metric_value == 12.0

    def test_raw_evidence_contains_all_signal_metrics(self):
        """AC5 — all SIGNAL_METRICS keys present and numeric in raw_evidence."""
        results = detect(_github_data(prs_over_threshold=6, avg_days_open=5.2, max_days_open=10.0, open_pr_count=9))
        assert len(results) == 1
        ev = results[0].raw_evidence
        for key in SIGNAL_METRICS:
            assert key in ev, f"Missing key '{key}' in raw_evidence"
            assert isinstance(ev[key], (int, float)), f"Key '{key}' is not numeric"

    def test_raw_evidence_values_match_input(self):
        """AC5 — raw_evidence values match the ingestor data."""
        results = detect(_github_data(prs_over_threshold=7, avg_days_open=6.0, max_days_open=11.5, open_pr_count=10))
        ev = results[0].raw_evidence
        assert ev["prs_over_threshold"] == 7
        assert ev["avg_days_open"] == 6.0
        assert ev["max_days_open"] == 11.5
        assert ev["open_pr_count"] == 10


# ─── AC2: does not fire when prs_over_threshold < 5 ──────────────────────────

class TestDoesNotFireBelowThreshold:
    def test_does_not_fire_at_4(self):
        """AC2 — one below threshold."""
        assert detect(_github_data(prs_over_threshold=4, avg_days_open=999.0)) == []

    def test_does_not_fire_at_zero(self):
        """AC2 — no stale PRs."""
        assert detect(_github_data(prs_over_threshold=0)) == []

    def test_does_not_fire_high_avg_days_but_low_count(self):
        """AC2 — avg_days_open is high but prs_over_threshold is below 5."""
        assert detect(_github_data(prs_over_threshold=3, avg_days_open=30.0, max_days_open=60.0)) == []


# ─── AC3: does not fire when degraded_signal is True ─────────────────────────

class TestDoesNotFireWhenDegraded:
    def test_does_not_fire_when_degraded(self):
        """AC3 — degraded signal suppresses the detector."""
        assert detect(_github_data(prs_over_threshold=10, degraded_signal=True)) == []

    def test_does_not_fire_degraded_at_threshold(self):
        """AC3 — even at exactly 5, degraded suppresses."""
        assert detect(_github_data(prs_over_threshold=5, degraded_signal=True)) == []


# ─── evaluate() function: returns evaluation even when not fired ──────────────

class TestEvaluate:
    def test_evaluate_fired_true(self):
        ev = evaluate(_github_data(prs_over_threshold=5))
        assert ev.fired is True

    def test_evaluate_fired_false_below_threshold(self):
        ev = evaluate(_github_data(prs_over_threshold=4))
        assert ev.fired is False

    def test_evaluate_fired_false_degraded(self):
        ev = evaluate(_github_data(prs_over_threshold=10, degraded_signal=True))
        assert ev.fired is False

    def test_evaluate_returns_when_github_data_missing(self):
        """Graceful handling of absent pr_review block."""
        ev = evaluate({})
        assert ev.fired is False

    def test_evaluate_returns_when_github_data_none(self):
        """Graceful handling of None input."""
        ev = evaluate(None)
        assert ev.fired is False
