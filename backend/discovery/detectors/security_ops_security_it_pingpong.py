"""
SECOPS_SECURITY_IT_PING_PONG — MSP-B12 T2, Security Operations pack.

The canonical ownership-friction loop: a remediation task or security incident
oscillating between assignment groups — Security → IT → Security. Reuses the
group-only history processing from ``ops_pingpong`` (ordered assignment-GROUP
sequence, consecutive duplicates collapsed, longest alternating A/B/A… span) and
points it at MSP-B11's SecOps assignment history.

Consumes ONLY the ``assignment_history`` of MSP-B11 records
(``sn_data['secops']['security_incidents']`` and
``sn_data['vulnerability_response']`` items / remediation tasks). It NEVER inspects
or emits an assignee — only assignment-group transitions define a hop (B11 audits
only ``state`` and ``assignment_group``; ``assigned_to`` is deliberately not read).

Emits one finding per record whose longest oscillation reaches ``min_hops`` hops
(default 2 = A→B→A). Fails safe: a record with no assignment history contributes
nothing.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..models import DetectorResult, make_detector_evaluation
from ..packs import security_ops_finding as fc
from . import security_ops_common as common

try:
    from ..packs.security_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "SECOPS_SECURITY_IT_PING_PONG"

DEFAULT_MIN_HOPS = 2  # A → B → A is two group transitions (canonical ping-pong).
_THRESHOLD_SECTION = "security_it_ping_pong"

SIGNAL_METRICS: List[str] = [
    "hop_count",             # metric_value — group transitions in the loop
    "return_count",
    "ping_pong_record_count",
]

# Best-effort group-role hints for the security↔IT boundary annotation. Never gate
# firing (naming varies per tenant); the ping-pong pattern itself is the signal.
_SECURITY_HINTS = ("security", "secops", "soc", "vulnerability", "vuln", "csirt", "incident response")
_IT_HINTS = ("it", "infra", "infrastructure", "server", "platform", "network", "ops", "operations", "sysadmin")


def _min_hops() -> int:
    if get_detector_thresholds is None:
        return DEFAULT_MIN_HOPS
    return int(get_detector_thresholds(_THRESHOLD_SECTION, {"min_hops": DEFAULT_MIN_HOPS}).get(
        "min_hops", DEFAULT_MIN_HOPS) or DEFAULT_MIN_HOPS)


def _role(group: str) -> Optional[str]:
    lowered = group.casefold()
    if any(h in lowered for h in _SECURITY_HINTS):
        return "security"
    if any(h in lowered for h in _IT_HINTS):
        return "it"
    return None


def _longest_alternating_span(seq: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Longest contiguous A/B/A… span; ties choose the earliest (ops_pingpong)."""
    best: Optional[Tuple[int, int]] = None
    keys = [g.casefold() for g in seq]
    for start in range(0, len(keys) - 2):
        first, second = keys[start], keys[start + 1]
        if not first or not second or first == second:
            continue
        end = start + 1
        while end + 1 < len(keys):
            expected = first if (end + 1 - start) % 2 == 0 else second
            if keys[end + 1] != expected:
                break
            end += 1
        if end - start < 2:
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    return best


def _candidate_records(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str]):
    """Yield (record, record_type) across the SecOps records that carry history."""
    secops = common.secops_block(sn_data)
    vr = common.vr_block(sn_data)
    effective = common.effective_org(secops, org_id) or common.effective_org(vr, org_id)
    plan = (
        (common._records(secops, "security_incidents"), "security_incident"),
        (common._records(vr, "remediation_tasks"), "remediation_task"),
        (common._records(vr, "vulnerable_items"), "vulnerable_item"),
    )
    for records, record_type in plan:
        for record in records:
            if common.in_org(record, effective):
                yield record, record_type, effective


def _finding_for_record(
    record: Mapping[str, Any], record_type: str, org: Optional[str], min_hops: int
) -> Optional[Dict[str, Any]]:
    seq = common.group_sequence(record)
    span = _longest_alternating_span(seq)
    if span is None:
        return None
    start, end = span
    loop = seq[start:end + 1]
    hop_count = len(loop) - 1
    if hop_count < min_hops:
        return None
    groups = (loop[0], loop[1])
    roles = {_role(loop[0]), _role(loop[1])}
    return {
        "record_type": record_type,
        "sys_id": common._text(record.get("sys_id")) or "",
        "hop_count": hop_count,
        "return_count": len(loop) - 2,
        "groups_involved": list(groups),
        "assignment_sequence": list(loop),
        "security_it_boundary": roles == {"security", "it"},
        "org_id": org,
        "record": record,
    }


def _findings(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str] = None) -> List[Dict[str, Any]]:
    min_hops = _min_hops()
    findings = [
        f for record, record_type, org in _candidate_records(sn_data, org_id)
        if (f := _finding_for_record(record, record_type, org, min_hops)) is not None
    ]
    findings.sort(key=lambda f: (-f["hop_count"], f["record_type"], f["sys_id"]))
    return findings


def _build_result(f: Dict[str, Any], min_hops: int) -> DetectorResult:
    material = {"org_id": f["org_id"] or "", "sys_id": f["sys_id"], "groups": [g.casefold() for g in f["assignment_sequence"]]}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    evidence = {
        "record_type": f["record_type"],
        "hop_count": f["hop_count"],
        "return_count": f["return_count"],
        "groups_involved": f["groups_involved"],
        "assignment_sequence": f["assignment_sequence"],
        "security_it_boundary": f["security_it_boundary"],
    }
    artifacts = common.pointer_artifacts([f["record"]], artifact_type=f["record_type"])
    systems = [common.SOURCE_SYSTEM]
    confidence = fc.build_confidence(
        "MEDIUM",
        capped=True,
        eligible_for_high=False,
        cap_reason="ServiceNow assignment history only — single-source routing loop.",
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE, sources=systems, label=fc.SINGLE_SOURCE_LABEL
    )
    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(systems=systems, artifacts=artifacts),
    )
    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(f["hop_count"]),
        threshold=float(min_hops),
        raw_evidence={
            "hop_count": f["hop_count"],
            "return_count": f["return_count"],
            "groups_involved": f["groups_involved"],
            "security_it_boundary": f["security_it_boundary"],
            "record_type": f["record_type"],
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_ref": f"servicenow:secops-pingpong:{digest}",
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    findings = _findings(sn_data)
    min_hops = _min_hops()
    max_hops = max((f["hop_count"] for f in findings), default=0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(max_hops),
        threshold=float(min_hops),
        fired=bool(findings),
        raw_evidence={
            "hop_count": max_hops,
            "return_count": max((f["return_count"] for f in findings), default=0),
            "ping_pong_record_count": len(findings),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    min_hops = _min_hops()
    return [_build_result(f, min_hops) for f in _findings(sn_data)]
