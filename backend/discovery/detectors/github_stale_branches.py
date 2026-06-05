"""
GITHUB_STALE_BRANCHES detector — AT-188 / T1-S12 Task 4.

Fires when 10 or more branches have had no commit activity for longer than
STALE_BRANCH_DAYS (30 days).  Requires a clean (non-degraded) signal from
the GitHub ingestor before evaluating — degraded data is silently skipped.

Signal source: github connector via connectors/saas/github.py ingest().
Threshold:    stale_count >= 10 and degraded_signal is False.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "GITHUB_STALE_BRANCHES"
STALE_BRANCH_DAYS = 30
STALE_COUNT_THRESHOLD = 10

SIGNAL_METRICS = [
    "stale_count",        # number of branches inactive for >= STALE_BRANCH_DAYS
    "total_branches",     # total branch count in the org/repo
    "oldest_stale_days",  # age of the most-stale branch in days
]


def evaluate(
    github_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    """Evaluate stale-branch accumulation and return a DetectorEvaluation.

    Returns fired=True when:
      - degraded_signal is False (ingestor completed cleanly)
      - stale_count >= STALE_COUNT_THRESHOLD
    """
    sb = (github_data or {}).get("stale_branches", {})
    degraded = bool(sb.get("degraded_signal", True))
    stale_count = int(sb.get("stale_count", 0))
    total_branches = int(sb.get("total_branches", 0))
    oldest_stale_days = float(sb.get("oldest_stale_days", 0.0))

    fired = (not degraded) and (stale_count >= STALE_COUNT_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="github",
        metric_value=float(stale_count),
        threshold=float(STALE_COUNT_THRESHOLD),
        fired=fired,
        raw_evidence={
            "stale_count": stale_count,
            "total_branches": total_branches,
            "oldest_stale_days": oldest_stale_days,
            "degraded_signal": degraded,
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
