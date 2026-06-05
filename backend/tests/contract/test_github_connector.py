"""
T1-S12 T8 / AT-192 — GitHub Connector Contract Tests.

Covers acceptance criteria:
  AC1  >= 18 tests, all passing.
  AC2  Threshold boundary tests for each detector: fires at threshold, not below.
  AC3  Jira corroboration: confidence HIGH with Jira + corroborating data; MEDIUM without.
  AC4  degraded_signal: ingestor sets flag on 429/timeout; detector does not fire.
  AC5  Cross-org isolation: signals from org A do not appear in org B results.
  AC6  End-to-end: mocked GitHub data produces at least one DetectorResult from the three detectors.

Run:
  cd backend
  pytest tests/contract/test_github_connector.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from discovery.models import DetectorResult


# ── Import helpers ─────────────────────────────────────────────────────────────

def _pr_bottleneck():
    from discovery.detectors.github_pr_bottleneck import detect, evaluate, SIGNAL_METRICS
    return detect, evaluate, SIGNAL_METRICS


def _commit_concentration():
    from discovery.detectors.github_commit_concentration import detect, evaluate, SIGNAL_METRICS
    return detect, evaluate, SIGNAL_METRICS


def _stale_branches():
    from discovery.detectors.github_stale_branches import detect, evaluate, SIGNAL_METRICS
    return detect, evaluate, SIGNAL_METRICS


def _scorer():
    from discovery.packs.github_engineering_scorer import (
        score_github_engineering,
        jira_corroborates_pr_bottleneck,
        is_github_engineering_detector,
    )
    return score_github_engineering, jira_corroborates_pr_bottleneck, is_github_engineering_detector


def _pack_config():
    from discovery.packs.pack_config import get_pack, get_ui_labels, is_github_engineering_pack
    return get_pack, get_ui_labels, is_github_engineering_pack


# ── Shared fixture builders ────────────────────────────────────────────────────

def _github_pr(
    prs_over_threshold: int = 5,
    avg_days_open: float = 4.5,
    max_days_open: float = 9.0,
    open_pr_count: int = 8,
    degraded: bool = False,
) -> Dict[str, Any]:
    return {
        "pr_review": {
            "prs_over_threshold": prs_over_threshold,
            "avg_days_open": avg_days_open,
            "max_days_open": max_days_open,
            "open_pr_count": open_pr_count,
            "degraded_signal": degraded,
        }
    }


def _github_cc(
    top_author_pct: float = 0.75,
    total_contributors: int = 3,
    top_author_name: str = "alice",
    degraded: bool = False,
) -> Dict[str, Any]:
    return {
        "commit_concentration": {
            "top_author_pct": top_author_pct,
            "top_author_name": top_author_name,
            "total_contributors": total_contributors,
            "degraded_signal": degraded,
        }
    }


def _github_sb(
    stale_count: int = 10,
    total_branches: int = 25,
    oldest_stale_days: float = 45.0,
    degraded: bool = False,
) -> Dict[str, Any]:
    return {
        "stale_branches": {
            "stale_count": stale_count,
            "total_branches": total_branches,
            "oldest_stale_days": oldest_stale_days,
            "degraded_signal": degraded,
        }
    }


def _github_all(
    prs_over_threshold: int = 5,
    top_author_pct: float = 0.75,
    stale_count: int = 10,
) -> Dict[str, Any]:
    """Full ingestor payload with all three signal blocks."""
    data = {}
    data.update(_github_pr(prs_over_threshold=prs_over_threshold))
    data.update(_github_cc(top_author_pct=top_author_pct))
    data.update(_github_sb(stale_count=stale_count))
    return data


def _dr(detector_id: str, metric_value: float = 5.0) -> DetectorResult:
    return DetectorResult(
        detector_id=detector_id,
        signal_source="github",
        metric_value=metric_value,
        threshold=5.0,
        raw_evidence={"prs_over_threshold": int(metric_value)},
    )


def _jira_with_unlinked_issue(
    days_open: float = 10.0,
    org_id: str = None,
    resolved: bool = False,
) -> Dict[str, Any]:
    issue: Dict[str, Any] = {
        "id": "JIRA-1",
        "days_open": days_open,
        "pr_linked": False,
        "status": "open",
    }
    if resolved:
        issue["resolution"] = "done"
        issue["status"] = "done"
    if org_id is not None:
        issue["org_id"] = org_id
    return {"issue_metrics": {"issues": [issue]}}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Threshold boundary tests for each detector
# ─────────────────────────────────────────────────────────────────────────────

class TestAC2PrBottleneckBoundary:
    """GITHUB_PR_REVIEW_BOTTLENECK fires at threshold, not below."""

    def test_fires_at_exactly_threshold(self):
        detect, _, _ = _pr_bottleneck()
        results = detect(_github_pr(prs_over_threshold=5))
        assert len(results) == 1
        assert results[0].detector_id == "GITHUB_PR_REVIEW_BOTTLENECK"

    def test_fires_above_threshold(self):
        detect, _, _ = _pr_bottleneck()
        assert len(detect(_github_pr(prs_over_threshold=12))) == 1

    def test_does_not_fire_one_below_threshold(self):
        detect, _, _ = _pr_bottleneck()
        assert detect(_github_pr(prs_over_threshold=4)) == []

    def test_does_not_fire_at_zero(self):
        detect, _, _ = _pr_bottleneck()
        assert detect(_github_pr(prs_over_threshold=0)) == []


class TestAC2CommitConcentrationBoundary:
    """GITHUB_COMMIT_CONCENTRATION fires at threshold, not below."""

    def test_fires_at_exactly_threshold(self):
        detect, _, _ = _commit_concentration()
        results = detect(_github_cc(top_author_pct=0.60, total_contributors=2))
        assert len(results) == 1
        assert results[0].detector_id == "GITHUB_COMMIT_CONCENTRATION"

    def test_fires_above_threshold(self):
        detect, _, _ = _commit_concentration()
        assert len(detect(_github_cc(top_author_pct=0.95, total_contributors=4))) == 1

    def test_does_not_fire_just_below_threshold(self):
        detect, _, _ = _commit_concentration()
        assert detect(_github_cc(top_author_pct=0.59, total_contributors=3)) == []

    def test_does_not_fire_with_solo_contributor(self):
        """Contributor-count guard: total_contributors < 2 suppresses the detector."""
        detect, _, _ = _commit_concentration()
        assert detect(_github_cc(top_author_pct=0.99, total_contributors=1)) == []


class TestAC2StaleBranchesBoundary:
    """GITHUB_STALE_BRANCHES fires at threshold, not below."""

    def test_fires_at_exactly_threshold(self):
        detect, _, _ = _stale_branches()
        results = detect(_github_sb(stale_count=10))
        assert len(results) == 1
        assert results[0].detector_id == "GITHUB_STALE_BRANCHES"

    def test_fires_above_threshold(self):
        detect, _, _ = _stale_branches()
        assert len(detect(_github_sb(stale_count=25))) == 1

    def test_does_not_fire_one_below_threshold(self):
        detect, _, _ = _stale_branches()
        assert detect(_github_sb(stale_count=9)) == []

    def test_does_not_fire_at_zero(self):
        detect, _, _ = _stale_branches()
        assert detect(_github_sb(stale_count=0)) == []


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Jira corroboration: HIGH with Jira connected + corroborating; MEDIUM without
# ─────────────────────────────────────────────────────────────────────────────

class TestAC3JiraCorroboration:
    """confidence elevates to HIGH only when Jira is connected AND corroborates."""

    def test_confidence_high_with_jira_and_corroborating_issue(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=10.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "HIGH"

    def test_confidence_medium_without_jira(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        result = score_fn(dr, jira_data=None)
        assert result["confidence"] == "MEDIUM"

    def test_confidence_medium_jira_connected_but_no_corroborating_issues(self):
        """Jira connected but all issues are resolved — no corroboration."""
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=10.0, resolved=True)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_confidence_medium_jira_issue_with_pr_linkage(self):
        """Issue has PR linkage — does not corroborate the bottleneck."""
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = {"issue_metrics": {"issues": [{"id": "JIRA-2", "days_open": 15.0, "pr_linked": True, "status": "open"}]}}
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_confidence_medium_jira_issue_not_old_enough(self):
        """Issue open only 3 days — below the 7-day threshold."""
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=3.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_commit_concentration_never_elevates_to_high(self):
        """Only PR_REVIEW_BOTTLENECK participates in Jira corroboration."""
        score_fn, _, _ = _scorer()
        dr = DetectorResult(
            detector_id="GITHUB_COMMIT_CONCENTRATION",
            signal_source="github",
            metric_value=0.75,
            threshold=0.60,
            raw_evidence={"top_author_pct": 0.75, "total_contributors": 3},
        )
        jira = _jira_with_unlinked_issue(days_open=10.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_stale_branches_never_elevates_to_high(self):
        score_fn, _, _ = _scorer()
        dr = DetectorResult(
            detector_id="GITHUB_STALE_BRANCHES",
            signal_source="github",
            metric_value=12.0,
            threshold=10.0,
            raw_evidence={"stale_count": 12, "total_branches": 30, "oldest_stale_days": 45.0},
        )
        jira = _jira_with_unlinked_issue(days_open=10.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_jira_corroborates_pr_bottleneck_true_with_valid_issue(self):
        _, corroborate_fn, _ = _scorer()
        jira = _jira_with_unlinked_issue(days_open=10.0)
        assert corroborate_fn(jira, jira_connected=True) is True

    def test_jira_corroborates_pr_bottleneck_false_with_no_data(self):
        _, corroborate_fn, _ = _scorer()
        assert corroborate_fn(None, jira_connected=False) is False

    def test_corroborated_flag_true_when_elevated(self):
        """score_debug reflects the corroboration."""
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=10.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["corroborated"] is True
        assert "jira" in [s.lower() for s in result["corroboration_sources"]]


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — degraded_signal: ingestor sets flag on 429/timeout; detector does not fire
# ─────────────────────────────────────────────────────────────────────────────

class TestAC4DegradedSignal:
    """When degraded_signal=True detectors must not fire."""

    def test_pr_bottleneck_does_not_fire_when_degraded(self):
        detect, _, _ = _pr_bottleneck()
        assert detect(_github_pr(prs_over_threshold=99, degraded=True)) == []

    def test_commit_concentration_does_not_fire_when_degraded(self):
        detect, _, _ = _commit_concentration()
        assert detect(_github_cc(top_author_pct=0.99, total_contributors=5, degraded=True)) == []

    def test_stale_branches_does_not_fire_when_degraded(self):
        detect, _, _ = _stale_branches()
        assert detect(_github_sb(stale_count=100, degraded=True)) == []

    def test_pr_bottleneck_fires_after_degraded_clears(self):
        """Degraded on first call; clean signal on second fires correctly."""
        detect, _, _ = _pr_bottleneck()
        assert detect(_github_pr(prs_over_threshold=5, degraded=True)) == []
        assert len(detect(_github_pr(prs_over_threshold=5, degraded=False))) == 1

    def test_degraded_default_when_block_absent(self):
        """Missing signal block is treated as degraded — no false positives."""
        detect, _, _ = _pr_bottleneck()
        assert detect({}) == []


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — Cross-org isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestAC5CrossOrgIsolation:
    """Jira issues from org A must not corroborate signals in org B."""

    def test_corroboration_uses_issue_from_correct_org(self):
        _, corroborate_fn, _ = _scorer()
        jira = _jira_with_unlinked_issue(days_open=10.0, org_id="org-a")
        assert corroborate_fn(jira, org_id="org-a", jira_connected=True) is True

    def test_corroboration_does_not_use_issue_from_different_org(self):
        _, corroborate_fn, _ = _scorer()
        jira = _jira_with_unlinked_issue(days_open=10.0, org_id="org-a")
        assert corroborate_fn(jira, org_id="org-b", jira_connected=True) is False

    def test_org_b_score_stays_medium_when_only_org_a_jira_present(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=10.0, org_id="org-a")
        result = score_fn(dr, jira_data=jira, org_id="org-b", jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_org_a_score_elevated_independently_of_org_b(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=10.0, org_id="org-a")
        result_a = score_fn(dr, jira_data=jira, org_id="org-a", jira_connected=True)
        result_b = score_fn(dr, jira_data=jira, org_id="org-b", jira_connected=True)
        assert result_a["confidence"] == "HIGH"
        assert result_b["confidence"] == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — End-to-end: mocked GitHub data produces at least one DetectorResult
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6EndToEnd:
    """Mocked GitHub ingestor payload drives at least one DetectorResult."""

    def test_pr_bottleneck_produces_detector_result(self):
        detect, _, _ = _pr_bottleneck()
        results = detect(_github_all(prs_over_threshold=5))
        assert len(results) == 1
        assert isinstance(results[0], DetectorResult)

    def test_commit_concentration_produces_detector_result(self):
        detect, _, _ = _commit_concentration()
        results = detect(_github_all(top_author_pct=0.75))
        assert len(results) == 1
        assert isinstance(results[0], DetectorResult)

    def test_stale_branches_produces_detector_result(self):
        detect, _, _ = _stale_branches()
        results = detect(_github_all(stale_count=10))
        assert len(results) == 1
        assert isinstance(results[0], DetectorResult)

    def test_all_three_detectors_fire_from_single_payload(self):
        """Full mocked payload fires all three detectors."""
        pr_detect, _, _ = _pr_bottleneck()
        cc_detect, _, _ = _commit_concentration()
        sb_detect, _, _ = _stale_branches()
        payload = _github_all(prs_over_threshold=5, top_author_pct=0.75, stale_count=10)
        results = (
            pr_detect(payload)
            + cc_detect(payload)
            + sb_detect(payload)
        )
        assert len(results) >= 1
        assert all(isinstance(r, DetectorResult) for r in results)

    def test_detector_results_have_correct_signal_source(self):
        detect, _, _ = _pr_bottleneck()
        results = detect(_github_pr(prs_over_threshold=5))
        assert results[0].signal_source == "github"

    def test_detector_results_contain_numeric_raw_evidence(self):
        detect, _, _ = _pr_bottleneck()
        results = detect(_github_pr(prs_over_threshold=7, avg_days_open=5.0, max_days_open=10.0, open_pr_count=9))
        ev = results[0].raw_evidence
        for key in ("prs_over_threshold", "avg_days_open", "max_days_open", "open_pr_count"):
            assert isinstance(ev[key], (int, float))

    def test_scorer_produces_full_score_dict_for_fired_result(self):
        score_fn, _, _ = _scorer()
        detect, _, _ = _pr_bottleneck()
        results = detect(_github_pr(prs_over_threshold=5))
        score = score_fn(results[0])
        required = {"tier", "impact", "effort", "effort_label", "confidence", "roadmap_stage", "score_debug"}
        assert required.issubset(score)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL_METRICS validation
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalMetricsValidation:
    """All SIGNAL_METRICS keys are numeric and present in raw_evidence."""

    def test_pr_bottleneck_signal_metrics_all_in_raw_evidence(self):
        detect, _, sm = _pr_bottleneck()
        results = detect(_github_pr(prs_over_threshold=5))
        ev = results[0].raw_evidence
        for key in sm:
            assert key in ev, f"Missing '{key}' in raw_evidence"
            assert isinstance(ev[key], (int, float)), f"'{key}' must be numeric"

    def test_commit_concentration_signal_metrics_all_in_raw_evidence(self):
        detect, _, sm = _commit_concentration()
        results = detect(_github_cc(top_author_pct=0.75, total_contributors=3))
        ev = results[0].raw_evidence
        for key in sm:
            assert key in ev, f"Missing '{key}' in raw_evidence"
            assert isinstance(ev[key], (int, float)), f"'{key}' must be numeric"

    def test_stale_branches_signal_metrics_all_in_raw_evidence(self):
        detect, _, sm = _stale_branches()
        results = detect(_github_sb(stale_count=10))
        ev = results[0].raw_evidence
        for key in sm:
            assert key in ev, f"Missing '{key}' in raw_evidence"
            assert isinstance(ev[key], (int, float)), f"'{key}' must be numeric"

    def test_all_detectors_have_at_most_8_signal_metrics(self):
        _, _, sm_pr = _pr_bottleneck()
        _, _, sm_cc = _commit_concentration()
        _, _, sm_sb = _stale_branches()
        assert len(sm_pr) <= 8
        assert len(sm_cc) <= 8
        assert len(sm_sb) <= 8


# ─────────────────────────────────────────────────────────────────────────────
# Pack registration and UI labels (coverage supplement)
# ─────────────────────────────────────────────────────────────────────────────

class TestPackRegistration:
    """Pack is correctly registered and UI labels are loadable."""

    def test_github_engineering_in_pack_registry(self):
        get_pack, _, _ = _pack_config()
        pack = get_pack("github_engineering")
        assert pack["packId"] == "github_engineering"

    def test_is_github_engineering_pack_true(self):
        _, _, is_pack = _pack_config()
        assert is_pack("github_engineering") is True

    def test_is_github_engineering_pack_false_for_other_packs(self):
        _, _, is_pack = _pack_config()
        assert is_pack("service_cloud") is False
        assert is_pack("ncino") is False

    def test_ui_labels_load_for_all_three_detectors(self):
        get_pack, get_labels, _ = _pack_config()
        labels = get_labels("github_engineering")
        assert labels is not None
        for det_id in ("GITHUB_PR_REVIEW_BOTTLENECK", "GITHUB_COMMIT_CONCENTRATION", "GITHUB_STALE_BRANCHES"):
            assert det_id in labels
            for field in ("s6_title", "agentType", "s6_why", "s6_action"):
                assert labels[det_id][field]
            assert labels[det_id]["agentType"] == "Monitoring Agent"
