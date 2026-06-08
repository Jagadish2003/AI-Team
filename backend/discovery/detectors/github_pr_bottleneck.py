"""
GITHUB_PR_REVIEW_BOTTLENECK detector — AT-186 / T1-S12 Task 2.

Fires when 5 or more open PRs have been waiting longer than
PR_AGE_THRESHOLD_DAYS (default: 3 days).  Requires a clean
(non-degraded) signal from the GitHub ingestor before evaluating —
degraded data is silently skipped.

Signal source: github connector via connectors/saas/github.py ingest().
Threshold:    prs_over_threshold >= 5 and degraded_signal is False.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "GITHUB_PR_REVIEW_BOTTLENECK"
PR_AGE_THRESHOLD_DAYS = 3
PRS_OVER_THRESHOLD_MIN = 5

SIGNAL_METRICS = [
    "prs_over_threshold",  # number of open PRs older than PR_AGE_THRESHOLD_DAYS
    "avg_days_open",       # average age of open PRs in days
    "max_days_open",       # age of the oldest open PR in days
    "open_pr_count",       # total number of open PRs
]


def evaluate(
    github_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    """Evaluate PR review bottleneck and return a DetectorEvaluation.

    Returns fired=True when:
      - degraded_signal is False (ingestor completed cleanly)
      - prs_over_threshold >= PRS_OVER_THRESHOLD_MIN
    """
    pr = (github_data or {}).get("pr_review", {})
    degraded = bool(pr.get("degraded_signal", True))
    prs_over_threshold = int(pr.get("prs_over_threshold", 0))
    avg_days_open = float(pr.get("avg_days_open", 0.0))
    max_days_open = float(pr.get("max_days_open", 0.0))
    open_pr_count = int(pr.get("open_pr_count", 0))

    fired = (not degraded) and (prs_over_threshold >= PRS_OVER_THRESHOLD_MIN)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="github",
        metric_value=float(prs_over_threshold),
        threshold=float(PRS_OVER_THRESHOLD_MIN),
        fired=fired,
        raw_evidence={
            "prs_over_threshold": prs_over_threshold,
            "avg_days_open": avg_days_open,
            "max_days_open": max_days_open,
            "open_pr_count": open_pr_count,
        },
    )


def detect(
    github_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    """Return a list containing one DetectorResult if the detector fires."""
    evaluation = evaluate(github_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
