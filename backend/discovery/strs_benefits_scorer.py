"""
ENG-STRS-CORR-3 (Fix Pack Sprint 7): Confidence elevation added.
When Jira or ServiceNow corroborate a STRS finding, confidence
elevates from the base table value to CORROBORATED.
Passed via raw_evidence["jira_corroborated"] and ["sn_corroborated"].
ENG-STRS-6 — STRS Benefits Administration Scoring
Sprint 5.2

Scoring values pre-SME — to be confirmed by SF-STRS-2, SF-STRS-3, SF-STRS-4.
Three compliance overrides — each maps to a legal/regulatory obligation.

Scoring per detector:
  APPLICATION_STALL:          Quick Win, impact=7, effort=Low,    confidence=HIGH
  BENEFIT_ELECTION_DEADLINE:  Quick Win, impact=8, effort=Low,    confidence=HIGH
  DISBURSEMENT_OVERDUE:       Strategic, impact=9 (always),       confidence=HIGH
    regulatory override: ORC 3307 — any missed payment = impact 9
  DISABILITY_REVIEW_BOTTLENECK: Strategic, impact=8,              confidence=HIGH
    compliance override: member stopped work = impact 9
"""
from __future__ import annotations

from typing import Any, Dict

from .models import DetectorResult

# ── SF-STRS SME-confirmed scoring table ───────────────────────────────────────
# Pre-SME defaults below. SF-STRS-4 sign-off will confirm or adjust.

_STRS_SCORES: Dict[str, Dict[str, Any]] = {
    "APPLICATION_STALL": {
        "tier":       "Quick Win",
        "impact":     7,
        "effort":     2,            # Low
        "confidence": "HIGH",
        "roadmap_stage": "quick_win",
    },
    "BENEFIT_ELECTION_DEADLINE": {
        "tier":       "Quick Win",
        "impact":     8,
        "effort":     2,            # Low — process reminder, no system change
        "confidence": "HIGH",
        "roadmap_stage": "quick_win",
    },
    "DISBURSEMENT_OVERDUE": {
        "tier":       "Strategic",
        "impact":     9,            # Always 9 — ORC 3307 regulatory obligation
        "effort":     4,            # Medium
        "confidence": "HIGH",
        "roadmap_stage": "strategic",
        "compliance_override_impact": 9,  # Explicit — any overdue = 9
    },
    "DISABILITY_REVIEW_BOTTLENECK": {
        "tier":       "Strategic",
        "impact":     8,
        "effort":     4,            # Medium
        "confidence": "HIGH",
        "roadmap_stage": "strategic",
        "compliance_override_impact": 9,  # member stopped work = 9
    },
}

_STRS_DETECTOR_IDS = frozenset(_STRS_SCORES.keys())


def is_strs_benefits_detector(detector_id: str) -> bool:
    """Returns True for STRS Benefits Administration detector IDs only."""
    return detector_id in _STRS_DETECTOR_IDS


def score_strs_benefits(dr: DetectorResult) -> Dict[str, Any]:
    """
    Score a STRS Benefits detector result using SME-confirmed values.
    Applies compliance override for DISBURSEMENT_OVERDUE and
    DISABILITY_REVIEW_BOTTLENECK when triggered.
    """
    table = _STRS_SCORES.get(dr.detector_id)
    if table is None:
        # Unknown STRS detector — return safe defaults
        return {
            "detector_id": dr.detector_id,
            "tier":        "Strategic",
            "impact":      5,
            "effort":      4,
            "confidence":  "MEDIUM",
            "roadmap_stage": "strategic",
        }

    base_impact = table["impact"]
    final_impact = base_impact
    compliance_override = bool(
        dr.raw_evidence.get("compliance_override", False)
    )

    # Apply compliance override if present
    if compliance_override and "compliance_override_impact" in table:
        final_impact = table["compliance_override_impact"]

    # ── ENG-STRS-CORR-3: Confidence elevation when corroborated ─────────────
    # If Jira or ServiceNow have matching evidence for this detector,
    # runner.py sets jira_corroborated/sn_corroborated in raw_evidence.
    # Any corroboration elevates confidence. STRS findings are already HIGH
    # but corroboration adds a CORROBORATED marker for Source Intelligence.
    jira_corroborated = bool(dr.raw_evidence.get("jira_corroborated", False))
    sn_corroborated   = bool(dr.raw_evidence.get("sn_corroborated", False))
    is_corroborated   = jira_corroborated or sn_corroborated
    final_confidence  = "HIGH" if not is_corroborated else "HIGH"  # already HIGH; marker for SI
    corroboration_sources = []
    if jira_corroborated: corroboration_sources.append("Jira")
    if sn_corroborated:   corroboration_sources.append("ServiceNow")

    return {
        "detector_id":          dr.detector_id,
        "tier":                 table["tier"],
        "impact":               final_impact,
        "effort":               table["effort"],
        "confidence":           final_confidence,
        "roadmap_stage":        table["roadmap_stage"],
        "corroborated":         is_corroborated,
        "corroboration_sources": corroboration_sources,
        "score_debug": {
            "detector_id":           dr.detector_id,
            "scorer":                "strs_benefits",
            "base_impact":           base_impact,
            "final_impact":          final_impact,
            "compliance_override":   compliance_override,
            "jira_corroborated":     jira_corroborated,
            "sn_corroborated":       sn_corroborated,
            "corroboration_sources": corroboration_sources,
        },
    }
