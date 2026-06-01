"""
SQL Server Operational Signal Pack scorer — T2-S11-A.

Scores the three sqlserver_opsignal detectors with confidence=MEDIUM for
DB-only signals. Confidence elevates to HIGH via the T2-S16-A normalisation
layer when ServiceNow or Jira findings corroborate the same pattern.
"""
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

_SQLSERVER_DETECTOR_IDS = frozenset(_SQLSERVER_SCORES.keys())


def is_sqlserver_opsignal_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the sqlserver_opsignal pack."""
    return detector_id in _SQLSERVER_DETECTOR_IDS


def score_sqlserver_opsignal(dr: DetectorResult) -> Optional[Dict[str, Any]]:
    """Score a sqlserver_opsignal DetectorResult.

    Returns None when the detector_id is not in this pack (caller should
    fall through to the next scorer).
    """
    entry = _SQLSERVER_SCORES.get(dr.detector_id)
    if entry is None:
        return None

    return {
        "detector_id": dr.detector_id,
        "tier": entry["tier"],
        "impact": entry["impact"],
        "effort": entry["effort"],
        "confidence": entry["confidence"],
        "roadmap_stage": entry["roadmap_stage"],
        "corroborated": False,
        "corroboration_sources": [],
        "score_debug": {
            "pack": "sqlserver_opsignal",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
        },
    }
