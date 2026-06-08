"""
GITHUB_COMMIT_CONCENTRATION detector — AT-187 / T1-S12 Task 3.

Identifies bus-factor risk: a single contributor responsible for 60% or more
of commits over the last 90 days.  A contributor-count guard
(total_contributors >= 2) prevents false positives on solo repositories.
Requires a clean (non-degraded) signal from the GitHub ingestor before
evaluating — degraded data is silently skipped.

Signal source: github connector via connectors/saas/github.py ingest().
Threshold:    top_author_pct >= 0.60 and total_contributors >= 2
              and degraded_signal is False.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "GITHUB_COMMIT_CONCENTRATION"
TOP_AUTHOR_PCT_THRESHOLD = 0.60
MIN_CONTRIBUTORS = 2

SIGNAL_METRICS = [
    "top_author_pct",      # share of commits from the single top author (last 90d)
    "total_contributors",  # distinct commit authors in the window
]


def evaluate(
    github_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    """Evaluate commit concentration and return a DetectorEvaluation.

    Returns fired=True when:
      - degraded_signal is False (ingestor completed cleanly)
      - top_author_pct >= TOP_AUTHOR_PCT_THRESHOLD
      - total_contributors >= MIN_CONTRIBUTORS (solo-repo guard)
    """
    cc = (github_data or {}).get("commit_concentration", {})
    degraded = bool(cc.get("degraded_signal", True))
    top_author_pct = float(cc.get("top_author_pct", 0.0))
    top_author_name = cc.get("top_author_name", "") or ""
    total_contributors = int(cc.get("total_contributors", 0))

    fired = (
        (not degraded)
        and (top_author_pct >= TOP_AUTHOR_PCT_THRESHOLD)
        and (total_contributors >= MIN_CONTRIBUTORS)
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="github",
        metric_value=top_author_pct,
        threshold=TOP_AUTHOR_PCT_THRESHOLD,
        fired=fired,
        raw_evidence={
            "top_author_pct": top_author_pct,
            "top_author_name": top_author_name,  # feeds T3-S12-A entity extraction
            "total_contributors": total_contributors,
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
