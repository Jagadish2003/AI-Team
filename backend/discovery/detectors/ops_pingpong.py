"""MSP-B4 T4 — deterministic ServiceNow reassignment ping-pong detector.

The detector operates only on ordered assignment-group/queue history.  It does
not inspect or emit assignees, users, audit authors, descriptions, work notes,
or any other person-level field.  A normal one-way transfer is not a finding;
the minimum qualifying pattern is ``A -> B -> A``.  Longer alternating chains
such as ``A -> B -> A -> B`` are retained in full.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.provenance import EvidencePointer

from ..models import DetectorResult, make_detector_evaluation

DETECTOR_ID = "OPS_REASSIGNMENT_PING_PONG"
MIN_OSCILLATION_HOPS = 2  # A -> B -> A contains two group transitions.

SIGNAL_METRICS = [
    "ping_pong_incident_count",
    "max_hop_count",
]

_POINTER_FIELDS = frozenset(EvidencePointer.__dataclass_fields__)


@dataclass(frozen=True)
class _HistoryPoint:
    group_name: str
    comparison_key: str
    changed_at: Optional[str]
    history_sys_id: Optional[str]
    evidence: Optional[Dict[str, Any]]
    source_url: Optional[str]


@dataclass(frozen=True)
class PingPongFinding:
    """Group-level routing-loop finding for one source incident."""

    finding_id: str
    detector_id: str
    org_id: Optional[str]
    incident_sys_id: str
    incident_number: Optional[str]
    title: str
    explanation: str
    hop_count: int
    return_count: int
    groups_involved: Tuple[str, ...]
    assignment_sequence: Tuple[str, ...]
    ownership_boundaries: Tuple[Dict[str, str], ...]
    first_changed_at: Optional[str]
    last_changed_at: Optional[str]
    source_url: Optional[str]
    evidence_pointers: Tuple[Dict[str, Any], ...]

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["groups_involved"] = list(self.groups_involved)
        payload["assignment_sequence"] = list(self.assignment_sequence)
        payload["ownership_boundaries"] = [
            dict(item) for item in self.ownership_boundaries
        ]
        payload["evidence_pointers"] = [dict(item) for item in self.evidence_pointers]
        return payload


def _text(value: Any) -> Optional[str]:
    """Extract a group-level scalar without looking at person fields."""
    if isinstance(value, Mapping):
        value = (
            value.get("display_value")
            or value.get("displayName")
            or value.get("name")
            or value.get("value")
        )
    if value is None:
        return None
    result = " ".join(str(value).strip().split())
    return result or None


def _group_key(group_name: str) -> str:
    return " ".join(group_name.casefold().split())


def _safe_pointer(value: Any) -> Optional[Dict[str, Any]]:
    """Keep only the evidence-spine fields and reject invalid pointers.

    Copying arbitrary history dictionaries could accidentally copy an audit
    author's display name.  Rebuilding the pointer through the shared contract
    makes the output an allow-list rather than a pass-through.
    """
    if not isinstance(value, Mapping):
        return None
    pointer = EvidencePointer.from_dict(
        {key: value.get(key) for key in _POINTER_FIELDS if key in value}
    )
    return pointer.to_dict() if pointer.is_valid() else None


def _incident_evidence(incident: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    pointer = _safe_pointer(incident.get("evidence"))
    if pointer:
        return pointer
    resolution = incident.get("resolution")
    if isinstance(resolution, Mapping):
        return _safe_pointer(resolution.get("evidence"))
    return None


def _point_from_group(
    group_value: Any,
    entry: Mapping[str, Any],
    *,
    incident_sys_id: str,
    fallback_evidence: Optional[Dict[str, Any]],
) -> Optional[_HistoryPoint]:
    group_name = _text(group_value)
    if not group_name:
        return None

    changed_at = _text(
        entry.get("changed_at")
        or entry.get("sys_created_on")
        or entry.get("source_timestamp")
        or entry.get("timestamp")
    )
    history_sys_id = _text(
        entry.get("history_sys_id")
        or entry.get("audit_sys_id")
        or entry.get("sys_id")
    )
    evidence = _safe_pointer(entry.get("evidence")) or fallback_evidence

    # Build an observed pointer only from stable source values already present
    # in the history.  Never use a wall-clock default: the same input must
    # produce byte-for-byte stable detector output.
    if evidence is None and changed_at and (history_sys_id or incident_sys_id):
        evidence = EvidencePointer.observed(
            source_system="servicenow",
            source_artifact=history_sys_id or incident_sys_id,
            source_timestamp=changed_at,
            source_artifact_type="record_id",
        ).to_dict()

    return _HistoryPoint(
        group_name=group_name,
        comparison_key=_group_key(group_name),
        changed_at=changed_at,
        history_sys_id=history_sys_id,
        evidence=dict(evidence) if evidence else None,
        source_url=_text(entry.get("source_url")),
    )


def _history_entries(incident: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    history = incident.get("assignment_history")
    if history is None:
        history = incident.get("assignment_group_history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return ()
    return (entry for entry in history if isinstance(entry, Mapping))


def normalize_assignment_history(incident: Mapping[str, Any]) -> List[_HistoryPoint]:
    """Return ordered group states with consecutive duplicates collapsed.

    Input order is authoritative: ServiceNow ingestion orders rows by audit
    timestamp and sys_id.  Raw ``old/new`` audit-shaped fixtures are also
    accepted and expanded without inspecting any person-level columns.
    """
    incident_sys_id = _text(incident.get("sys_id") or incident.get("id")) or ""
    fallback_evidence = _incident_evidence(incident)
    points: List[_HistoryPoint] = []

    for entry in _history_entries(incident):
        old_group = entry.get("old_group", entry.get("oldvalue"))
        new_group = entry.get("new_group", entry.get("newvalue"))
        if old_group is not None or new_group is not None:
            group_values = (old_group, new_group)
        else:
            group_values = (
                entry.get("assignment_group")
                or entry.get("group")
                or entry.get("group_name")
                or entry.get("queue")
                or entry.get("queue_name")
                or entry.get("new_value"),
            )

        for group_value in group_values:
            point = _point_from_group(
                group_value,
                entry,
                incident_sys_id=incident_sys_id,
                fallback_evidence=fallback_evidence,
            )
            if point is None:
                continue
            if points and points[-1].comparison_key == point.comparison_key:
                # Duplicate audit rows that do not change queue are not hops.
                continue
            points.append(point)
    return points


def _longest_alternating_span(
    points: Sequence[_HistoryPoint],
) -> Optional[Tuple[int, int]]:
    """Return the longest contiguous A/B/A... span; ties choose the earliest."""
    best: Optional[Tuple[int, int]] = None
    for start in range(0, len(points) - 2):
        first = points[start].comparison_key
        second = points[start + 1].comparison_key
        if not first or not second or first == second:
            continue

        end = start + 1
        while end + 1 < len(points):
            expected = first if (end + 1 - start) % 2 == 0 else second
            if points[end + 1].comparison_key != expected:
                break
            end += 1

        if end - start < MIN_OSCILLATION_HOPS:
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    return best


def _finding_for_incident(
    incident: Mapping[str, Any],
    *,
    org_id: Optional[str],
) -> Optional[PingPongFinding]:
    points = normalize_assignment_history(incident)
    span = _longest_alternating_span(points)
    if span is None:
        return None

    start, end = span
    loop = points[start : end + 1]
    incident_sys_id = _text(incident.get("sys_id") or incident.get("id")) or ""
    incident_number = _text(incident.get("number"))
    hop_count = len(loop) - 1
    return_count = len(loop) - 2
    groups = (loop[0].group_name, loop[1].group_name)
    sequence = tuple(point.group_name for point in loop)
    boundaries = tuple(
        {"from_group": sequence[index], "to_group": sequence[index + 1]}
        for index in range(len(sequence) - 1)
    )

    stable_material = {
        "incident_sys_id": incident_sys_id,
        "groups": [point.comparison_key for point in loop],
        "history_ids": [point.history_sys_id or "" for point in loop],
    }
    digest = hashlib.sha256(
        json.dumps(stable_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    finding_id = f"servicenow:assignment-pingpong:{digest}"

    evidence: List[Dict[str, Any]] = []
    seen_pointers = set()
    for point in loop:
        if point.evidence is None:
            continue
        pointer_key = json.dumps(point.evidence, sort_keys=True, separators=(",", ":"))
        if pointer_key in seen_pointers:
            continue
        seen_pointers.add(pointer_key)
        evidence.append(dict(point.evidence))

    source_url = _text(incident.get("source_url")) or next(
        (point.source_url for point in loop if point.source_url),
        None,
    )
    incident_label = incident_number or incident_sys_id or "The incident"
    explanation = (
        f"Incident {incident_label} moved {hop_count} times between the assignment "
        f"groups {groups[0]} and {groups[1]}, returning to a prior queue "
        f"{return_count} time{'s' if return_count != 1 else ''}."
    )

    return PingPongFinding(
        finding_id=finding_id,
        detector_id=DETECTOR_ID,
        org_id=org_id,
        incident_sys_id=incident_sys_id,
        incident_number=incident_number,
        title="Repeated routing between assignment groups",
        explanation=explanation,
        hop_count=hop_count,
        return_count=return_count,
        groups_involved=groups,
        assignment_sequence=sequence,
        ownership_boundaries=boundaries,
        first_changed_at=loop[0].changed_at,
        last_changed_at=loop[-1].changed_at,
        source_url=source_url,
        evidence_pointers=tuple(evidence),
    )


def _incidents(sn_data: Optional[Dict[str, Any]]) -> List[Mapping[str, Any]]:
    metrics = (sn_data or {}).get("incident_metrics") or {}
    incidents = metrics.get("incidents") if isinstance(metrics, Mapping) else None
    if incidents is None:
        incidents = (sn_data or {}).get("incidents")
    if not isinstance(incidents, Sequence) or isinstance(incidents, (str, bytes)):
        return []
    return [item for item in incidents if isinstance(item, Mapping)]


def find_ping_pong(
    sn_data: Optional[Dict[str, Any]],
    *,
    org_id: Optional[str] = None,
) -> List[PingPongFinding]:
    """Find group-level ping-pong incidents, scoped to one organization."""
    metrics = (sn_data or {}).get("incident_metrics") or {}
    effective_org = org_id or _text((sn_data or {}).get("org_id"))
    if effective_org is None and isinstance(metrics, Mapping):
        effective_org = _text(metrics.get("org_id"))

    findings: List[PingPongFinding] = []
    for incident in _incidents(sn_data):
        incident_org = _text(incident.get("org_id"))
        if effective_org and incident_org and incident_org != effective_org:
            continue
        finding = _finding_for_incident(incident, org_id=effective_org or incident_org)
        if finding is not None:
            findings.append(finding)

    findings.sort(key=lambda item: (item.incident_sys_id, item.finding_id))
    return findings


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    findings = find_ping_pong(sn_data)
    max_hops = max((finding.hop_count for finding in findings), default=0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(max_hops),
        threshold=float(MIN_OSCILLATION_HOPS),
        fired=bool(findings),
        raw_evidence={
            "ping_pong_incident_count": len(findings),
            "max_hop_count": max_hops,
            "findings": [finding.as_dict() for finding in findings],
        },
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    """Emit one observed finding per incident with a qualifying routing loop."""
    return [
        DetectorResult(
            detector_id=DETECTOR_ID,
            signal_source="servicenow",
            metric_value=float(finding.hop_count),
            threshold=float(MIN_OSCILLATION_HOPS),
            raw_evidence=finding.as_dict(),
            provenance_type="observed",
        )
        for finding in find_ping_pong(sn_data)
    ]
