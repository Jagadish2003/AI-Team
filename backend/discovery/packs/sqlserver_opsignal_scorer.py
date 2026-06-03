"""Scorer for the SQL Server Operational Signal Pack."""
from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:
    from discovery.models import DetectorResult


_SQLSERVER_SCORES: Dict[str, Dict[str, Any]] = {
    "DB_TICKET_VOLUME_SURGE": {
        "tier": "Quick Win",
        "impact": 6,
        "effort": 2,
        "confidence": "MEDIUM",
        "roadmap_stage": "quick_win",
    },
    "DB_SLA_BREACH_RATE": {
        "tier": "Quick Win",
        "impact": 7,
        "effort": 2,
        "confidence": "MEDIUM",
        "roadmap_stage": "quick_win",
    },
    "DB_QUEUE_DEPTH_ELEVATED": {
        "tier": "Strategic",
        "impact": 8,
        "effort": 3,
        "confidence": "MEDIUM",
        "roadmap_stage": "strategic",
    },
}

_DEFAULT_SCORE: Dict[str, Any] = {
    "tier": "Quick Win",
    "impact": 5,
    "effort": 2,
    "confidence": "MEDIUM",
    "roadmap_stage": "quick_win",
}


def is_sqlserver_opsignal_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the SQL Server opsignal pack."""
    return detector_id in _SQLSERVER_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return the score table entry for detector_id."""
    return _SQLSERVER_SCORES.get(detector_id, _DEFAULT_SCORE)


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a score dict plus the detector metric value."""
    score = dict(get_score(detector_id))
    score["metric_value"] = metric_value
    return score


def score_sqlserver_opsignal(dr: DetectorResult) -> Optional[Dict[str, Any]]:
    """Score a sqlserver_opsignal DetectorResult.

    Returns None when the detector_id is not part of this pack so callers can
    fall through to other pack scorers.
    """
    if not is_sqlserver_opsignal_detector(dr.detector_id):
        return None

    score = dict(get_score(dr.detector_id))
    return {
        "detector_id": dr.detector_id,
        "tier": score["tier"],
        "impact": score["impact"],
        "effort": score["effort"],
        "confidence": score["confidence"],
        "roadmap_stage": score["roadmap_stage"],
        "corroborated": False,
        "corroboration_sources": [],
        "score_debug": {
            "pack": "sqlserver_opsignal",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
        },
    }
