"""Canonical scorer for the Enterprise Operations Intelligence Pack (ENT-5).

T5 (AT-266) wires two confidence-elevation paths:
  ENT_INCIDENT_RESOLUTION_LAG  — MEDIUM → HIGH when COR-06 Slack escalation fires
                                  in the same 30-day window (ENT-2 corroboration).
  ENT_SLA_BREACH_BY_TEAM       — MEDIUM → HIGH when the top team resolves to a Team
                                  entity via ENT-1 overlay AND Jira backlog ≥ 20 issues
                                  (result already computed by the detector and stored in
                                  raw_evidence; the scorer just reads it).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:
    from discovery.models import DetectorResult

logger = logging.getLogger(__name__)

_ENTERPRISE_OPS_SCORES: Dict[str, Dict[str, Any]] = {
    "ENT_INCIDENT_RESOLUTION_LAG": {
        "tier": "Strategic",
        "impact": 7,
        "effort": 3,
        "confidence": "MEDIUM",
        "roadmap_stage": "strategic",
    },
    "ENT_CHANGE_INCIDENT_CORRELATION": {
        "tier": "Strategic",
        "impact": 8,
        "effort": 3,
        "confidence": "HIGH",
        "roadmap_stage": "strategic",
    },
    "ENT_SLA_BREACH_BY_TEAM": {
        "tier": "Quick Win",
        "impact": 7,
        "effort": 2,
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

_EFFORT_LABEL: Dict[int, str] = {2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}

_CONFIDENCE_NOTES: Dict[str, str] = {
    "ENT_INCIDENT_RESOLUTION_LAG": (
        "MEDIUM standalone. Elevates to HIGH when Slack escalation pattern "
        "(COR-06) also fires in the same window via ENT-2 corroboration engine."
    ),
    "ENT_CHANGE_INCIDENT_CORRELATION": (
        "HIGH — post-change incident ratio >= 2.0 is a strong single-system "
        "signal. No external corroboration required."
    ),
    "ENT_SLA_BREACH_BY_TEAM": (
        "MEDIUM standalone. Elevates to HIGH when the top team resolves to a "
        "Team entity in the knowledge graph and Jira also shows high open issue "
        "count for that team (ENT-1 entity overlay)."
    ),
}


_COR06_DETECTOR = "ENT_INCIDENT_RESOLUTION_LAG"
_SLA_TEAM_DETECTOR = "ENT_SLA_BREACH_BY_TEAM"


def _cor06_fired(
    sn_data: Optional[Dict[str, Any]],
    org_id: Optional[str] = None,
) -> bool:
    """Return True when the COR-06 Slack escalation pattern fired in the same 30-day window.

    Reads sn_data["cor06_slack_escalation"]["fired"].  An org_id mismatch blocks
    elevation so one org's Slack escalations cannot inflate another org's confidence.
    """
    cor06 = (sn_data or {}).get("cor06_slack_escalation") or {}
    if not isinstance(cor06, dict):
        return False
    # Org-scope guard: prevent cross-org elevation (mirrors GitHub AT-191 pattern).
    if org_id and cor06.get("org_id") and cor06.get("org_id") != org_id:
        return False
    return bool(cor06.get("fired", False))


def is_enterprise_ops_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the Enterprise Operations pack."""
    return detector_id in _ENTERPRISE_OPS_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return a copy of the score table entry for detector_id."""
    return dict(_ENTERPRISE_OPS_SCORES.get(detector_id, _DEFAULT_SCORE))


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a score dict plus the detector metric value."""
    score = get_score(detector_id)
    score["metric_value"] = metric_value
    return score


def score_enterprise_ops(
    dr: DetectorResult,
    *,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
    org_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Score an Enterprise Operations Intelligence Pack DetectorResult.

    All corroboration arguments are keyword-only and default to None, so existing
    callers ``score_enterprise_ops(dr)`` remain backward-compatible and simply
    keep the base confidence.

    ENT_INCIDENT_RESOLUTION_LAG: confidence elevates MEDIUM → HIGH when COR-06
    Slack escalation fires in the same window (ENT-2, AT-266 T5).

    ENT_SLA_BREACH_BY_TEAM: confidence elevates MEDIUM → HIGH when the ENT-1
    entity overlay resolved the top team AND Jira backlog >= 20 issues.  The
    detector already computed this and stored the result in raw_evidence.

    Unknown detector IDs receive safe default scoring instead of falling through
    silently.
    """
    known = is_enterprise_ops_detector(dr.detector_id)
    score = get_score(dr.detector_id)

    if not known:
        logger.warning(
            "score_enterprise_ops: unknown detector '%s' - returning default score. "
            "Check pack_config.py detector list.",
            dr.detector_id,
        )

    confidence: str = score["confidence"]
    corroborated: bool = False
    corroboration_sources: List[str] = []
    confidence_note: str = _CONFIDENCE_NOTES.get(
        dr.detector_id,
        "MEDIUM - cross-system signal. Check ENT-2 corroboration engine for elevation.",
    )

    # ── ENT-2: COR-06 Slack escalation wiring ────────────────────────────────
    if dr.detector_id == _COR06_DETECTOR and _cor06_fired(sn_data, org_id):
        confidence = "HIGH"
        corroborated = True
        corroboration_sources = ["Slack"]
        confidence_note = (
            "HIGH — elevated by COR-06 Slack escalation pattern (ENT-2): "
            "Slack escalation activity corroborates the incident-issue resolution "
            "gap in the same 30-day window."
        )
        logger.info(
            "%s: confidence MEDIUM->HIGH via COR-06 Slack escalation (org=%s)",
            dr.detector_id,
            org_id,
        )

    # ── ENT-1: entity graph wiring for SLA breach team ───────────────────────
    elif dr.detector_id == _SLA_TEAM_DETECTOR:
        ent1_confidence = dr.raw_evidence.get("confidence", "MEDIUM")
        if ent1_confidence == "HIGH":
            confidence = "HIGH"
            corroborated = True
            corroboration_sources = ["Jira"]
            match_strategy = dr.raw_evidence.get("match_strategy", "entity_graph")
            top_team = dr.raw_evidence.get("top_team_name", "")
            open_issues = dr.raw_evidence.get("top_team_jira_open_issues", 0)
            confidence_note = (
                f"HIGH — ENT-1 entity overlay resolved '{top_team}' to a Team entity "
                f"({match_strategy}) and Jira shows {open_issues} open issues for that "
                "team (>= 20 threshold)."
            )
            logger.info(
                "%s: confidence MEDIUM->HIGH via ENT-1 entity overlay "
                "(team=%s strategy=%s open_issues=%s org=%s)",
                dr.detector_id,
                top_team,
                match_strategy,
                open_issues,
                org_id,
            )

    return {
        "tier": score["tier"],
        "impact": score["impact"],
        "effort": score["effort"],
        "effort_label": _EFFORT_LABEL.get(score["effort"], "Low"),
        "confidence": confidence,
        "roadmap_stage": score["roadmap_stage"],
        "corroborated": corroborated,
        "corroboration_sources": corroboration_sources,
        "score_debug": {
            "detector_id": dr.detector_id,
            "scorer": "enterprise_ops",
            "pack": "enterprise_ops",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact": score["impact"],
            "final_impact": score["impact"],
            "base_confidence": score["confidence"],
            "confidence_note": confidence_note,
            **({"note": "unknown detector - default score applied"} if not known else {}),
        },
    }
