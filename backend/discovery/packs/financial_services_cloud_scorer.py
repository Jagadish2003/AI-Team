"""financial_services_cloud_scorer.py — 2.0-D1 T3: FSC scoring calibration.

Scores the five FSC detector findings with FSC-SPECIFIC calibration. Used when the
`financial_services_cloud` pack is active; the shared Service Cloud scorer
(``discovery/scorer.py``) is UNTOUCHED and remains the scorer for every other path.

D1 AC4 — "delivered with zero template-model and zero scoring-engine code changes"
— is the governing constraint. Nothing under the shared scoring engine
(``discovery/scorer.py``, ``discovery/calibration/``) or the template model
(``discovery/packs/template_registry.py``, ``app/terminology.py``) is modified by
this subtask. The only edits outside this module are the pack's own config file, the
pack registry entry, and one dispatch branch in the runner — the same three touch
points every existing pack scorer has.

WHERE THE VALUES LIVE, AND WHY IT IS SPLIT
------------------------------------------
Two kinds of number, deliberately kept in two places:

  1. **Per-detector base scores** — ``_FSC_SCORES`` below. A module-level dict
     mapping detector id to {tier, impact, effort, confidence, roadmap_stage},
     mirroring ``_LENDING_SCORES`` in ``discovery/lending_scorer.py``. This is the
     convention every pack scorer on this branch uses, it keeps the diff
     reviewable, and — most importantly — it carries INLINE PROVENANCE on each
     value. An undocumented ``impact: 7`` is indistinguishable from a researched
     one, so every entry below states where its number came from and says
     explicitly when it is a judgement rather than a measurement.

  2. **Dimension weights** — the pack config
     (``financial_services_cloud_pack_config.json`` → ``calibration``), read through
     the loader T2 already built. The three dimensions D1 names (effort
     concentration, breadth, automation shape) are weighted there, so changing the
     ranked order of FSC findings is a config edit with no code deploy.

A note on the grooming premise: the ticket recommends the hardcoded-dict route on
the grounds that the externalised-config pattern "is not merged here". On this
branch it IS — ``cloud_ops_scorer.py`` and ``security_ops_scorer.py`` are both
config-driven — and 2.0-D1 T2 already shipped this pack's config file with a
``calibration`` block explicitly reserved for T3. So no new mechanism is introduced
here (the risk the recommendation guards against does not arise), and the split
above honours both: the reviewable, provenance-annotated dict for the per-detector
values, and pack config for the dimension weights the story asks for by name.

WHAT THIS SCORER DOES NOT DO
----------------------------
* It does not recompute CONFIDENCE. The level is the honest, capped one the detector
  set on its four-part contract (single-source → MEDIUM, capped, with a reason).
  Recomputing it would undo T2's honesty guarantee, so the contract value always
  wins and ``_FSC_SCORES['confidence']`` is only a fallback for a finding that
  carries none.
* It does not recompute IMPACT from the dimensions. The base impact stays the
  documented value from ``_FSC_SCORES`` so its provenance survives into the output;
  the dimensions produce an additive ``ops_impact_score`` / ``ops_impact_rank`` that
  ORDERS findings (the same fields the runner already surfaces for cloud_ops). A
  computed number silently replacing a documented one is exactly the traceability
  loss this module is trying to avoid.

Public API:
  is_financial_services_cloud_detector(detector_id) -> bool
  rank_fsc_findings(results, *, calibration=None) -> Dict[int, dict]
  score_financial_services_cloud(dr, *, ranking=None, calibration=None) -> dict
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.models import DetectorResult

try:
    from backend.discovery.packs.financial_services_cloud_config import (
        FscCalibration,
        FscConfigError,
        get_calibration,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs.financial_services_cloud_config import (
        FscCalibration,
        FscConfigError,
        get_calibration,
    )

logger = logging.getLogger(__name__)


# ── Per-detector base scores ────────────────────────────────────────────────────
#
# Same shape as _LENDING_SCORES (discovery/lending_scorer.py). Keys match
# DETECTOR_ID values exactly — a mismatch degrades LOUDLY (see score_* below).
#
# PROVENANCE: there is no measured FSC dataset behind these numbers. Every entry
# records what its value is based on and whether it is a judgement or a
# measurement. "JUDGEMENT" means exactly that: reasoned, defensible, and still
# unvalidated. Replace with SME-confirmed values and update the comment — the way
# _LENDING_SCORES records "SF-NC-5 confirmed: upgraded from 6".
#
# Effort domain: Low=2, Medium=4, High=7 — the same scale as the Service Cloud
# scorer and every other pack scorer, so downstream rendering stays uniform.

_FSC_SCORES: Dict[str, Dict[str, Any]] = {
    "FSC_SERVICING_REQUEST_RECURRENCE": {
        "tier":       "Quick Win",
        # JUDGEMENT (2.0-D1 T3, no SME confirmation). 7 by analogy with
        # CHECKLIST_BOTTLENECK (impact 7, Quick Win) in _LENDING_SCORES: both are
        # repeated, low-complexity servicing work whose cost is volume rather than
        # severity. Not 8+ because no single recurrence is individually material.
        "impact":     7,
        # Low: repeated data maintenance is the most automatable FSC work
        # (calibration.automation_shape ranks it highest at 0.85).
        "effort":     2,
        # FALLBACK ONLY — the detector's four-part contract confidence wins. T2
        # caps every FSC finding at MEDIUM (single-source), so this is what a
        # future corroborated variant would reach, not what ships today.
        "confidence": "MEDIUM",
        "roadmap_stage": "quick_win",
    },
    "FSC_REFERRAL_HANDOFF_FRICTION": {
        "tier":       "Quick Win",
        # JUDGEMENT (2.0-D1 T3, no SME confirmation). 7 by analogy with
        # LOAN_ORIGINATION_ROUTING_FRICTION (impact 7, Quick Win) in
        # _LENDING_SCORES — the same routing-friction shape, and the nCino SME
        # explicitly upgraded that one from 6 on the grounds that hand-offs extend
        # cycle time. The identical argument applies to referral hops, so 7 rather
        # than 6 is the consistent value.
        "impact":     7,
        "effort":     2,
        "confidence": "MEDIUM",   # fallback only — see above
        "roadmap_stage": "quick_win",
    },
    "FSC_APPROVAL_REVIEW_CYCLE": {
        "tier":       "Strategic",
        # JUDGEMENT (2.0-D1 T3, no SME confirmation). 8 matching
        # APPROVAL_BOTTLENECK (impact 8, Strategic) in _LENDING_SCORES, which the
        # nCino SME confirmed unchanged. FSC servicing reviews carry the same
        # regulated-approval weight, and this detector's compliance guardrail
        # states the decision itself stays human — so the impact is in the delay,
        # not in automating the decision.
        "impact":     8,
        # Medium: the automatable part is escalation and visibility, never the
        # decision (calibration.automation_shape ranks this LOWEST at 0.20).
        "effort":     4,
        "confidence": "MEDIUM",   # fallback only — see above
        "roadmap_stage": "strategic",
    },
    "FSC_SERVICE_QUEUE_AGEING": {
        "tier":       "Strategic",
        # JUDGEMENT (2.0-D1 T3, no SME confirmation). 7 — ageing concentrated in a
        # few queues is a real servicing-turnaround problem, but it is primarily a
        # CAPACITY question rather than an automation opportunity, so it does not
        # reach the 8 given to the regulated approval cycle.
        "impact":     7,
        "effort":     4,
        "confidence": "MEDIUM",   # fallback only — see above
        "roadmap_stage": "strategic",
    },
    "FSC_CROSS_OBJECT_REWORK": {
        "tier":       "Quick Win",
        # JUDGEMENT (2.0-D1 T3, no SME confirmation). 6 — the LOWEST of the five.
        # Duplicated record maintenance is genuinely wasteful and highly
        # automatable, but each individual touch is small, and unlike the approval
        # cycle it carries no regulatory exposure beyond record consistency.
        # Deliberately not inflated to match the others.
        "impact":     6,
        "effort":     2,
        "confidence": "MEDIUM",   # fallback only — see above
        "roadmap_stage": "quick_win",
    },
}

FSC_DETECTOR_IDS = frozenset(_FSC_SCORES)

# The three dimensions D1 names, in story order. Weights live in pack config.
DIMENSIONS = ("effort_concentration", "breadth", "automation_shape")

# ── Fail-open fallbacks (config wins; these cover only a missing/partial file) ──
# Not the live values — a documented default so a config outage degrades rather
# than crashing a run, the same pattern the T2 detectors use for thresholds.
DEFAULT_IMPACT_WEIGHTS: Dict[str, float] = {
    "effort_concentration": 0.45,
    "breadth": 0.30,
    "automation_shape": 0.25,
}
DEFAULT_AUTOMATION_SHAPE = 0.5
DEFAULT_BREADTH_MINIMUM = 1.0

_EFFORT_LABEL: Dict[int, str] = {2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}


def is_financial_services_cloud_detector(detector_id: str) -> bool:
    """Return True when ``detector_id`` is a scored FSC detector."""
    return detector_id in FSC_DETECTOR_IDS


# ── calibration access ─────────────────────────────────────────────────────────


def _calibration(calibration: Optional[FscCalibration]) -> FscCalibration:
    if calibration is not None:
        return calibration
    try:
        return get_calibration()
    except FscConfigError as exc:  # pragma: no cover - defensive
        logger.warning(
            "financial_services_cloud calibration unavailable (%s); using "
            "documented defaults", exc,
        )
        return FscCalibration(impact_weights=dict(DEFAULT_IMPACT_WEIGHTS))


def _weights(cal: FscCalibration) -> Dict[str, float]:
    """The three dimension weights. Config wins; defaults cover a missing file."""
    if cal.impact_weights:
        return {d: float(cal.impact_weights.get(d, 0.0)) for d in DIMENSIONS}
    return dict(DEFAULT_IMPACT_WEIGHTS)


# ── evidence access ────────────────────────────────────────────────────────────


def _evidence(dr: DetectorResult) -> Dict[str, Any]:
    """The finding's evidence view: the four-part contract's evidence block, with
    the flat raw_evidence mirrors as a fallback for any key it omits."""
    raw = dr.raw_evidence or {}
    contract = raw.get("finding_contract") or {}
    ev = contract.get("evidence") or {}
    merged = dict(raw)
    merged.update(ev)  # contract evidence is authoritative where both carry a key
    return merged


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


# ── the three dimensions ───────────────────────────────────────────────────────


def _volume(detector_id: str, ev: Dict[str, Any]) -> float:
    """The per-detector volume driving effort concentration."""
    if detector_id == "FSC_SERVICING_REQUEST_RECURRENCE":
        return _num(ev.get("recurrence_count"))
    if detector_id == "FSC_REFERRAL_HANDOFF_FRICTION":
        return _num(ev.get("total_hops"))
    if detector_id == "FSC_APPROVAL_REVIEW_CYCLE":
        return _num(ev.get("pending_count"))
    if detector_id == "FSC_SERVICE_QUEUE_AGEING":
        return _num(ev.get("open_count"))
    if detector_id == "FSC_CROSS_OBJECT_REWORK":
        return _num(ev.get("rework_touches"))
    for key in ("recurrence_count", "total_hops", "pending_count", "open_count",
                "rework_touches"):
        if ev.get(key) is not None:
            return _num(ev.get(key))
    return 0.0


def _duration_days(detector_id: str, ev: Dict[str, Any], cal: FscCalibration) -> float:
    """The per-detector duration (days) driving effort concentration.

    Returns 1.0 for a detector the config lists as volume-only, so effort
    concentration collapses to the raw volume for findings with no time dimension
    (a referral hop count has no duration to multiply by).
    """
    volume_only = set(
        (cal.effort_concentration or {}).get("volume_only_detectors") or ()
    )
    if detector_id in volume_only:
        return 1.0
    if detector_id == "FSC_APPROVAL_REVIEW_CYCLE":
        return _num(ev.get("median_dwell_days"), 1.0) or 1.0
    if detector_id == "FSC_SERVICE_QUEUE_AGEING":
        return _num(ev.get("current_avg_age_days"), 1.0) or 1.0
    return 1.0


def effort_concentration(
    dr: DetectorResult,
    cal: FscCalibration,
    ev: Optional[Dict[str, Any]] = None,
) -> float:
    """Raw effort magnitude: volume x duration where there is a time dimension,
    volume alone where there is not (>= 0). Normalised across the set by the ranker."""
    ev = ev if ev is not None else _evidence(dr)
    return max(0.0, _volume(dr.detector_id, ev) * _duration_days(dr.detector_id, ev, cal))


def breadth(
    dr: DetectorResult,
    cal: FscCalibration,
    ev: Optional[Dict[str, Any]] = None,
) -> float:
    """Raw breadth: distinct PERMITTED units the finding touches (>= minimum).

    Counts households, financial accounts, teams, queues and service-process types
    — units, never individuals. The AC5 aggregation floor governs what may be a
    unit; this dimension only counts them.
    """
    ev = ev if ev is not None else _evidence(dr)
    minimum = _num((cal.breadth or {}).get("minimum"), DEFAULT_BREADTH_MINIMUM)

    total = (
        _num(ev.get("households_affected"))
        + _num(ev.get("distinct_financial_accounts"))
        + _num(ev.get("teams_involved"))
        + _num(ev.get("queues_involved"))
        + _num(ev.get("service_process_types"))
        + _num(ev.get("households_with_rework"))
    )
    return max(total, minimum)


def automation_shape(
    dr: DetectorResult,
    cal: FscCalibration,
    ev: Optional[Dict[str, Any]] = None,
) -> float:
    """Trivially-automatable vs judgment-heavy, 0..1 (trivial ranks higher).

    Read from config per detector. FSC findings carry DWELL time, which measures
    waiting rather than work, so deriving this from dwell — as the cloud_ops scorer
    derives it from MTTR — would be wrong. An explicit numeric ``automation_shape``
    on the evidence still wins, so a future detector can report its own.
    """
    ev = ev if ev is not None else _evidence(dr)
    explicit = ev.get("automation_shape")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return _clamp01(float(explicit))

    configured = cal.automation_shape_for(dr.detector_id)
    if configured is not None:
        return _clamp01(configured)

    default = _num((cal.automation_shape or {}).get("default"), DEFAULT_AUTOMATION_SHAPE)
    return _clamp01(default)


def _raw_dimensions(dr: DetectorResult, cal: FscCalibration) -> Dict[str, float]:
    ev = _evidence(dr)
    return {
        "effort_concentration": effort_concentration(dr, cal, ev),
        "breadth": breadth(dr, cal, ev),
        "automation_shape": automation_shape(dr, cal, ev),
    }


# ── ranking ────────────────────────────────────────────────────────────────────


def _composite(normalized: Dict[str, float], weights: Dict[str, float]) -> float:
    return round(
        sum(weights.get(d, 0.0) * normalized.get(d, 0.0) for d in DIMENSIONS), 6
    )


def rank_fsc_findings(
    results: Sequence[DetectorResult],
    *,
    calibration: Optional[FscCalibration] = None,
) -> Dict[int, Dict[str, Any]]:
    """Rank FSC findings by config-weighted ops impact.

    Returns a map keyed by ``id(dr)`` → {ops_impact_score, rank, dimensions,
    normalized, weights}. ``effort_concentration`` and ``breadth`` are raw
    magnitudes normalised across the ranked SET (divided by the set maximum) so the
    composite ranks findings RELATIVE to each other; ``automation_shape`` is already
    0..1 and passes through. Rank is 1-based, biggest impact first, ties broken
    deterministically by detector id then aggregation unit.
    """
    cal = _calibration(calibration)
    weights = _weights(cal)

    findings = [
        dr for dr in results if is_financial_services_cloud_detector(dr.detector_id)
    ]
    raw = {id(dr): _raw_dimensions(dr, cal) for dr in findings}

    max_effort = max(
        (raw[id(dr)]["effort_concentration"] for dr in findings), default=0.0
    )
    max_breadth = max((raw[id(dr)]["breadth"] for dr in findings), default=0.0)

    index: Dict[int, Dict[str, Any]] = {}
    for dr in findings:
        d = raw[id(dr)]
        normalized = {
            "effort_concentration": (
                d["effort_concentration"] / max_effort if max_effort > 0 else 0.0
            ),
            "breadth": (d["breadth"] / max_breadth if max_breadth > 0 else 0.0),
            "automation_shape": d["automation_shape"],
        }
        index[id(dr)] = {
            "ops_impact_score": _composite(normalized, weights),
            "dimensions": d,
            "normalized": normalized,
            "weights": dict(weights),
        }

    def _unit(dr: DetectorResult) -> str:
        ev = _evidence(dr)
        for key in ("service_process_type", "referral_type", "review_type", "queue",
                    "object_pair"):
            if ev.get(key):
                return str(ev[key])
        return ""

    ordered = sorted(
        findings,
        key=lambda dr: (-index[id(dr)]["ops_impact_score"], dr.detector_id, _unit(dr)),
    )
    for rank, dr in enumerate(ordered, start=1):
        index[id(dr)]["rank"] = rank
    return index


# ── per-finding score (the shape the runner + evidence builder consume) ────────


def score_financial_services_cloud(
    dr: DetectorResult,
    *,
    ranking: Optional[Dict[int, Dict[str, Any]]] = None,
    calibration: Optional[FscCalibration] = None,
) -> Dict[str, Any]:
    """Score one FSC finding using FSC calibration.

    Returns the same shape as ``discovery/scorer.score()`` for compatibility.

    LOUD DEGRADE: a detector registered in the pack but missing from
    ``_FSC_SCORES`` falls back to the shared Service Cloud scorer and logs a
    WARNING naming it a likely config bug — carried across from ``score_lending``.
    Silent fallback here would mean an FSC finding scored with Service Cloud
    weights and no indication anything was wrong.
    """
    base = _FSC_SCORES.get(dr.detector_id)

    if base is None:
        logger.warning(
            "score_financial_services_cloud: unknown detector '%s' — falling back "
            "to the Service Cloud scorer, so this finding is being scored with "
            "Service Cloud weights rather than FSC calibration. This is most "
            "likely a config bug: the detector is registered in pack_config.py but "
            "missing from _FSC_SCORES in financial_services_cloud_scorer.py. "
            "Known FSC detectors: %s",
            dr.detector_id,
            sorted(FSC_DETECTOR_IDS),
        )
        try:
            from backend.discovery.scorer import score as sc_score
        except ModuleNotFoundError:  # pragma: no cover - import shim
            from discovery.scorer import score as sc_score
        fallback = sc_score(dr)
        # Mark the degrade on the output too, so it is visible to anyone reading a
        # stored artifact rather than only to whoever was watching the log.
        debug = dict(fallback.get("score_debug") or {})
        debug.update({
            "scorer": "service_cloud_fallback",
            "pack": "financial_services_cloud",
            "fallback_reason": (
                "detector not in _FSC_SCORES — scored with Service Cloud weights, "
                "not FSC calibration"
            ),
        })
        fallback["score_debug"] = debug
        return fallback

    cal = _calibration(calibration)
    entry = (ranking or {}).get(id(dr))
    if entry is None:
        # Score in isolation: rank the finding against itself.
        entry = rank_fsc_findings([dr], calibration=cal).get(id(dr), {})

    ops_impact = float(entry.get("ops_impact_score", 0.0))
    dims = entry.get("dimensions", _raw_dimensions(dr, cal))
    rank = entry.get("rank", 1)

    # Confidence is the detector's honest, capped level — never recomputed here.
    raw = dr.raw_evidence or {}
    contract = raw.get("finding_contract") or {}
    confidence = str(
        raw.get("confidence")
        or (contract.get("confidence") or {}).get("level")
        or base["confidence"]
    ).upper()
    corroborated = bool(raw.get("corroborated", False))
    corroboration_sources: List[str] = list(raw.get("corroboration_sources") or [])

    impact = base["impact"]
    effort = base["effort"]

    return {
        "tier":          base["tier"],
        "impact":        impact,
        "effort":        effort,
        "effort_label":  _EFFORT_LABEL.get(effort, "Medium"),
        "confidence":    confidence,
        "roadmap_stage": base["roadmap_stage"],
        "corroborated":  corroborated,
        "corroboration_sources": corroboration_sources,
        # The config-weighted ranking — additive, for ordering and auditability.
        "ops_impact_score": ops_impact,
        "ops_impact_rank":  rank,
        "score_debug": {
            "detector_id":   dr.detector_id,
            "scorer":        "financial_services_cloud",
            "pack":          "financial_services_cloud",
            "metric_value":  dr.metric_value,
            "threshold":     dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact":   base["impact"],
            "final_impact":  impact,
            "impact_note": (
                "Impact is the documented _FSC_SCORES value, NOT recomputed from "
                "the dimensions — a computed number silently replacing a "
                "documented one would lose its provenance."
            ),
            "confidence_note": (
                "Confidence is the honest, capped level the detector set on its "
                "four-part contract (single-source capped). The scorer never "
                "recomputes it."
            ),
            "ops_impact_score": ops_impact,
            "ops_impact_rank":  rank,
            "dimensions":       dims,
            "normalized":       entry.get("normalized", {}),
            "impact_weights":   entry.get("weights", _weights(cal)),
            "calibration_provisional": cal.is_provisional(),
        },
    }
