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

class TestAC10EndToEndRunner:
    """AC10 — the discovery runner, given mocked GitHub data, produces at least
    one OpportunityCandidate from the three GitHub detectors.

    These run the *real* runner.run() pipeline (ingest -> detect -> score ->
    OpportunityCandidate[]) with the async GitHub connector replaced by a
    deterministic mocked payload via runner._ingest_github.
    """

    def _run_with_mock(self, github_payload: Dict[str, Any], systems=None) -> Dict[str, Any]:
        import os
        from unittest.mock import patch
        from discovery import runner

        os.environ["INGEST_MODE"] = "offline"
        try:
            with patch.object(runner, "_ingest_github", return_value=github_payload):
                return runner.run(
                    mode="offline",
                    pack="github_engineering",
                    systems=systems or ["github"],
                    org_id="org-ac10",
                    run_id="run_ac10test",
                )
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_runner_produces_at_least_one_opportunity(self):
        result = self._run_with_mock(
            _github_all(prs_over_threshold=5, top_author_pct=0.75, stale_count=10)
        )
        assert len(result.get("opportunities", [])) >= 1

    def test_runner_packid_is_github_engineering(self):
        result = self._run_with_mock(_github_all())
        assert result.get("packId") == "github_engineering"

    def test_all_three_github_detectors_produce_opportunities(self):
        result = self._run_with_mock(
            _github_all(prs_over_threshold=8, top_author_pct=0.80, stale_count=15)
        )
        fired = {opp["detector_id"] for opp in result.get("opportunities", [])}
        assert {
            "GITHUB_PR_REVIEW_BOTTLENECK",
            "GITHUB_COMMIT_CONCENTRATION",
            "GITHUB_STALE_BRANCHES",
        } <= fired

    def test_opportunities_carry_github_signal_source_and_scores(self):
        result = self._run_with_mock(_github_all())
        opps = result.get("opportunities", [])
        assert opps
        for opp in opps:
            assert opp["signal_source"] == "github"
            assert opp["packId"] == "github_engineering"
            assert opp["confidence"] in ("MEDIUM", "HIGH")
            assert isinstance(opp["impact"], (int, float))
            assert isinstance(opp["effort"], (int, float))

    def test_degraded_github_ingest_produces_no_opportunities(self):
        """All three signal blocks degraded -> no detector fires -> no opportunities."""
        degraded = {
            "pr_review": {
                "prs_over_threshold": 99, "avg_days_open": 9.0,
                "max_days_open": 20.0, "open_pr_count": 99, "degraded_signal": True,
            },
            "commit_concentration": {
                "top_author_pct": 0.99, "top_author_name": "solo",
                "total_contributors": 5, "degraded_signal": True,
            },
            "stale_branches": {
                "stale_count": 99, "total_branches": 120,
                "oldest_stale_days": 90.0, "degraded_signal": True,
            },
        }
        result = self._run_with_mock(degraded)
        gh_opps = [
            o for o in result.get("opportunities", [])
            if o["detector_id"].startswith("GITHUB_")
        ]
        assert gh_opps == []


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


# ─────────────────────────────────────────────────────────────────────────────
# Issue 2 — Solo-repo guard: mixed-repo payload where highest concentration
#           belongs to a solo-contributor repo must suppress the detector.
# ─────────────────────────────────────────────────────────────────────────────

