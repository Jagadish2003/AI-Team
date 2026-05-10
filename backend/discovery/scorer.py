"""
SF-2.6 — Scorer  (patched: T41-6 Scorer Recalibration)

Converts a DetectorResult into four scored outputs:
    Impact      (1–10, integer)
    Effort      (1–10, integer)
    Confidence  (HIGH | MEDIUM | LOW)
    Tier        (Quick Win | Strategic | Complex)

Rules: SF-1.4 scoring rubric v1.0 (Final).
This is a pure function — no DB, no network, no side effects.
Same input always produces same output.

T41-6 CHANGE LOG
----------------
Root cause: weighted-sum formula has a mathematical ceiling of ~6.1 (max possible
raw_sum across all detectors and pts bands).  Round(raw_sum) therefore compresses
all live scores into the 3–4 band regardless of organisation scale.

Fix: add a post-sum linear rescaling step inside _compute_impact() that maps
the full theoretical raw range [_RAW_IMPACT_MIN, _RAW_IMPACT_MAX] onto the full
target range [1, 10].  All factor derivation and weight logic is unchanged.

Constants introduced (all named, all testable):
    _RAW_IMPACT_MIN  — minimum possible weighted sum (near-empty org, no friction)
    _RAW_IMPACT_MAX  — maximum possible weighted sum (all factors at ceiling)

The rescaled value is clamped to [1, 10] and rounded to int — identical contract
to the original function, only the internal mapping changes.

No other functions are modified.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Literal, Tuple

from .models import DetectorResult

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
Tier       = Literal["Quick Win", "Strategic", "Complex"]


# ─────────────────────────────────────────────────────────────────────────────
# Impact factor weights  (SF-1.4 Section 3)
# ─────────────────────────────────────────────────────────────────────────────

_W_VOLUME      = 0.25   # reduced: volume was over-dominating at low org scale
_W_FRICTION    = 0.30   # increased: pain signal matters more than raw count
_W_CUSTOMER    = 0.20
_W_REVENUE     = 0.15
_W_EXTERNAL    = 0.10


# ─────────────────────────────────────────────────────────────────────────────
# T41-6: Impact rescaling constants
#
# _RAW_IMPACT_MIN: minimum weighted sum — near-empty org (volume<10 → 2pts),
#   no friction (2pts), no customer/revenue signals, 1 external pt.
#   = 2*0.25 + 2*0.30 + 0*0.20 + 0*0.15 + 1*0.10 = 1.20
#
# _RAW_IMPACT_MAX: maximum weighted sum — volume ≥500 (9pts), max friction (8pts),
#   customer-facing (3pts), revenue touch (2pts), multi-system external (3pts).
#   = 9*0.25 + 8*0.30 + 3*0.20 + 2*0.15 + 3*0.10 = 2.25+2.40+0.60+0.30+0.30 = 5.85
#   Using 6.1 as the ceiling to account for floating point and future band changes.
#
# These constants must be updated if _W_* weights or pts band ceilings change.
# ─────────────────────────────────────────────────────────────────────────────

_RAW_IMPACT_MIN: float = 1.20   # T41-6
_RAW_IMPACT_MAX: float = 6.10   # T41-6


def _rescale_impact(raw: float) -> int:
    """
    T41-6: Linear rescale raw weighted sum from [_RAW_IMPACT_MIN, _RAW_IMPACT_MAX]
    onto [1, 10].  Clamp then round — identical output contract to original round().

    Formula:
        scaled = (raw - MIN) / (MAX - MIN) * 9 + 1
    """
    span = _RAW_IMPACT_MAX - _RAW_IMPACT_MIN
    scaled = (raw - _RAW_IMPACT_MIN) / span * 9.0 + 1.0
    return max(1, min(10, round(scaled)))


def _volume_pts(weekly_rate: float) -> float:
    """ Maps weekly record volume to impact points.    Bands calibrated to real org scale (SF-3.2 live calibration, April 2026).    OVERFIT GUARD: validate at 200/week before applying — should score 6.5, not 9.0.
    """
    if weekly_rate < 10:   return 2.0   # genuinely tiny / empty org
    if weekly_rate < 30:   return 3.5   # dev org / sandbox range (was: < 50 → 2.0)
    if weekly_rate < 75:   return 5.0   # small enterprise
    if weekly_rate < 200:  return 6.5   # mid-size enterprise
    if weekly_rate < 500:  return 8.0   # large enterprise
    return 9.0                          # very high volume

def _friction_pts_delay(days: float) -> float:
    """Maps avg_delay_days → friction score."""
    if days < 1:   return 2.0
    if days <= 3:  return 5.0
    return 8.0


def _friction_pts_handoff(score: float) -> float:
    """Maps handoff_score → friction score."""
    if score < 1.5:   return 2.0
    if score <= 2.5:  return 5.0
    return 8.0


def _friction_pts_gap(score: float) -> float:
    """Maps knowledge_gap_score → friction score."""
    if score < 0.4:   return 2.0
    if score <= 0.6:  return 5.0
    return 8.0


def _friction_pts_bottleneck(score: float) -> float:
    """Maps bottleneck_score → friction score."""
    if score <= 10:   return 2.0
    if score <= 20:   return 5.0
    return 8.0


def _friction_pts_echo(score: float) -> float:
    """Maps echo_score → friction score."""
    if score < 0.15:   return 2.0
    if score <= 0.30:  return 5.0
    return 8.0


def _friction_pts_element_count(avg_elements: float) -> float:
    """Maps flow avg_element_count → friction score (inverse — low complexity = low friction)."""
    if avg_elements < 10:   return 2.0
    if avg_elements <= 20:  return 5.0
    return 8.0


# ─────────────────────────────────────────────────────────────────────────────
# Per-detector Impact factor derivation  (SF-1.4 Section 3 table)
# ─────────────────────────────────────────────────────────────────────────────

def _impact_factors(dr: DetectorResult) -> Tuple[float, float, float, float, float]:
    """
    Return (volume_pts, friction_pts, customer_pts, revenue_pts, external_pts)
    derived from DetectorResult raw_evidence per SF-1.4 mapping table.
    """
    ev = dr.raw_evidence
    did = dr.detector_id

    if did == "REPETITIVE_AUTOMATION":
        records_90d = float(ev.get("records_90d", 0))
        weekly = records_90d / 13.0
        volume   = _volume_pts(weekly)
        # SF-1.4: "flow_activity_score 2.128 → high repetition → 5pts"
        # activity_score maps to the delay/friction band — not element_count
        act_score = float(ev.get("flow_activity_score", 0))
        friction = 2.0 if act_score < 0.8 else (5.0 if act_score <= 2.5 else 8.0)
        customer = 3.0 if ev.get("trigger_object") == "Case" else 0.0
        revenue  = 2.0  # Case management has compliance touch (SLA)
        external = 1.0  # 0 external systems → 1 pt

    elif did == "HANDOFF_FRICTION":
        total = float(ev.get("total_cases_90d", 0))
        weekly = total / 13.0
        volume   = _volume_pts(weekly)
        friction = _friction_pts_handoff(float(ev.get("handoff_score", 0)))
        customer = 3.0  # Case object is always customer-facing
        revenue  = 0.0  # Support process — neither
        external = 1.0  # 0 external systems

    elif did == "APPROVAL_BOTTLENECK":
        pending = float(ev.get("pending_count", 0))
        volume   = _volume_pts(pending / 13.0)  # SF-1.4: pending/13 = weekly rate
        friction = _friction_pts_delay(float(ev.get("avg_delay_days", 0)))
        customer = 0.0  # Internal approval process
        revenue  = 2.0  # Approval = compliance touch
        external = 1.0

    elif did == "KNOWLEDGE_GAP":
        closed = float(ev.get("closed_cases_90d", 0))
        weekly = closed / 13.0
        volume   = _volume_pts(weekly)
        friction = _friction_pts_gap(float(ev.get("knowledge_gap_score", 0)))
        customer = 3.0  # Case object is customer-facing
        revenue  = 0.0
        external = 1.0

    elif did == "INTEGRATION_CONCENTRATION":
        ref_count = float(ev.get("flow_reference_count", 0))
        volume   = _volume_pts(ref_count)          # flow ref count as proxy
        friction = 2.0                              # latency risk, not direct delay
        customer = 0.0                              # integration layer
        revenue  = 0.0
        external = 3.0                              # 2+ systems → 3 pts

    elif did == "PERMISSION_BOTTLENECK":
        pending = float(ev.get("pending_count", 0))
        volume   = _volume_pts(pending / 13.0)  # SF-1.4: pending/13 = weekly rate
        friction = _friction_pts_bottleneck(float(ev.get("bottleneck_score", 0)))
        customer = 0.0   # Internal
        revenue  = 2.0   # Approval = compliance touch
        external = 1.0

    elif did == "CROSS_SYSTEM_ECHO":
        total_cases = float(ev.get("sf_total_cases", 0))
        weekly = total_cases / 13.0
        volume   = _volume_pts(weekly)
        max_echo = max(
            float(ev.get("sf_echo_score", 0)),
            float(ev.get("sn_echo_score", 0)),
            float(ev.get("jira_echo_score", 0) if "jira_echo_score" in ev else 0),
        )
        friction = _friction_pts_echo(max_echo)
        customer = 0.0   # Ops / integration process
        revenue  = 0.0
        external = 3.0   # SF + SN/Jira = 2+ systems

    else:
        # Unknown detector — conservative defaults
        volume = friction = customer = revenue = external = 2.0

    return volume, friction, customer, revenue, external


def _compute_impact(dr: DetectorResult) -> int:
    """
    Compute Impact score per SF-1.4 formula.

    T41-6 patch: apply _rescale_impact() instead of direct round() to map
    the full theoretical raw range onto [1, 10].  Factor derivation is unchanged.
    """
    v, f, c, r, e = _impact_factors(dr)
    raw = (v * _W_VOLUME + f * _W_FRICTION + c * _W_CUSTOMER +
           r * _W_REVENUE + e * _W_EXTERNAL)
    return _rescale_impact(raw)  # T41-6: was max(1, min(10, round(raw)))


# ─────────────────────────────────────────────────────────────────────────────
# Effort scoring  (SF-1.4 Section 4)
# ─────────────────────────────────────────────────────────────────────────────

_W_DATA   = 0.30
_W_PERM   = 0.25
_W_SYS    = 0.25
_W_PROC   = 0.20

# Permission scope counts per detector (SF-1.4 mapping table)
_PERMISSION_SCOPES: Dict[str, int] = {
    "REPETITIVE_AUTOMATION":       1,   # Flow metadata read — 1 scope
    "HANDOFF_FRICTION":            1,   # CaseHistory read — 1 scope
    "APPROVAL_BOTTLENECK":         3,   # ProcessInstance + ProcessInstanceStep + User
    "KNOWLEDGE_GAP":               2,   # Case + CaseArticle read
    "INTEGRATION_CONCENTRATION":   2,   # NamedCredential + Flow metadata
    "PERMISSION_BOTTLENECK":       3,   # ProcessInstance + User + Workitem
    "CROSS_SYSTEM_ECHO":           2,   # Case read + cross-system read
}

# System boundary counts per detector (SF-1.4 mapping table)
_SYSTEM_BOUNDARIES: Dict[str, int] = {
    "REPETITIVE_AUTOMATION":       1,
    "HANDOFF_FRICTION":            1,
    "APPROVAL_BOTTLENECK":         1,
    "KNOWLEDGE_GAP":               1,
    "INTEGRATION_CONCENTRATION":   2,   # Salesforce + external system
    "PERMISSION_BOTTLENECK":       1,
    "CROSS_SYSTEM_ECHO":           2,   # SF + SN or Jira
}

# Process complexity per detector (LOW/MEDIUM/HIGH → 2/5/8 pts)
_PROCESS_COMPLEXITY: Dict[str, float] = {
    "REPETITIVE_AUTOMATION":       2.0,   # LOW — simple flow replacement
    "HANDOFF_FRICTION":            2.0,   # LOW — routing rule change
    "APPROVAL_BOTTLENECK":         5.0,   # MEDIUM — multi-step process
    "KNOWLEDGE_GAP":               2.0,   # LOW — KB attachment workflow
    "INTEGRATION_CONCENTRATION":   2.0,   # LOW — consolidation pattern
    "PERMISSION_BOTTLENECK":       5.0,   # MEDIUM — approval redesign
    "CROSS_SYSTEM_ECHO":           5.0,   # MEDIUM — sync automation
}


def _perm_pts(scope_count: int) -> float:
    if scope_count < 3:   return 2.0
    if scope_count <= 6:  return 5.0
    return 8.0


def _sys_pts(sys_count: int) -> float:
    if sys_count == 1:  return 2.0
    if sys_count == 2:  return 5.0
    return 8.0


def _compute_effort(dr: DetectorResult) -> int:
    """
    Compute Effort score per SF-1.4 formula.
    Data availability defaults to 2 pts (All Tier A) for all Track B v1 detectors.
    """
    did = dr.detector_id
    data_pts  = 2.0   # All Tier A — SF-1.4 Section 4 note
    perm_pts  = _perm_pts(_PERMISSION_SCOPES.get(did, 2))
    sys_pts   = _sys_pts(_SYSTEM_BOUNDARIES.get(did, 1))
    proc_pts  = _PROCESS_COMPLEXITY.get(did, 2.0)

    raw = (data_pts * _W_DATA + perm_pts * _W_PERM +
           sys_pts  * _W_SYS  + proc_pts * _W_PROC)
    return max(1, min(10, round(raw)))


# ─────────────────────────────────────────────────────────────────────────────
# Confidence assignment  (SF-1.4 Section 5 — updated rules)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_confidence(dr: DetectorResult) -> Confidence:
    """
    Confidence rules (first match wins):
        HIGH   = Tier A AND proxy_ratio > 2.0 AND volume > 100
        MEDIUM = Tier A AND proxy_ratio >= 1.0 AND volume >= 20
        LOW    = anything weaker

    All Track B detectors use Tier A data — condition always met.
    proxy_ratio = metric_value / threshold
    volume = meaningful record count for this detector.
    """
    ev = dr.raw_evidence
    did = dr.detector_id

    proxy_ratio = dr.metric_value / dr.threshold if dr.threshold > 0 else 0.0

    # Volume: the most meaningful count for each detector
    volume_map = {
        "REPETITIVE_AUTOMATION":     float(ev.get("records_90d", 0)),
        "HANDOFF_FRICTION":          float(ev.get("total_cases_90d", 0)),
        "APPROVAL_BOTTLENECK":       float(ev.get("pending_count", 0)),
        "KNOWLEDGE_GAP":             float(ev.get("closed_cases_90d", 0)),
        "INTEGRATION_CONCENTRATION": float(ev.get("flow_reference_count", 0)) * 10,  # scale up
        "PERMISSION_BOTTLENECK":     float(ev.get("pending_count", 0)),
        "CROSS_SYSTEM_ECHO":         float(ev.get("sf_total_cases", 0)),
    }
    volume = volume_map.get(did, 0.0)

    # Check for approver_type_notes from SF-2.2 improved approval function
    # If non-User actors present, cap confidence at MEDIUM even if HIGH criteria met
    has_unreliable_approvers = "Role/Queue/Group" in str(ev.get("approver_type_notes", ""))

    if proxy_ratio > 2.0 and volume > 100 and not has_unreliable_approvers:
        return "HIGH"
    if proxy_ratio >= 1.0 and volume >= 20:
        return "MEDIUM"
    return "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# Tier assignment  (SF-1.4 Section 6 — deterministic, first match wins)
# ─────────────────────────────────────────────────────────────────────────────

def _assign_tier(effort: int, confidence: Confidence) -> Tier:
    """
    Steps (first match wins):
        1. Effort <= 4  → Quick Win  (NEXT_30)
        2. Effort >= 7  → Complex    (NEXT_90)
        3. Otherwise    → Strategic  (NEXT_60)
        4. If LOW confidence → downgrade one level
           Quick Win → Strategic, Strategic → Complex, Complex stays Complex

    Roadmap stage mapping:
        Quick Win  → NEXT_30
        Strategic  → NEXT_60
        Complex    → NEXT_90
    """
    if effort <= 4:
        tier: Tier = "Quick Win"
    elif effort >= 7:
        tier = "Complex"
    else:
        tier = "Strategic"

    # Confidence downgrade
    if confidence == "LOW":
        if tier == "Quick Win":
            tier = "Strategic"
        elif tier == "Strategic":
            tier = "Complex"
        # Complex stays Complex

    return tier


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

ROADMAP_STAGE: Dict[Tier, str] = {
    "Quick Win":  "NEXT_30",
    "Strategic":  "NEXT_60",
    "Complex":    "NEXT_90",
}


def score(dr: DetectorResult) -> Dict[str, Any]:
    """
    Convert a DetectorResult into scored opportunity fields.

    Returns dict with keys:
        impact       int   1–10
        effort       int   1–10
        confidence   str   HIGH | MEDIUM | LOW
        tier         str   Quick Win | Strategic | Complex
        roadmap_stage str  NEXT_30 | NEXT_60 | NEXT_90
        score_debug  dict  intermediate factor values (for testing and calibration)

    This function is the implementation contract for SF-1.4.
    """
    impact     = _compute_impact(dr)
    effort     = _compute_effort(dr)
    confidence = _compute_confidence(dr)
    tier       = _assign_tier(effort, confidence)

    # Debug / calibration breakdown
    v, f, c, r, e = _impact_factors(dr)
    raw_sum = v*_W_VOLUME + f*_W_FRICTION + c*_W_CUSTOMER + r*_W_REVENUE + e*_W_EXTERNAL

    score_debug = {
        "impact_factors": {
            "volume_pts":   v,
            "friction_pts": f,
            "customer_pts": c,
            "revenue_pts":  r,
            "external_pts": e,
            "raw_sum":      round(raw_sum, 4),
            # T41-6: expose rescaling inputs for calibration visibility
            "raw_impact_min": _RAW_IMPACT_MIN,
            "raw_impact_max": _RAW_IMPACT_MAX,
        },
        "effort_factors": {
            "data_pts":    2.0,
            "perm_pts":    _perm_pts(_PERMISSION_SCOPES.get(dr.detector_id, 2)),
            "sys_pts":     _sys_pts(_SYSTEM_BOUNDARIES.get(dr.detector_id, 1)),
            "proc_pts":    _PROCESS_COMPLEXITY.get(dr.detector_id, 2.0),
        },
        "proxy_ratio": round(
            dr.metric_value / dr.threshold if dr.threshold > 0 else 0.0, 4
        ),
    }

    return {
        "impact":        impact,
        "effort":        effort,
        "confidence":    confidence,
        "tier":          tier,
        "roadmap_stage": ROADMAP_STAGE[tier],
        "score_debug":   score_debug,
    }
