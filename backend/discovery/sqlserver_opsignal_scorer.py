"""
T2-S11-A — SQL Server Operational Signal Pack Scorer
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Provides scoring for the three SQL Server operational signal detectors.

Scoring values (doc reference: T2-S11-A Section 2e):
  DB_TICKET_VOLUME_SURGE    Quick Win,  impact=6, effort=2, confidence=MEDIUM
  DB_SLA_BREACH_RATE        Quick Win,  impact=7, effort=2, confidence=MEDIUM
  DB_QUEUE_DEPTH_ELEVATED   Strategic,  impact=8, effort=3, confidence=MEDIUM

Confidence design note
----------------------
All three detectors start at MEDIUM because they are DB-only signals.
A single database source is a weaker signal than a corroborated cross-system
finding.  Confidence will elevate to HIGH when T2-S16-A normalisation layer
enables cross-system corroboration with ServiceNow and Jira findings.
This is intentional — do not change these to HIGH until T2-S16-A lands.

Return shape compatibility
--------------------------
score_sqlserver_opsignal() returns the same field set as score_strs_benefits()
and score_lending() so SQL Server opportunities fit into the existing
AgentIQ opportunity and roadmap flow without changes to the runner,
materialize_t2, or UI layers.

Effort mapping (consistent with other pack scorers):
  2 → Low    (quick configuration or monitoring change)
  3 → Low-Med
  4 → Medium (some cross-team coordination required)
  7 → High   (significant system change)
"""

from __future__ import annotations

from typing import Any, Dict

from .models import DetectorResult

# ── Scoring table (doc: T2-S11-A Section 2e) ──────────────────────────────────

_SQLSERVER_SCORES: Dict[str, Dict[str, Any]] = {
    "DB_TICKET_VOLUME_SURGE": {
        "tier":          "Quick Win",
        "impact":        6,
        "effort":        2,           # Low — monitoring agent, no system change
        "confidence":    "MEDIUM",    # DB-only; elevates to HIGH after T2-S16-A
        "roadmap_stage": "quick_win",
    },
    "DB_SLA_BREACH_RATE": {
        "tier":          "Quick Win",
        "impact":        7,
        "effort":        2,           # Low — process + alert change
        "confidence":    "MEDIUM",
        "roadmap_stage": "quick_win",
    },
    "DB_QUEUE_DEPTH_ELEVATED": {
        "tier":          "Strategic",
        "impact":        8,
        "effort":        3,           # Low-Med — escalation + triaging workflow
        "confidence":    "MEDIUM",
        "roadmap_stage": "strategic",
    },
}

_SQLSERVER_DETECTOR_IDS: frozenset[str] = frozenset(_SQLSERVER_SCORES.keys())

_EFFORT_LABEL: Dict[int, str] = {2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}

_DEFAULT_SCORE: Dict[str, Any] = {
    "tier":          "Quick Win",
    "impact":        5,
    "effort":        2,
    "confidence":    "MEDIUM",
    "roadmap_stage": "quick_win",
}


# ── Public helpers ─────────────────────────────────────────────────────────────


def is_sqlserver_opsignal_detector(detector_id: str) -> bool:
    """Return True when *detector_id* belongs to the SQL Server opsignal pack.

    Used in runner.py and evidence_builder for pack routing — same pattern as
    is_lending_detector() and is_strs_benefits_detector().

    Parameters
    ----------
    detector_id:
        The detector_id string from a DetectorResult.  Case-sensitive.

    Returns
    -------
    bool
        True for DB_TICKET_VOLUME_SURGE, DB_SLA_BREACH_RATE,
        DB_QUEUE_DEPTH_ELEVATED.  False for all other detector IDs.
    """
    return detector_id in _SQLSERVER_DETECTOR_IDS


# ── Main scoring function ──────────────────────────────────────────────────────


def score_sqlserver_opsignal(dr: DetectorResult) -> Dict[str, Any]:
    """Score a SQL Server operational signal DetectorResult.

    Returns the same field set as score_strs_benefits() and score_lending()
    so SQL Server opportunities slot into the existing opportunity and roadmap
    flow without changes to the runner, materialize_t2, or UI.

    Parameters
    ----------
    dr:
        A fired DetectorResult from one of the three SQL Server detectors.

    Returns
    -------
    dict
        Keys: tier, impact, effort, effort_label, confidence, roadmap_stage,
        score_debug.

    Notes
    -----
    * Confidence starts at MEDIUM for all DB-only signals.
    * No compliance override logic — SQL Server operational signals do not
      carry regulatory obligations (unlike STRS or nCino covenant signals).
    * Cross-system corroboration logic (ServiceNow / Jira) is planned for
      T2-S16-A.  When it lands, add an elevation step here following the
      same pattern as ENG-STRS-CORR-3.
    """
    base = _SQLSERVER_SCORES.get(dr.detector_id)

    if base is None:
        # Unknown detector ID passed with this scorer — return safe defaults
        # and log a warning so config bugs are surfaced in test output.
        import logging
        logging.getLogger(__name__).warning(
            "score_sqlserver_opsignal: unknown detector '%s' — returning default score. "
            "Check pack_config.py detector list.",
            dr.detector_id,
        )
        return {
            "tier":          _DEFAULT_SCORE["tier"],
            "impact":        _DEFAULT_SCORE["impact"],
            "effort":        _DEFAULT_SCORE["effort"],
            "effort_label":  _EFFORT_LABEL.get(_DEFAULT_SCORE["effort"], "Low"),
            "confidence":    _DEFAULT_SCORE["confidence"],
            "roadmap_stage": _DEFAULT_SCORE["roadmap_stage"],
            "score_debug": {
                "detector_id": dr.detector_id,
                "scorer":      "sqlserver_opsignal",
                "note":        "unknown detector — default score applied",
            },
        }

    impact     = base["impact"]
    effort     = base["effort"]
    confidence = base["confidence"]
    tier       = base["tier"]
    roadmap    = base["roadmap_stage"]

    # ── Future hook: T2-S16-A cross-system corroboration ─────────────────────
    # When ServiceNow or Jira corroborate the same pattern, elevate confidence
    # from MEDIUM to HIGH here.  Pattern to follow:
    #
    #   jira_corroborated = bool(dr.raw_evidence.get("jira_corroborated", False))
    #   sn_corroborated   = bool(dr.raw_evidence.get("sn_corroborated", False))
    #   if jira_corroborated or sn_corroborated:
    #       confidence = "HIGH"
    #
    # Do NOT add this until T2-S16-A lands — premature elevation defeats the
    # purpose of MEDIUM as a meaningful signal quality indicator.

    return {
        "tier":          tier,
        "impact":        impact,
        "effort":        effort,
        "effort_label":  _EFFORT_LABEL.get(effort, "Low"),
        "confidence":    confidence,
        "roadmap_stage": roadmap,
        "score_debug": {
            "detector_id": dr.detector_id,
            "scorer":      "sqlserver_opsignal",
            "base_impact": impact,
            "final_impact": impact,
            "confidence_note": (
                "MEDIUM — DB-only signal. "
                "Elevates to HIGH after T2-S16-A cross-system corroboration."
            ),
        },
    }