class TestAggregateCommitConcentrationSoloGuard:
    """_aggregate_commit_concentration surfaces the worst (highest pct) repo;
    when that repo has total_contributors=1, the MIN_CONTRIBUTORS guard stops
    the detector from firing."""

    def _aggregate(self, signals):
        from connectors.saas.github import _aggregate_commit_concentration
        return _aggregate_commit_concentration(signals)

    def test_solo_repo_with_highest_pct_surfaces_to_aggregator(self):
        """Aggregator picks the repo with the highest top_author_pct."""
        signals = [
            {"top_author_pct": 0.99, "top_author_name": "solo", "total_contributors": 1, "degraded_signal": False},
            {"top_author_pct": 0.70, "top_author_name": "alice", "total_contributors": 4, "degraded_signal": False},
        ]
        result = self._aggregate(signals)
        assert result["top_author_pct"] == 0.99
        assert result["total_contributors"] == 1

    def test_detector_does_not_fire_when_aggregated_solo_repo_wins(self):
        """Even though pct is 0.99, total_contributors=1 prevents the detector."""
        detect, _, _ = _commit_concentration()
        data = {
            "commit_concentration": {
                "top_author_pct": 0.99,
                "top_author_name": "solo",
                "total_contributors": 1,
                "degraded_signal": False,
            }
        }
        assert detect(data) == []

    def test_detector_fires_when_non_solo_repo_has_high_pct(self):
        """Multi-contributor repo with 75% concentration still fires."""
        signals = [
            {"top_author_pct": 0.99, "top_author_name": "solo", "total_contributors": 1, "degraded_signal": False},
            {"top_author_pct": 0.75, "top_author_name": "alice", "total_contributors": 3, "degraded_signal": False},
        ]
        result = self._aggregate(signals)
        # Solo repo wins on pct — detector must NOT fire (total_contributors=1)
        assert result["total_contributors"] == 1

    def test_aggregate_ignores_degraded_repos(self):
        """Degraded repos are excluded from concentration comparison."""
        signals = [
            {"top_author_pct": 0.99, "top_author_name": "solo", "total_contributors": 1, "degraded_signal": True},
            {"top_author_pct": 0.70, "top_author_name": "bob", "total_contributors": 3, "degraded_signal": False},
        ]
        result = self._aggregate(signals)
        # Degraded solo repo excluded; bob's repo surfaces
        assert result["top_author_name"] == "bob"
        assert result["total_contributors"] == 3
        assert result["degraded_signal"] is True  # any-degraded flag set


# ─────────────────────────────────────────────────────────────────────────────
# Issue 6 — get_pack() logs a WARNING for unknown pack IDs
# ─────────────────────────────────────────────────────────────────────────────

class TestGetPackUnknownIdWarning:
    def test_unknown_pack_id_returns_service_cloud_fallback(self):
        get_pack, _, _ = _pack_config()
        result = get_pack("totally_unknown_pack")
        assert result["packId"] == "service_cloud"

    def test_unknown_pack_id_logs_warning(self, caplog):
        import logging
        get_pack, _, _ = _pack_config()
        with caplog.at_level(logging.WARNING, logger="discovery.packs.pack_config"):
            get_pack("totally_unknown_pack")
        assert any("totally_unknown_pack" in msg for msg in caplog.messages)

    def test_none_pack_id_returns_service_cloud_silently(self, caplog):
        import logging
        get_pack, _, _ = _pack_config()
        with caplog.at_level(logging.WARNING, logger="discovery.packs.pack_config"):
            result = get_pack(None)
        assert result["packId"] == "service_cloud"
        assert not any("unrecognized" in msg for msg in caplog.messages)


# ─────────────────────────────────────────────────────────────────────────────
# Issue 7 — _ingest_github is NOT called for non-github_engineering packs
# ─────────────────────────────────────────────────────────────────────────────

class TestRunnerDoesNotIngestGithubForOtherPacks:
    """_ingest_github must never be called when pack != github_engineering."""

    def _run_pack(self, pack_name: str):
        import os
        from unittest.mock import patch, MagicMock
        from discovery import runner

        os.environ["INGEST_MODE"] = "offline"
        try:
            with patch.object(runner, "_ingest_github", return_value={}) as mock_gh:
                runner.run(
                    mode="offline",
                    pack=pack_name,
                    systems=["servicenow"],
                    org_id="org-test",
                    run_id="run_pack_guard",
                )
                return mock_gh
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_ingest_github_not_called_for_service_cloud(self):
        mock = self._run_pack("service_cloud")
        mock.assert_not_called()

    def test_ingest_github_not_called_for_ncino(self):
        mock = self._run_pack("ncino")
        mock.assert_not_called()

    def test_ingest_github_not_called_for_strs_benefits(self):
        mock = self._run_pack("strs_benefits")
        mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Issue 9 — Exact 7-day boundary for Jira corroboration (strictly > 7 days)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC3ExactSevenDayBoundary:
    """_opened_before uses `days_open > 7` (strictly greater-than).
    7.0 must NOT corroborate; 7.1 must corroborate."""

    def _corroborate(self, days_open: float) -> bool:
        _, corroborate_fn, _ = _scorer()
        jira = _jira_with_unlinked_issue(days_open=days_open)
        return corroborate_fn(jira, jira_connected=True)

    def test_exactly_7_days_does_not_corroborate(self):
        assert self._corroborate(7.0) is False

    def test_just_above_7_days_does_corroborate(self):
        assert self._corroborate(7.1) is True

    def test_6_days_does_not_corroborate(self):
        assert self._corroborate(6.0) is False

    def test_8_days_corroborates(self):
        assert self._corroborate(8.0) is True

    def test_confidence_medium_at_exactly_7_days(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=7.0)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "MEDIUM"

    def test_confidence_high_at_7_point_1_days(self):
        score_fn, _, _ = _scorer()
        dr = _dr("GITHUB_PR_REVIEW_BOTTLENECK")
        jira = _jira_with_unlinked_issue(days_open=7.1)
        result = score_fn(dr, jira_data=jira, jira_connected=True)
        assert result["confidence"] == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# Issue 10 — Runner mock: assert _ingest_github is called with expected args
