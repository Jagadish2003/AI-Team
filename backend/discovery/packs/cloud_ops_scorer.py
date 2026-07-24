"""
cloud_ops_scorer.py — MSP-B6 T4 (AT-739): the Cloud-Operations pack ops-impact
scorer, with config-driven calibration.

Ranks the T2/T3 findings this pack emits by OPERATIONAL IMPACT across the four
Section-2 dimensions:

  1. effort_concentration  — count x median time-to-resolve (the raw effort a
                             recurring pattern consumes).
  2. breadth               — how many services / CIs (and groups) the finding
                             touches (breadth via MSP-B3).
  3. recurrence_stability  — steady vs burst: a predictable, steady cadence is a
                             better automation candidate than a one-off spike.
  4. automation_shape      — trivially-resolved (short MTTR, one close code)
                             OUTRANKS judgment-heavy work: the purest agent
                             candidate ranks first (T4 AC3).

Every weight and threshold is loaded from the external pack config
(``cloud_ops_pack_config.json`` via ``cloud_ops_config.get_calibration``) — NOT
hardcoded here (T4 AC2). A weight change in that file alters the ranked order with
no code deploy. The module-level DEFAULT_* constants exist only as a documented,
fail-open fallback for a missing/partial config (the same pattern the detectors
use for their thresholds); when the config is present its values win.

The composite ops-impact score is a weighted sum. ``effort_concentration`` and
``breadth`` are raw magnitudes normalised to 0..1 across the ranked SET (so
ranking is relative — "the biggest effort concentration among these findings");
``recurrence_stability`` and ``automation_shape`` are already 0..1 per finding.
The score never touches confidence — confidence is set honestly by the detector
(single-source capped, corroborated HIGH-eligible) and carried through unchanged.

Public API:
  is_cloud_ops_detector(detector_id) -> bool
  rank_cloud_ops_findings(results, *, calibration=None) -> Dict[int, dict]
  score_cloud_ops(dr, *, ranking=None, calibration=None) -> dict
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.models import DetectorResult

try:
    from backend.discovery.packs.cloud_ops_config import (
        get_calibration,
        CloudOpsCalibration,
        CloudOpsConfigError,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs.cloud_ops_config import (
        get_calibration,
        CloudOpsCalibration,
        CloudOpsConfigError,
    )

logger = logging.getLogger(__name__)

# The detector IDs this pack emits (T2 record/stream detectors + T3 hotspot).
CLOUD_OPS_DETECTOR_IDS = frozenset({
    "RECURRING_RESOLUTION_LOOP",
    "ALERT_TRIAGE_TOIL",
    "REASSIGNMENT_PING_PONG",
    "QUEUE_AGEING",
    "SHARED_CI_HOTSPOT",
    "OPS_RUNBOOK_DOCUMENTATION_GAP",
})

# The four ranking dimensions, in Section-2 order.
DIMENSIONS = ("effort_concentration", "breadth", "recurrence_stability", "automation_shape")

# ── Fail-open fallbacks (config wins; these only cover a missing/partial file) ──
# T4 AC2 requires weights/thresholds to come from config — these are NOT the live
# values, only a safe default so a config outage degrades gracefully instead of
# crashing a run (mirrors the detectors' get_detector_thresholds(section, defaults)).
DEFAULT_IMPACT_WEIGHTS: Dict[str, float] = {
    "effort_concentration": 0.4,
    "breadth": 0.25,
    "recurrence_stability": 0.2,
    "automation_shape": 0.15,
}
DEFAULT_AUTOMATION_SHAPE: Dict[str, float] = {
    "trivial_ttr_minutes": 30.0,
    "judgment_ttr_minutes": 240.0,
    "single_close_code_bonus": 0.15,
    "default": 0.5,
}
DEFAULT_RECURRENCE_STABILITY: Dict[str, float] = {
    "steady": 1.0,
    "burst": 0.4,
    "default": 0.6,
}

# Presentational mapping only (impact/effort/tier are derived from the composite;
# ranking itself is the ops-impact score). Effort label domain matches the other
# pack scorers so downstream rendering stays uniform.
_EFFORT_LABEL: Dict[int, str] = {2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}


def is_cloud_ops_detector(detector_id: str) -> bool:
    """Return True when ``detector_id`` belongs to the Cloud-Operations pack."""
    return detector_id in CLOUD_OPS_DETECTOR_IDS


# ── calibration access ──────────────────────────────────────────────────────────


def _calibration(calibration: Optional[CloudOpsCalibration]) -> CloudOpsCalibration:
    if calibration is not None:
        return calibration
    try:
        return get_calibration()
    except CloudOpsConfigError as exc:  # pragma: no cover - defensive
        logger.warning(
            "cloud_ops calibration unavailable (%s); using documented defaults", exc
        )
        return CloudOpsCalibration(
            impact_weights=dict(DEFAULT_IMPACT_WEIGHTS),
            confidence={},
            automation_shape=dict(DEFAULT_AUTOMATION_SHAPE),
            recurrence_stability=dict(DEFAULT_RECURRENCE_STABILITY),
        )


def _weights(cal: CloudOpsCalibration) -> Dict[str, float]:
    weights = dict(DEFAULT_IMPACT_WEIGHTS)
    if cal.impact_weights:
        # Config wins; only the dimensions the config names are used, others 0.
        weights = {d: float(cal.impact_weights.get(d, 0.0)) for d in DIMENSIONS}
    return weights


def _auto_cfg(cal: CloudOpsCalibration) -> Dict[str, float]:
    cfg = dict(DEFAULT_AUTOMATION_SHAPE)
    cfg.update(cal.automation_shape or {})
    return cfg


def _rec_cfg(cal: CloudOpsCalibration) -> Dict[str, float]:
    cfg = dict(DEFAULT_RECURRENCE_STABILITY)
    cfg.update(cal.recurrence_stability or {})
    return cfg


# ── evidence access ───────────────────────────────────────────────────────────


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


# ── the four dimensions ─────────────────────────────────────────────────────────


def _count_field(detector_id: str, ev: Dict[str, Any]) -> float:
    """The per-detector 'count' driving effort concentration."""
    if detector_id == "RECURRING_RESOLUTION_LOOP":
        return _num(ev.get("recurrence_count"))
    if detector_id == "ALERT_TRIAGE_TOIL":
        return _num(ev.get("incident_volume", ev.get("event_count")))
    if detector_id == "QUEUE_AGEING":
        return _num(ev.get("open_count"))
    if detector_id == "SHARED_CI_HOTSPOT":
        return _num(ev.get("incident_count"))
    if detector_id == "REASSIGNMENT_PING_PONG":
        return _num(ev.get("hop_count"))
    # Generic best-effort for an unknown cloud_ops finding.
    for k in ("recurrence_count", "incident_volume", "incident_count", "open_count", "hop_count"):
        if ev.get(k) is not None:
            return _num(ev.get(k))
    return 0.0


def _duration_field(detector_id: str, ev: Dict[str, Any]) -> float:
    """The per-detector 'duration' (minutes/hours) driving effort concentration.

    Returns 1.0 when the finding has no time dimension (e.g. routing hops), so
    effort_concentration collapses to the raw count for that finding.
    """
    if detector_id == "QUEUE_AGEING":
        return _num(ev.get("current_avg_age_hours"), 1.0) or 1.0
    ttr = _num(ev.get("median_ttr_minutes"))
    return ttr if ttr > 0.0 else 1.0


def effort_concentration(dr: DetectorResult, ev: Optional[Dict[str, Any]] = None) -> float:
    """Raw effort magnitude: count x median time-to-resolve (>= 0).

    Honours an explicit ``effort_score`` on the evidence (the recurring-loop
    detector already computes count x median TTR into it) so the measure stays
    consistent with what the detector reported.
    """
    ev = ev if ev is not None else _evidence(dr)
    explicit = ev.get("effort_score")
    if explicit is not None:
        return max(0.0, _num(explicit))
    return max(0.0, _count_field(dr.detector_id, ev) * _duration_field(dr.detector_id, ev))


def breadth(dr: DetectorResult, ev: Optional[Dict[str, Any]] = None) -> float:
    """Raw breadth: distinct services + CIs (+ groups for routing findings) (>= 0).

    Honours an explicit ``breadth`` value on the evidence when present.
    """
    ev = ev if ev is not None else _evidence(dr)
    if ev.get("breadth") is not None:
        return max(0.0, _num(ev.get("breadth")))

    services = _num(ev.get("service_count"))
    if not services:
        svc_list = ev.get("affected_services") or ev.get("services") or []
        if isinstance(svc_list, (list, tuple, set)):
            services = float(len({str(s) for s in svc_list}))
    cis = 1.0 if ev.get("common_ci") else 0.0
    groups = _num(ev.get("groups_involved"))
    # Queue-ageing touches a single queue; give it a floor of 1 so it is not zero.
    total = services + cis + groups
    if total <= 0.0 and dr.detector_id == "QUEUE_AGEING":
        return 1.0
    return total


def recurrence_stability(
    dr: DetectorResult, cal: CloudOpsCalibration, ev: Optional[Dict[str, Any]] = None
) -> float:
    """Steady-vs-burst score in 0..1 (steady ranks higher).

    Resolution order: explicit numeric ``recurrence_stability`` on the evidence →
    a ``recurrence_shape``/``cadence`` label ('steady'/'burst') → an
    ``interarrival_cv`` coefficient of variation (lower CV = steadier) → the
    config default.
    """
    ev = ev if ev is not None else _evidence(dr)
    cfg = _rec_cfg(cal)

    explicit = ev.get("recurrence_stability")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return _clamp01(float(explicit))

    shape = str(ev.get("recurrence_shape") or ev.get("cadence") or "").strip().lower()
    if shape in ("steady", "stable", "regular"):
        return _clamp01(cfg.get("steady", DEFAULT_RECURRENCE_STABILITY["steady"]))
    if shape in ("burst", "bursty", "spike", "spiky"):
        return _clamp01(cfg.get("burst", DEFAULT_RECURRENCE_STABILITY["burst"]))

    cv = ev.get("interarrival_cv")
    if isinstance(cv, (int, float)) and not isinstance(cv, bool):
        return _clamp01(1.0 - float(cv))

    return _clamp01(cfg.get("default", DEFAULT_RECURRENCE_STABILITY["default"]))


def automation_shape(
    dr: DetectorResult, cal: CloudOpsCalibration, ev: Optional[Dict[str, Any]] = None
) -> float:
    """Trivially-resolved-vs-judgment-heavy score in 0..1 (trivial ranks higher).

    Resolution order: explicit numeric ``automation_shape`` on the evidence →
    derived from median MTTR against the configured trivial/judgment bounds, with
    a bonus when there is a single close code (same fix every time = automatable)
    → the config default when the finding carries no time dimension.
    """
    ev = ev if ev is not None else _evidence(dr)
    cfg = _auto_cfg(cal)

    explicit = ev.get("automation_shape")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return _clamp01(float(explicit))

    ttr = _num(ev.get("median_ttr_minutes"))
    if ttr <= 0.0:
        return _clamp01(cfg.get("default", DEFAULT_AUTOMATION_SHAPE["default"]))

    trivial = cfg.get("trivial_ttr_minutes", DEFAULT_AUTOMATION_SHAPE["trivial_ttr_minutes"])
    judgment = cfg.get("judgment_ttr_minutes", DEFAULT_AUTOMATION_SHAPE["judgment_ttr_minutes"])
    span = judgment - trivial
    if span <= 0.0:
        shape = 1.0 if ttr <= trivial else 0.0
    else:
        # ttr <= trivial → 1.0 (trivial); ttr >= judgment → 0.0 (judgment-heavy).
        shape = 1.0 - (ttr - trivial) / span
    shape = _clamp01(shape)

    if _num(ev.get("distinct_close_codes")) == 1.0:
        shape = _clamp01(shape + cfg.get("single_close_code_bonus", 0.0))
    return shape


def _raw_dimensions(dr: DetectorResult, cal: CloudOpsCalibration) -> Dict[str, float]:
    ev = _evidence(dr)
    return {
        "effort_concentration": effort_concentration(dr, ev),
        "breadth": breadth(dr, ev),
        "recurrence_stability": recurrence_stability(dr, cal, ev),
        "automation_shape": automation_shape(dr, cal, ev),
    }


# ── ranking ───────────────────────────────────────────────────────────────────


def _composite(normalized: Dict[str, float], weights: Dict[str, float]) -> float:
    return round(sum(weights.get(d, 0.0) * normalized.get(d, 0.0) for d in DIMENSIONS), 6)


def rank_cloud_ops_findings(
    results: Sequence[DetectorResult],
    *,
    calibration: Optional[CloudOpsCalibration] = None,
) -> Dict[int, Dict[str, Any]]:
    """Rank a set of Cloud-Operations findings by config-weighted ops impact.

    Returns a map keyed by ``id(dr)`` → {ops_impact_score, rank, dimensions,
    normalized}. ``effort_concentration`` and ``breadth`` are min-max-normalised
    (divided by the set maximum) so the composite ranks findings RELATIVE to each
    other; the two already-0..1 dimensions pass through. Rank is 1-based, biggest
    impact first, ties broken deterministically by detector id then signature.
    """
    cal = _calibration(calibration)
    weights = _weights(cal)

    findings = [dr for dr in results if is_cloud_ops_detector(dr.detector_id)]
    raw = {id(dr): _raw_dimensions(dr, cal) for dr in findings}

    max_effort = max((raw[id(dr)]["effort_concentration"] for dr in findings), default=0.0)
    max_breadth = max((raw[id(dr)]["breadth"] for dr in findings), default=0.0)

    index: Dict[int, Dict[str, Any]] = {}
    for dr in findings:
        d = raw[id(dr)]
        normalized = {
            "effort_concentration": (d["effort_concentration"] / max_effort) if max_effort > 0 else 0.0,
            "breadth": (d["breadth"] / max_breadth) if max_breadth > 0 else 0.0,
            "recurrence_stability": d["recurrence_stability"],
            "automation_shape": d["automation_shape"],
        }
        index[id(dr)] = {
            "ops_impact_score": _composite(normalized, weights),
            "dimensions": d,
            "normalized": normalized,
            "weights": dict(weights),
        }

    ordered = sorted(
        findings,
        key=lambda dr: (
            -index[id(dr)]["ops_impact_score"],
            dr.detector_id,
            str(_evidence(dr).get("signature", "")),
        ),
    )
    for rank, dr in enumerate(ordered, start=1):
        index[id(dr)]["rank"] = rank
    return index


# ── per-finding score (the shape the runner + evidence builder consume) ─────────


def _impact_from_score(score: float) -> int:
    """Map an ops-impact score (0..1) to the 1..10 impact domain."""
    return max(1, min(10, int(round(1 + score * 9))))


def _effort_from_automation(automation: float) -> int:
    """Trivially-resolved (high automation shape) => low effort to automate."""
    if automation >= 0.75:
        return 2
    if automation >= 0.5:
        return 3
    if automation >= 0.25:
        return 4
    return 7


def score_cloud_ops(
    dr: DetectorResult,
    *,
    ranking: Optional[Dict[int, Dict[str, Any]]] = None,
    calibration: Optional[CloudOpsCalibration] = None,
) -> Dict[str, Any]:
    """Score one Cloud-Operations finding into the standard scorer shape.

    ``ranking`` is the map from :func:`rank_cloud_ops_findings` for the whole run,
    so ops-impact is normalised across the finding SET. When it is absent (e.g.
    a single finding scored in isolation) the finding is ranked against itself
    (normalised effort/breadth become 1.0 when non-zero).

    Confidence is NOT recomputed — it is the honest, capped level the detector set
    on the four-part contract (single-source capped, corroborated HIGH-eligible),
    carried through unchanged.
    """
    known = is_cloud_ops_detector(dr.detector_id)
    if not known:
        logger.warning(
            "score_cloud_ops: unknown detector '%s' - returning default score. "
            "Check pack_config.py detector list.",
            dr.detector_id,
        )

    cal = _calibration(calibration)
    entry = (ranking or {}).get(id(dr))
    if entry is None:
        # Score in isolation: rank the finding against itself.
        entry = rank_cloud_ops_findings([dr], calibration=cal).get(id(dr), {})

    ops_impact = float(entry.get("ops_impact_score", 0.0))
    dims = entry.get("dimensions", _raw_dimensions(dr, cal))
    rank = entry.get("rank", 1)

    raw = dr.raw_evidence or {}
    contract = raw.get("finding_contract") or {}
    confidence = str(
        raw.get("confidence")
        or (contract.get("confidence") or {}).get("level")
        or "MEDIUM"
    ).upper()
    corroborated = bool(raw.get("corroborated", False))
    corroboration_sources: List[str] = list(raw.get("corroboration_sources") or [])

    impact = _impact_from_score(ops_impact)
    effort = _effort_from_automation(float(dims.get("automation_shape", 0.5)))
    tier = "Strategic" if impact >= 7 else "Quick Win"
    roadmap_stage = "strategic" if tier == "Strategic" else "quick_win"

    return {
        "tier": tier,
        "impact": impact,
        "effort": effort,
        "effort_label": _EFFORT_LABEL.get(effort, "Low"),
        "confidence": confidence,
        "roadmap_stage": roadmap_stage,
        "corroborated": corroborated,
        "corroboration_sources": corroboration_sources,
        # The ops-impact ranking itself — surfaced for ordering + auditability.
        "ops_impact_score": ops_impact,
        "ops_impact_rank": rank,
        "score_debug": {
            "detector_id": dr.detector_id,
            "scorer": "cloud_ops",
            "pack": "cloud_ops",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact": impact,
            "final_impact": impact,
            "confidence_note": (
                "Confidence set by the detector's four-part contract (honest "
                "cap; single-source capped, corroborated HIGH-eligible). The "
                "ops-impact score ranks by config-weighted effort concentration, "
                "breadth, recurrence stability, and automation shape."
            ),
            "ops_impact_score": ops_impact,
            "ops_impact_rank": rank,
            "dimensions": dims,
            "normalized": entry.get("normalized", {}),
            "impact_weights": entry.get("weights", _weights(cal)),
            **({"note": "unknown detector - default score applied"} if not known else {}),
        },
    }
