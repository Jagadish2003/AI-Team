"""Canonical scorer for the SQL Server Operational Signal Pack."""
from __future__ import annotations

import logging
from typing import Any, Dict

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:
    from discovery.models import DetectorResult

logger = logging.getLogger(__name__)

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

_EFFORT_LABEL: Dict[int, str] = {2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}


def is_sqlserver_opsignal_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the SQL Server opsignal pack."""
    return detector_id in _SQLSERVER_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return a copy of the score table entry for detector_id."""
    return dict(_SQLSERVER_SCORES.get(detector_id, _DEFAULT_SCORE))


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a score dict plus the detector metric value."""
    score = get_score(detector_id)
    score["metric_value"] = metric_value
    return score


def score_sqlserver_opsignal(dr: DetectorResult) -> Dict[str, Any]:
    """Score a SQL Server operational signal DetectorResult.

    Unknown detector IDs receive safe default scoring instead of falling
    through silently. This keeps runner wiring deterministic and makes config
    mistakes visible in logs.
    """
    known = is_sqlserver_opsignal_detector(dr.detector_id)
    score = get_score(dr.detector_id)

    if not known:
        logger.warning(
            "score_sqlserver_opsignal: unknown detector '%s' - returning default score. "
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
            "scorer": "sqlserver_opsignal",
            "pack": "sqlserver_opsignal",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact": score["impact"],
            "final_impact": score["impact"],
            "confidence_note": (
                "MEDIUM - DB-only signal. Elevates to HIGH after T2-S16-A "
                "cross-system corroboration."
            ),
            **({"note": "unknown detector - default score applied"} if not known else {}),
        },
    }