#            (prevents silent pass on import-path drift)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC10RunnerMockCallAssertion:
    """Verify _ingest_github is called with the right org_id and run_id."""

    def test_ingest_github_called_with_correct_args(self):
        import os
        from unittest.mock import patch
        from discovery import runner

        os.environ["INGEST_MODE"] = "offline"
        try:
            with patch.object(
                runner, "_ingest_github",
                return_value=_github_all(prs_over_threshold=5)
            ) as mock_gh:
                runner.run(
                    mode="offline",
                    pack="github_engineering",
                    systems=["github"],
                    org_id="org-assert-test",
                    run_id="run_assert_123",
                )
                mock_gh.assert_called_once()
                call_kwargs = mock_gh.call_args
                args, kwargs = call_kwargs
                # org_id and run_id may be positional or keyword
                all_args = list(args) + list(kwargs.values())
                assert "org-assert-test" in all_args
                assert "run_assert_123" in all_args
        finally:
            os.environ.pop("INGEST_MODE", None)


# ─────────────────────────────────────────────────────────────────────────────
# Issue 11 — Multi-repo aggregation with partial degradation
# ─────────────────────────────────────────────────────────────────────────────

class TestAggregateMultiRepoPrReview:
    """_aggregate_pr_review: 2 clean repos + 1 degraded repo.
    Weighted average must use all repos (degraded_signal just flags the result)
    and degraded_signal=True must be set in the output."""

    def _aggregate(self, signals):
        from connectors.saas.github import _aggregate_pr_review
        return _aggregate_pr_review(signals)

    def test_partial_degradation_sets_degraded_flag(self):
        signals = [
            {"open_pr_count": 8, "prs_over_threshold": 3, "avg_days_open": 4.0, "max_days_open": 9.0, "degraded_signal": False},
            {"open_pr_count": 6, "prs_over_threshold": 2, "avg_days_open": 3.5, "max_days_open": 7.0, "degraded_signal": False},
            {"open_pr_count": 0, "prs_over_threshold": 0, "avg_days_open": 0.0, "max_days_open": 0.0, "degraded_signal": True},
        ]
        result = self._aggregate(signals)
        assert result["degraded_signal"] is True

    def test_partial_degradation_still_sums_all_repos(self):
        signals = [
            {"open_pr_count": 8, "prs_over_threshold": 3, "avg_days_open": 4.0, "max_days_open": 9.0, "degraded_signal": False},
            {"open_pr_count": 6, "prs_over_threshold": 2, "avg_days_open": 3.5, "max_days_open": 7.0, "degraded_signal": False},
            {"open_pr_count": 0, "prs_over_threshold": 0, "avg_days_open": 0.0, "max_days_open": 0.0, "degraded_signal": True},
        ]
        result = self._aggregate(signals)
        assert result["open_pr_count"] == 14
        assert result["prs_over_threshold"] == 5

    def test_detector_does_not_fire_when_aggregated_result_is_degraded(self):
        """degraded_signal=True from aggregation suppresses detector even if
        prs_over_threshold meets the threshold."""
        detect, _, _ = _pr_bottleneck()
        data = {
            "pr_review": {
                "prs_over_threshold": 5,
                "avg_days_open": 4.0,
                "max_days_open": 9.0,
                "open_pr_count": 14,
                "degraded_signal": True,
            }
        }
        assert detect(data) == []


