"""Configuration-driven impact scoring for MSP-B12 Security Operations findings.

The result follows the MSP-B6 operational scorer shape. Breadth counts only
queues, services, and CI classes; hosts and individual vulnerabilities cannot
increase it. All calibration and presentation values live in pack configuration.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

try:
    from backend.discovery.models import DetectorResult
    from backend.discovery.packs.security_ops_config import (
        SecurityOpsCalibration, SecurityOpsConfigError, get_calibration,
    )
except ModuleNotFoundError:  # pragma: no cover
    from discovery.models import DetectorResult
    from discovery.packs.security_ops_config import (
        SecurityOpsCalibration, SecurityOpsConfigError, get_calibration,
    )

logger = logging.getLogger(__name__)

SECURITY_OPS_DETECTOR_IDS = frozenset({
    "SECOPS_REMEDIATION_RECURRENCE", "SECOPS_SECURITY_IT_PING_PONG",
    "SECOPS_SLA_DEFERRAL_AGEING", "SECOPS_SHARED_INFRA_CONCENTRATION",
    "SECOPS_SIR_TRIAGE_TOIL",
})
DIMENSIONS = ("effort_concentration", "breadth", "recurrence_stability", "severity_band")


def is_security_ops_detector(detector_id: str) -> bool:
    return detector_id in SECURITY_OPS_DETECTOR_IDS


def _calibration(value: Optional[SecurityOpsCalibration]) -> SecurityOpsCalibration:
    if value is not None:
        return value
    try:
        return get_calibration()
    except SecurityOpsConfigError as exc:  # pragma: no cover
        logger.warning("security_ops calibration unavailable: %s", exc)
        return SecurityOpsCalibration()


def _evidence(dr: DetectorResult) -> Dict[str, Any]:
    raw = dr.raw_evidence or {}
    merged = dict(raw)
    merged.update(((raw.get("finding_contract") or {}).get("evidence") or {}))
    return merged


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _count(ev: Dict[str, Any]) -> float:
    for key in ("recurrence_count", "incident_volume", "open_count", "hop_count", "workload_count"):
        if ev.get(key) is not None:
            return max(0.0, _number(ev[key]))
    return 0.0


def effort_concentration(dr: DetectorResult, ev: Optional[Dict[str, Any]] = None) -> float:
    """Aggregate effort in minutes."""
    ev = ev if ev is not None else _evidence(dr)
    if ev.get("effort_minutes") is not None:
        return max(0.0, _number(ev["effort_minutes"]))
    seconds = _number(ev.get("median_time_in_state_seconds") or ev.get("current_avg_age_seconds"))
    minutes = _number(ev.get("median_close_minutes"))
    duration = seconds / 60.0 if seconds > 0 else (minutes if minutes > 0 else 1.0)
    return max(0.0, _count(ev) * duration)


def breadth(dr: DetectorResult, ev: Optional[Dict[str, Any]] = None) -> float:
    """Count queues, services, and CI classes, never hosts or vulnerabilities."""
    ev = ev if ev is not None else _evidence(dr)
    if ev.get("breadth") is not None:
        return max(0.0, _number(ev["breadth"]))
    services = max(0.0, _number(ev.get("service_count")))
    queues = set()
    values = ev.get("queues") or []
    if isinstance(values, (list, tuple, set)):
        queues.update(str(v).strip().casefold() for v in values if str(v).strip())
    if ev.get("queue"):
        queues.add(str(ev["queue"]).strip().casefold())
    groups = max(0.0, _number(ev.get("groups_involved")))
    classes = set()
    values = ev.get("service_ci_classes") or ev.get("ci_classes") or []
    if isinstance(values, (list, tuple, set)):
        classes.update(str(v).strip().casefold() for v in values if str(v).strip())
    for key in ("ci_class", "common_ci_class"):
        if ev.get(key):
            classes.add(str(ev[key]).strip().casefold())
    return services + max(float(len(queues)), groups) + float(len(classes))


def recurrence_stability(ev: Dict[str, Any], cal: SecurityOpsCalibration) -> float:
    explicit = ev.get("recurrence_stability")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return _clamp(float(explicit))
    occurrences = _number(ev.get("recurrence_count"))
    target = _number(cal.recurrence_stability.get("target_occurrences"))
    if occurrences > 0 and target > 0:
        return _clamp(occurrences / target)
    return _clamp(_number(cal.recurrence_stability.get("default")))


def severity_band(ev: Dict[str, Any], cal: SecurityOpsCalibration) -> tuple[str, float]:
    weights = cal.severity_band
    default = cal.severity_default if cal.severity_default in weights else "medium"
    raw = str(ev.get("severity_band") or "").strip().casefold()
    band = {"info": "informational", "moderate": "medium", "unknown": default}.get(raw, raw)
    if band not in weights:
        band = default
    return band, _clamp(_number(weights.get(band)))


def _dimensions(dr: DetectorResult, cal: SecurityOpsCalibration) -> Dict[str, Any]:
    ev = _evidence(dr)
    band, severity_weight = severity_band(ev, cal)
    return {
        "effort_concentration": effort_concentration(dr, ev),
        "breadth": breadth(dr, ev),
        "recurrence_stability": recurrence_stability(ev, cal),
        "severity_band": severity_weight,
        "severity_label": band,
    }


def _weights(cal: SecurityOpsCalibration) -> Dict[str, float]:
    return {name: _number(cal.impact_weights.get(name)) for name in DIMENSIONS}


def rank_security_ops_findings(
    results: Sequence[DetectorResult], *, calibration: Optional[SecurityOpsCalibration] = None,
) -> Dict[int, Dict[str, Any]]:
    cal = _calibration(calibration)
    weights = _weights(cal)
    findings = [dr for dr in results if is_security_ops_detector(dr.detector_id)]
    raw = {id(dr): _dimensions(dr, cal) for dr in findings}
    max_effort = max((raw[id(dr)]["effort_concentration"] for dr in findings), default=0.0)
    max_breadth = max((raw[id(dr)]["breadth"] for dr in findings), default=0.0)
    effort_target = _number(cal.normalization.get("effort_target_minutes"), 1.0) or 1.0
    breadth_target = _number(cal.normalization.get("breadth_target"), 1.0) or 1.0
    ranked: Dict[int, Dict[str, Any]] = {}
    for dr in findings:
        dims = raw[id(dr)]
        normalized = {
            "effort_concentration": _clamp(dims["effort_concentration"] / (max_effort or effort_target)),
            "breadth": _clamp(dims["breadth"] / (max_breadth or breadth_target)),
            "recurrence_stability": dims["recurrence_stability"],
            "severity_band": dims["severity_band"],
        }
        score = round(sum(weights[d] * normalized[d] for d in DIMENSIONS), 6)
        ranked[id(dr)] = {"ops_impact_score": score, "dimensions": dims,
                          "normalized": normalized, "weights": dict(weights)}
    ordered = sorted(findings, key=lambda dr: (
        -ranked[id(dr)]["ops_impact_score"], dr.detector_id,
        str(_evidence(dr).get("finding_ref", "")),
    ))
    for position, dr in enumerate(ordered, 1):
        ranked[id(dr)]["rank"] = position
    return ranked


def _presentation(score: float, cal: SecurityOpsCalibration) -> tuple[str, str]:
    choices = [(_number(v.get("min_score")), key, str(v.get("tier") or key))
               for key, v in cal.score_tiers.items()]
    _, key, tier = max((v for v in choices if score >= v[0]),
                       default=(0.0, "quick_win", "Quick Win"))
    return tier, cal.roadmap_stages.get(key, key)


def score_security_ops(
    dr: DetectorResult, *, ranking: Optional[Dict[int, Dict[str, Any]]] = None,
    calibration: Optional[SecurityOpsCalibration] = None,
) -> Dict[str, Any]:
    cal = _calibration(calibration)
    entry = (ranking or {}).get(id(dr))
    if entry is None:
        entry = rank_security_ops_findings([dr], calibration=cal).get(id(dr), {})
    score = _number(entry.get("ops_impact_score"))
    low = int(_number(cal.normalization.get("impact_min"), 1.0))
    high = int(_number(cal.normalization.get("impact_max"), 10.0))
    impact = max(low, min(high, int(round(low + score * (high - low)))))
    tier, roadmap_stage = _presentation(score, cal)
    raw = dr.raw_evidence or {}
    contract = raw.get("finding_contract") or {}
    confidence = str(raw.get("confidence") or (contract.get("confidence") or {}).get("level")
                     or cal.confidence_defaults.get("missing", "MEDIUM")).upper()
    corroborated = bool(raw.get("corroborated", False))
    sources = list(raw.get("corroboration_sources") or [])
    return {
        "tier": tier, "impact": impact,
        "effort": int(_number(cal.presentation.get("effort"))),
        "effort_label": str(cal.presentation.get("effort_label") or ""),
        "confidence": confidence, "roadmap_stage": roadmap_stage,
        "corroborated": corroborated, "corroboration_sources": sources,
        "ops_impact_score": score, "ops_impact_rank": entry.get("rank", 1),
        "score_debug": {
            "detector_id": dr.detector_id, "scorer": "security_ops", "pack": "security_ops",
            "metric_value": dr.metric_value, "threshold": dr.threshold,
            "signal_source": dr.signal_source, "dimensions": entry.get("dimensions", {}),
            "normalized": entry.get("normalized", {}),
            "impact_weights": entry.get("weights", _weights(cal)),
            "evidence_preserved": bool(contract.get("evidence")),
            "corroboration_preserved": bool(contract.get("corroboration")),
        },
    }
