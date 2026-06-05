"""Canonical scorer for the GitHub Engineering Signal Pack."""
from __future__ import annotations

import logging
from typing import Any, Dict

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:
    from discovery.models import DetectorResult

logger = logging.getLogger(__name__)

_GITHUB_ENGINEERING_SCORES: Dict[str, Dict[str, Any]] = {
    "GITHUB_PR_REVIEW_BOTTLENECK": {
        "tier": "Quick Win",
        "impact": 7,
        "effort": 2,
        "confidence": "MEDIUM",  # elevates to HIGH with Jira corroboration (T7)
        "roadmap_stage": "quick_win",
    },
    "GITHUB_COMMIT_CONCENTRATION": {
        "tier": "Strategic",
        "impact": 8,
        "effort": 3,
        "confidence": "MEDIUM",
        "roadmap_stage": "strategic",
    },
    "GITHUB_STALE_BRANCHES": {
        "tier": "Quick Win",
        "impact": 5,
        "effort": 1,
        "confidence": "MEDIUM",
        "roadmap_stage": "quick_win",
    },
}

_DEFAULT_SCORE: Dict[str, Any] = {
    "tier": "Quick Win",
    "impact": 5,
    "effort": 2,
    "confidence": "MEDIUM",
    "roadmap_stage": "quick_win",
}

_EFFORT_LABEL: Dict[int, str] = {1: "Low", 2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}


def is_github_engineering_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the GitHub Engineering pack."""
    return detector_id in _GITHUB_ENGINEERING_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return a copy of the score table entry for detector_id."""
    return dict(_GITHUB_ENGINEERING_SCORES.get(detector_id, _DEFAULT_SCORE))


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a score dict plus the detector metric value."""
    score = get_score(detector_id)
    score["metric_value"] = metric_value
    return score


def score_github_engineering(dr: DetectorResult) -> Dict[str, Any]:
    """Score a GitHub engineering signal DetectorResult.

    Unknown detector IDs receive safe default scoring instead of falling
    through silently. Confidence elevation to HIGH via Jira corroboration
    is handled in T7 (github_engineering_scorer corroboration hook).
    """
    known = is_github_engineering_detector(dr.detector_id)
    score = get_score(dr.detector_id)

    if not known:
        logger.warning(
            "score_github_engineering: unknown detector '%s' - returning default score. "
            "Check pack_config.py detector list.",
            dr.detector_id,
        )

    return {
        "tier": score["tier"],
        "impact": score["impact"],
        "effort": score["effort"],
        "effort_label": _EFFORT_LABEL.get(score["effort"], "Low"),
        "confidence": score["confidence"],
        "roadmap_stage": score["roadmap_stage"],
        "corroborated": False,
        "corroboration_sources": [],
        "score_debug": {
            "detector_id": dr.detector_id,
            "scorer": "github_engineering",
            "pack": "github_engineering",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact": score["impact"],
            "final_impact": score["impact"],
            "confidence_note": (
                "MEDIUM - GitHub-only signal. Elevates to HIGH with Jira "
                "corroboration when unlinked open issues exceed 7 days (T7)."
            ),
            **({"note": "unknown detector - default score applied"} if not known else {}),
        },
    }
