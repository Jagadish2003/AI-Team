"""
ENG-AIQ-NC-4 — Lending Detector Scoring
Sprint 5 — Wave 4

Provides lending-specific scoring for nCino detectors.
SF-NC-5 confirmed scoring values — May 2026.

This scorer is used when pack='ncino'. The Service Cloud scorer
(discovery/scorer.py) remains unchanged for pack='service_cloud'.

Scoring per detector (SF-NC-5 defaults — SME confirmation pending):
  LOAN_ORIGINATION_ROUTING_FRICTION: Quick Win,  impact=6, effort=Low,  confidence=HIGH
  COVENANT_TRACKING_GAP:             Strategic,  impact=8, effort=Med,  confidence=HIGH
    compliance_override (Breached):  Strategic,  impact=9, effort=Med,  confidence=HIGH
  CHECKLIST_BOTTLENECK:              Quick Win,  impact=7, effort=Low,  confidence=HIGH
  SPREADING_BOTTLENECK:              Strategic,  impact=7, effort=Med,  confidence=MEDIUM
  APPROVAL_BOTTLENECK:               Strategic,  impact=8, effort=Med,  confidence=HIGH

Effort mapping: Low=2, Medium=4, High=7 (aligns with Service Cloud scorer scale).
"""
from __future__ import annotations

from typing import Any, Dict

from .models import DetectorResult

# ── SF-NC-5 confirmed scoring table ──────────────────────────────────────────
# Keys match detector_id values exactly.
# compliance_override_impact applied when Breached__c = true on covenant records.

_LENDING_SCORES: Dict[str, Dict[str, Any]] = {
    "LOAN_ORIGINATION_ROUTING_FRICTION": {
        "tier":       "Quick Win",
        "impact":     7,           # SF-NC-5 confirmed: upgraded from 6 (SME: 6-7 transitions extend cycle)
        "effort":     2,           # Low effort
        "confidence": "HIGH",
        "roadmap_stage": "quick_win",
    },
    "COVENANT_TRACKING_GAP": {
        "tier":       "Strategic",
        "impact":     9,           # SF-NC-5 confirmed: base impact 9 (breached covenants = regulatory)
        "effort":     4,           # Medium effort
        "confidence": "HIGH",
        "roadmap_stage": "strategic",
        "compliance_override_impact": 9,  # Redundant but explicit — breach always = 9
    },
    "CHECKLIST_BOTTLENECK": {
        "tier":       "Quick Win",
        "impact":     7,           # SF-NC-5 confirmed: unchanged
        "effort":     2,           # Low effort
        "confidence": "HIGH",
        "roadmap_stage": "quick_win",
    },
    "SPREADING_BOTTLENECK": {
        "tier":       "Strategic",
        "impact":     7,           # SF-NC-5 confirmed: unchanged
        "effort":     4,           # Medium effort
        "confidence": "HIGH",      # SF-NC-5 confirmed: upgraded from MEDIUM (11 unlocked + 4-backlog analyst)
        "roadmap_stage": "strategic",
    },
    "APPROVAL_BOTTLENECK": {
        "tier":       "Strategic",
        "impact":     8,           # SF-NC-5 confirmed: unchanged
        "effort":     4,           # Medium effort
        "confidence": "HIGH",
        "roadmap_stage": "strategic",
    },
}

_EFFORT_LABEL = {2: "Low", 4: "Medium", 7: "High"}


def score_lending(dr: DetectorResult) -> Dict[str, Any]:
    """
    Score a nCino lending DetectorResult using SF-NC-5 confirmed values.

    Compliance override: if COVENANT_TRACKING_GAP fires with
    compliance_override=True (Breached__c=true), impact is forced to 9.
    This is non-negotiable — a regulatory rule, not a scoring preference.

    Returns same shape as discovery/scorer.score() for compatibility.
    """
    base = _LENDING_SCORES.get(dr.detector_id)

    if base is None:
        # Unknown lending detector — fall back to Service Cloud scorer.
        # In ncino pack this likely means a config bug — log it explicitly.
        import logging
        logging.getLogger(__name__).warning(
            "score_lending: unknown detector '%s' — falling back to SC scorer. "
            "Check pack_config.py detector list.", dr.detector_id
        )
        from .scorer import score as sc_score
        return sc_score(dr)

    impact     = base["impact"]
    effort     = base["effort"]
    confidence = base["confidence"]
    tier       = base["tier"]
    roadmap    = base["roadmap_stage"]

    # Compliance override — Breached__c = true forces impact to 9
    compliance_override = bool(
        dr.raw_evidence.get("compliance_override", False)
        or dr.raw_evidence.get("breached_count", 0) > 0
    )
    if compliance_override and "compliance_override_impact" in base:
        impact = base["compliance_override_impact"]
        tier   = "Strategic"

    return {
        "impact":        impact,
        "effort":        effort,
        "effort_label":  _EFFORT_LABEL.get(effort, "Medium"),
        "confidence":    confidence,
        "tier":          tier,
        "roadmap_stage": roadmap,
        "compliance_override": compliance_override,
        "score_debug": {
            "detector_id":          dr.detector_id,
            "scorer":               "lending",
            "base_impact":          base["impact"],
            "final_impact":         impact,
            "compliance_override":  compliance_override,
        },
    }


def is_lending_detector(detector_id: str) -> bool:
    """Return True if this detector_id is a known lending detector."""
    return detector_id in _LENDING_SCORES