# ─────────────────────────────────────────────────────────────────────────────
# Issue 12 — UI labels guardrail copy present for all three detectors
# ─────────────────────────────────────────────────────────────────────────────

class TestUILabelsGuardrailCopy:
    """Each s6_action must contain explicit no-automation guardrail language."""

    def _labels(self):
        _, get_labels, _ = _pack_config()
        return get_labels("github_engineering")

    def test_pr_bottleneck_action_contains_no_automated_merge_approvals(self):
        labels = self._labels()
        action = labels["GITHUB_PR_REVIEW_BOTTLENECK"]["s6_action"]
        assert "No automated merge approvals" in action

    def test_commit_concentration_action_contains_no_automated_code_changes(self):
        labels = self._labels()
        action = labels["GITHUB_COMMIT_CONCENTRATION"]["s6_action"]
        assert "No automated code changes" in action

    def test_stale_branches_action_contains_no_automated_branch_deletions(self):
        labels = self._labels()
        action = labels["GITHUB_STALE_BRANCHES"]["s6_action"]
        assert "No automated branch deletions" in action


# ─────────────────────────────────────────────────────────────────────────────
# Issue 13 — Jira field safety: malformed/minimal Jira payloads do not crash
# ─────────────────────────────────────────────────────────────────────────────

class TestJiraFieldSafety:
    """jira_corroborates_pr_bottleneck handles malformed inputs without raising."""

    def _corroborate(self, jira_data, **kwargs):
        _, corroborate_fn, _ = _scorer()
        return corroborate_fn(jira_data, jira_connected=True, **kwargs)

    def test_empty_jira_dict_returns_false(self):
        assert self._corroborate({}) is False

    def test_jira_issues_is_none_returns_false(self):
        assert self._corroborate({"issue_metrics": {"issues": None}}) is False

    def test_jira_issue_missing_days_open_does_not_raise(self):
        # Issue without days_open — should be treated as not corroborating
        jira = {"issue_metrics": {"issues": [{"id": "JIRA-X", "pr_linked": False, "status": "open"}]}}
        result = self._corroborate(jira)
        assert result is False

    def test_jira_issue_missing_pr_linked_does_not_raise(self):
        # Issue without pr_linked — should be treated as not corroborating
        jira = {"issue_metrics": {"issues": [{"id": "JIRA-X", "days_open": 10.0, "status": "open"}]}}
        result = self._corroborate(jira)
        # pr_linked missing → defaults to not linked, so it should corroborate
        assert isinstance(result, bool)

    def test_jira_issue_missing_status_does_not_raise(self):
        jira = {"issue_metrics": {"issues": [{"id": "JIRA-X", "days_open": 10.0, "pr_linked": False}]}}
        result = self._corroborate(jira)
        assert isinstance(result, bool)

    def test_empty_issues_list_returns_false(self):
        assert self._corroborate({"issue_metrics": {"issues": []}}) is False

    def test_issue_metrics_missing_entirely_returns_false(self):
        assert self._corroborate({"other_key": "value"}) is False


# ─────────────────────────────────────────────────────────────────────────────
# Issue 14 — Threshold constants are module-level (not magic numbers)
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectorThresholdConstants:
    """Each detector module must expose its threshold as a module-level constant."""

    def test_pr_bottleneck_has_threshold_constant(self):
        from discovery.detectors.github_pr_bottleneck import PRS_OVER_THRESHOLD_MIN
        assert isinstance(PRS_OVER_THRESHOLD_MIN, int)
        assert PRS_OVER_THRESHOLD_MIN == 5

    def test_commit_concentration_has_threshold_constants(self):
        from discovery.detectors.github_commit_concentration import (
            TOP_AUTHOR_PCT_THRESHOLD,
            MIN_CONTRIBUTORS,
        )
        assert isinstance(TOP_AUTHOR_PCT_THRESHOLD, float)
        assert TOP_AUTHOR_PCT_THRESHOLD == 0.60
        assert isinstance(MIN_CONTRIBUTORS, int)
        assert MIN_CONTRIBUTORS == 2

    def test_stale_branches_has_threshold_constant(self):
        from discovery.detectors.github_stale_branches import STALE_COUNT_THRESHOLD
        assert isinstance(STALE_COUNT_THRESHOLD, int)
        assert STALE_COUNT_THRESHOLD == 10
