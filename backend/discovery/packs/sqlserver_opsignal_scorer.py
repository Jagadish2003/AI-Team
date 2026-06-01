"""
T2-S11-A  |  SQL Server Operational Signal Pack — Scorer
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Scoring values for all three SQL Server operational signal detectors.
Confidence is MEDIUM for DB-only signals — a single database is a weaker
signal source than a corroborated cross-system finding.  Confidence
elevates to HIGH when T2-S16-A normalisation layer enables cross-system
corroboration with ServiceNow and Jira findings.

Doc reference: T2-S11-A Section 2e.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ── Score table (doc reference: T2-S11-A Section 2e) ─────────────────────────

_SQLSERVER_SCORES: Dict[str, Dict[str, Any]] = {
    "DB_TICKET_VOLUME_SURGE": {
        "tier":          "Quick Win",
        "impact":        6,
        "effort":        2,
        "confidence":    "MEDIUM",    # DB-only; elevates to HIGH after T2-S16-A
        "roadmap_stage": "quick_win",
    },
    "DB_SLA_BREACH_RATE": {
        "tier":          "Quick Win",
        "impact":        7,
        "effort":        2,
        "confidence":    "MEDIUM",
        "roadmap_stage": "quick_win",
    },
    "DB_QUEUE_DEPTH_ELEVATED": {
        "tier":          "Strategic",
        "impact":        8,
        "effort":        3,
        "confidence":    "MEDIUM",
        "roadmap_stage": "strategic",
    },
}

_DEFAULT_SCORE: Dict[str, Any] = {
    "tier":          "Quick Win",
    "impact":        5,
    "effort":        2,
    "confidence":    "MEDIUM",
    "roadmap_stage": "quick_win",
}


# ── Public API ────────────────────────────────────────────────────────────────


def is_sqlserver_opsignal_detector(detector_id: str) -> bool:
    """Return True when *detector_id* belongs to the SQL Server opsignal pack."""
    return detector_id in _SQLSERVER_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return the scoring dict for *detector_id*.

    Falls back to _DEFAULT_SCORE for unknown detectors so the scorer never
    raises when a new detector is added before this file is updated.
    """
    return _SQLSERVER_SCORES.get(detector_id, _DEFAULT_SCORE)


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a complete score dict for an opportunity.

    Parameters
    ----------
    detector_id:
        One of DB_TICKET_VOLUME_SURGE, DB_SLA_BREACH_RATE,
        DB_QUEUE_DEPTH_ELEVATED.
    metric_value:
        The raw metric value from the detector (e.g. ratio, pct, count).
        Included in the returned dict for downstream consumption.

    Returns
    -------
    dict
        Keys: tier, impact, effort, confidence, roadmap_stage, metric_value.
    """
    base = dict(get_score(detector_id))
    base["metric_value"] = metric_value
    return base
