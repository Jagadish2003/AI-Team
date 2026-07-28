"""Production assembly for the MSP cloud-operations signal chain.

The component stories deliberately expose small, independently testable
primitives:

* MSP-B4 emits deterministic ServiceNow recurrence and group-routing records;
* MSP-B5 evaluates those recurrences against the runbook library; and
* MSP-B8 emits normalised ``OperationalEvent`` records from staged exports.

The Cloud Operations pack consumes a compact ``sn_data["cloud_ops"]`` block.
This module is the production seam between those two shapes.  It does not invent
joins: event-to-incident corroboration requires an explicit event signature on
the incident and must also pass MSP-B7's configured correlation window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .correlation.windows import JOIN_EVENT_INCIDENT, join_within_window
from .detectors.ops_pingpong import find_ping_pong
from .detectors.ops_recurrence import find_recurrences
from .detectors.ops_recurrence_joins import extract_event_signatures
from .detectors.runbook_pipeline import evaluate_runbook_recurrences
from .signals.noise_floor import apply_noise_floors
from .signals.operational_event import OperationalEvent, ResourceRef
from .signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET
from .signals.ops_stream import ActiveSignal, OpsEventStream

logger = logging.getLogger(__name__)

_EVENT_FIELDS = frozenset(OperationalEvent.__dataclass_fields__)
_MAX_EVENT_POINTERS = 10
_MAX_WINDOW_TRACES = 20


@dataclass(frozen=True)
class CloudOpsRuntimeResult:
    """The detector input block plus a safe, user-visible health summary."""

    block: Dict[str, Any]
    health: Dict[str, Any]


def _text(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = (
            value.get("display_value")
            or value.get("displayName")
            or value.get("name")
            or value.get("value")
            or value.get("sys_id")
        )
    result = " ".join(str(value or "").strip().split())
    return result or None


def _incident_rows(sn_data: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    payload = sn_data or {}
    metrics = payload.get("incident_metrics")
    incidents = metrics.get("incidents") if isinstance(metrics, Mapping) else None
    if incidents is None:
        incidents = payload.get("incidents")
    if not isinstance(incidents, Sequence) or isinstance(incidents, (str, bytes)):
        return []
    return [row for row in incidents if isinstance(row, Mapping)]


def _incident_id(incident: Mapping[str, Any]) -> Optional[str]:
    resolution = incident.get("resolution")
    if isinstance(resolution, Mapping):
        value = _text(resolution.get("incident_sys_id"))
        if value:
            return value
    return _text(incident.get("sys_id") or incident.get("id"))


def _incident_org(incident: Mapping[str, Any]) -> Optional[str]:
    return _text(incident.get("org_id"))


def _resolution(incident: Mapping[str, Any]) -> Mapping[str, Any]:
    value = incident.get("resolution")
    return value if isinstance(value, Mapping) else {}


def _safe_service_names(incident: Mapping[str, Any]) -> Tuple[str, ...]:
    """Read service-level fields only; never person/assignee fields."""

    names = set()
    for key in ("business_service", "service", "affected_service"):
        value = _text(incident.get(key))
        if value:
            names.add(value)
    values = incident.get("affected_services")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            text = _text(value)
            if text:
                names.add(text)
    return tuple(sorted(names))


def _incident_opened_at(incident: Mapping[str, Any]) -> Optional[str]:
    """The incident-side timestamp used by the B7 event/incident window."""

    resolution = _resolution(incident)
    return _text(
        incident.get("opened_at")
        or incident.get("opened")
        or incident.get("sys_created_on")
        or incident.get("created_at")
        or resolution.get("resolved_at")
        or incident.get("resolved_at")
        or incident.get("closed_at")
    )


def _incident_ttr_seconds(incident: Mapping[str, Any]) -> Optional[float]:
    value = _resolution(incident).get(
        "time_to_resolve_seconds", incident.get("time_to_resolve_seconds")
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def operational_event_from_bridge_record(
    record: Mapping[str, Any],
    *,
    org_id: str,
) -> OperationalEvent:
    """Rebuild the bridge's JSON event as the canonical in-memory event.

    This validation is intentionally suitable for a change-runner
    ``process_batch`` callback: an invalid or cross-org record raises before its
    checkpoint can advance.
    """

    if not org_id or not str(org_id).strip():
        raise ValueError("org_id is required")
    payload = record.get("event", record)
    if not isinstance(payload, Mapping):
        raise ValueError("bridge record must carry an event mapping")
    event_org = _text(payload.get("org_id"))
    if event_org != str(org_id).strip():
        raise ValueError("bridge event does not belong to the requested org")

    values = {key: payload.get(key) for key in _EVENT_FIELDS if key in payload}
    resource = values.get("resource")
    if isinstance(resource, Mapping):
        values["resource"] = ResourceRef(**dict(resource))
    elif resource is not None and not isinstance(resource, ResourceRef):
        raise ValueError("bridge event resource must be a mapping or ResourceRef")
    return OperationalEvent(**values)


def _incident_signature_index(
    sn_data: Optional[Mapping[str, Any]],
    *,
    org_id: str,
) -> Dict[str, List[Mapping[str, Any]]]:
    index: Dict[str, List[Mapping[str, Any]]] = {}
    for incident in _incident_rows(sn_data):
        row_org = _incident_org(incident)
        if row_org and row_org != org_id:
            continue
        resolution = _resolution(incident)
        for signature in extract_event_signatures(incident, resolution):
            index.setdefault(signature, []).append(incident)
    return index


def _best_window_join(
    member_timestamps: Sequence[str],
    incident: Mapping[str, Any],
    *,
    org_id: str,
):
    incident_at = _incident_opened_at(incident)
    candidates = [
        join_within_window(
            event_at,
            incident_at,
            JOIN_EVENT_INCIDENT,
            org_id=org_id,
        )
        for event_at in member_timestamps
    ]
    if not candidates:
        return join_within_window(
            None,
            incident_at,
            JOIN_EVENT_INCIDENT,
            org_id=org_id,
        )
    return min(
        candidates,
        key=lambda item: (
            item.delta_seconds is None,
            item.delta_seconds if item.delta_seconds is not None else float("inf"),
        ),
    )


def _aggregate_event_signatures(
    bridge_records: Iterable[Mapping[str, Any]],
    sn_data: Optional[Mapping[str, Any]],
    *,
    org_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    records = list(bridge_records or ())
    stream = OpsEventStream(budget=CALIBRATED_RUN_EVENT_BUDGET)
    metadata: Dict[str, Dict[str, Any]] = {}
    for record in records:
        event = operational_event_from_bridge_record(record, org_id=org_id)
        stream.admit(event, org_id=org_id)
        metadata[event.signal_id] = {
            "batch_id": _text(record.get("batch_id")),
            "staging_row_id": record.get("staging_row_id"),
        }

    visible, suppression = apply_noise_floors(stream.active_signals(org_id))
    budget = stream.budget_report()
    incident_index = _incident_signature_index(sn_data, org_id=org_id)

    buckets: Dict[str, List[ActiveSignal]] = {}
    for signal in visible:
        buckets.setdefault(signal.event_signature, []).append(signal)

    event_rows: List[Dict[str, Any]] = []
    for signature in sorted(buckets):
        signals = buckets[signature]
        representatives = sorted(
            (signal.representative for signal in signals),
            key=lambda event: (event.observed_at, event.signal_id),
        )
        representative = representatives[0]
        event_count = sum(signal.occurrence_count for signal in signals)
        first_seen = min(signal.first_seen for signal in signals)
        last_seen = max(signal.last_seen for signal in signals)
        pointers = sorted(
            (
                dict(pointer)
                for signal in signals
                for pointer in signal.member_pointers
            ),
            key=lambda pointer: (
                str(pointer.get("source_timestamp") or ""),
                str(pointer.get("source_artifact") or ""),
            ),
        )
        member_timestamps = [
            str(pointer.get("source_timestamp"))
            for pointer in pointers
            if pointer.get("source_timestamp")
        ]
        if not member_timestamps:
            member_timestamps = [first_seen, last_seen]

        linked_incidents = incident_index.get(signature, [])
        joined_incidents: List[Mapping[str, Any]] = []
        window_traces: List[Dict[str, Any]] = []
        for incident in linked_incidents:
            joined = _best_window_join(member_timestamps, incident, org_id=org_id)
            if len(window_traces) < _MAX_WINDOW_TRACES:
                window_traces.append(joined.to_trace()["correlation_window"])
            if joined.within:
                joined_incidents.append(incident)

        incident_ids = sorted(
            {
                incident_id
                for incident in joined_incidents
                if (incident_id := _incident_id(incident))
            }
        )
        ttrs = [
            value
            for incident in joined_incidents
            if (value := _incident_ttr_seconds(incident)) is not None
        ]
        close_codes = sorted(
            {
                code
                for incident in joined_incidents
                if (
                    code := _text(
                        _resolution(incident).get(
                            "close_code", incident.get("close_code")
                        )
                    )
                )
            }
        )
        groups = sorted(
            {
                group
                for incident in joined_incidents
                if (
                    group := _text(
                        _resolution(incident).get(
                            "resolved_by_group", incident.get("assignment_group")
                        )
                    )
                )
            }
        )
        services = sorted(
            {
                service
                for incident in joined_incidents
                for service in _safe_service_names(incident)
            }
        )
        provider_event_ids = sorted(
            {
                event_id
                for signal in signals
                for event_id in signal.provider_event_ids
            }
        )
        batch_ids = sorted(
            {
                batch_id
                for event_id in provider_event_ids
                if (batch_id := metadata.get(event_id, {}).get("batch_id"))
            }
        )
        staging_row_ids = sorted(
            {
                row_id
                for event_id in provider_event_ids
                if (row_id := metadata.get(event_id, {}).get("staging_row_id"))
                is not None
            }
        )
        severity_profile: Dict[str, int] = {}
        for signal in signals:
            for severity, count in signal.severity_profile.items():
                severity_profile[severity] = severity_profile.get(severity, 0) + count

        resource = representative.resource
        event_rows.append(
            {
                "signature": signature,
                "event_count": event_count,
                "recurring": event_count > 1,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "active_period_count": len(signals),
                "source_systems": sorted(
                    {signal.representative.source_system for signal in signals}
                ),
                "provider": resource.provider if resource else None,
                "resource_id": resource.resource_id if resource else "",
                "resource_type": representative.resource_type,
                "event_class": representative.event_class,
                "severity_profile": dict(sorted(severity_profile.items())),
                "ci": resource.resource_id if resource else "",
                "incident_count": len(incident_ids),
                "incident_ids": incident_ids,
                "median_ttr_minutes": (
                    round(float(median(ttrs)) / 60.0, 4) if ttrs else 0.0
                ),
                "close_code": close_codes[0] if len(close_codes) == 1 else "",
                "close_codes": close_codes,
                "distinct_close_codes": len(close_codes),
                "assignment_group": groups[0] if len(groups) == 1 else "",
                "affected_services": services,
                "window_overlap": bool(joined_incidents),
                "window_gated": True,
                "explicitly_linked_incident_count": len(linked_incidents),
                "correlation_windows": window_traces,
                "evidence_pointers": pointers[:_MAX_EVENT_POINTERS],
                "evidence_sampled_from": len(pointers),
                "provider_event_ids": provider_event_ids,
                "batch_ids": batch_ids,
                "staging_row_ids": staging_row_ids,
                "transport": "event_history_bridge",
            }
        )

    health = {
        "status": "degraded" if budget.breached else "ok",
        "bridge_records": len(records),
        "active_signals": len(stream.active_signals(org_id)),
        "visible_signals": len(visible),
        "event_signatures": len(event_rows),
        "budget": budget.to_dict(),
        "noise_suppression": suppression.to_dict(),
    }
    return event_rows, health


def _recurrence_incidents(
    record: Any,
    incidents_by_id: Mapping[str, Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    result = []
    for example in getattr(record, "examples", ()) or ():
        incident = incidents_by_id.get(str(example.get("incident_sys_id") or ""))
        if incident is not None:
            result.append(incident)
    return result


def _adapt_recurrence(
    record: Any,
    incidents_by_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    components = dict(getattr(record, "signature_components", {}) or {})
    identity = components.get("incident_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    resolution = components.get("resolution")
    resolution = resolution if isinstance(resolution, Mapping) else {}
    category = _text(identity.get("category")) or "incident"
    ci_component = _text(identity.get("ci_component"))
    incident_kind = f"{category} / {ci_component}" if ci_component else category
    close_code = _text(resolution.get("close_code")) or ""
    group = _text(resolution.get("resolved_by_group")) or ""
    resolution_label = " / ".join(part for part in (close_code, group) if part)

    incidents = _recurrence_incidents(record, incidents_by_id)
    services = sorted(
        {
            service
            for incident in incidents
            for service in _safe_service_names(incident)
        }
    )
    event_link = dict(getattr(record, "event_signature_link", {}) or {})
    linked_signatures = [
        str(value)
        for value in (event_link.get("event_signatures") or [])
        if value
    ]
    ci_location = dict(getattr(record, "ci_location", {}) or {})
    examples = list(getattr(record, "examples", ()) or ())
    incident_ids = [
        str(example.get("incident_sys_id"))
        for example in examples
        if example.get("incident_sys_id")
    ]
    median_seconds = getattr(record, "median_time_to_resolve_seconds", None)
    return {
        "signature": str(record.record_id),
        "incident_kind": incident_kind,
        "resolution": resolution_label,
        "count": int(record.recurrence_count),
        "median_ttr_minutes": (
            round(float(median_seconds) / 60.0, 4)
            if isinstance(median_seconds, (int, float))
            else 0.0
        ),
        "assignment_group": group,
        "affected_services": services,
        "close_code": close_code,
        # B6 currently accepts one exact event signature. Multiple explicit links
        # remain visible below but do not receive an arbitrary "first" join.
        "event_signature": (
            linked_signatures[0] if len(linked_signatures) == 1 else None
        ),
        "event_signatures": linked_signatures,
        "incident_ids": incident_ids,
        "ci": ci_location.get("ci_id"),
        "ci_class": ci_location.get("ci_class"),
        "b4_record": record.as_dict(),
    }


def _adapt_ping_pong(
    finding: Any,
    incidents_by_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    incident = incidents_by_id.get(str(finding.incident_sys_id))
    services = _safe_service_names(incident) if incident is not None else ()
    return {
        "signature": str(finding.finding_id),
        "incident_id": str(finding.incident_sys_id),
        "hop_count": int(finding.hop_count),
        "groups_involved": list(finding.groups_involved),
        "hops": [dict(boundary) for boundary in finding.ownership_boundaries],
        "affected_service": services[0] if len(services) == 1 else "",
        "b4_record": finding.as_dict(),
    }


def _merge_keyed(
    existing: Any,
    produced: Sequence[Mapping[str, Any]],
    *,
    key: str,
    produced_wins: bool,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in existing or ():
        if not isinstance(item, Mapping):
            continue
        item_key = str(item.get(key) or "")
        if not item_key:
            continue
        if item_key not in merged:
            order.append(item_key)
        merged[item_key] = dict(item)
    for item in produced:
        item_key = str(item.get(key) or "")
        if not item_key:
            continue
        if item_key not in merged:
            order.append(item_key)
            merged[item_key] = dict(item)
        elif produced_wins:
            merged[item_key] = {**merged[item_key], **dict(item)}
    return [merged[item_key] for item_key in order]


def _runbook_outputs(
    org_id: str,
    sn_data: Optional[Mapping[str, Any]],
    recurrence_rows: List[Dict[str, Any]],
    *,
    runbook_batch_fn: Callable[..., Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    if not recurrence_rows:
        return (
            {
                "available": False,
                "status": "not_applicable",
                "matches": {},
                "evaluations": {},
                "note_handoff": {"status": "not_applicable"},
            },
            [],
            {"status": "not_applicable", "evaluated": 0, "matches": 0, "gaps": 0},
        )

    try:
        batch = runbook_batch_fn(org_id, sn_data)
    except Exception as exc:  # B5 is explicitly degradable.
        logger.warning(
            "cloud-ops runbook matching unavailable for org %s: [%s]",
            org_id,
            type(exc).__name__,
        )
        for row in recurrence_rows:
            row["runbook_state"] = "unavailable"
            row["runbook_matching_available"] = False
        return (
            {
                "available": False,
                "status": "unavailable",
                "reason": type(exc).__name__,
                "matches": {},
                "evaluations": {},
                "note_handoff": {"status": "unavailable"},
            },
            [],
            {
                "status": "unavailable",
                "evaluated": 0,
                "matches": 0,
                "gaps": 0,
                "reason": type(exc).__name__,
            },
        )

    rows_by_id = {str(row["signature"]): row for row in recurrence_rows}
    matches: Dict[str, Dict[str, Any]] = {}
    evaluations: Dict[str, Dict[str, Any]] = {}
    gaps: List[Dict[str, Any]] = []
    for result in getattr(batch, "recurrences", ()) or ():
        recurrence_id = str(getattr(result, "recurrence_id", "") or "")
        if not recurrence_id:
            continue
        state = str(getattr(result, "state", "") or "")
        retrieval = getattr(result, "retrieval", None)
        retrieval_status = str(getattr(retrieval, "status", "") or "")
        composite = getattr(result, "composite", None)
        match = getattr(composite, "runbook_match", None)
        match_payload = dict(match) if isinstance(match, Mapping) else None
        if match_payload:
            matches[recurrence_id] = match_payload

        gap_evaluation = getattr(result, "documentation_gap", None)
        gap_state = str(getattr(gap_evaluation, "state", "") or "")
        gap_finding = getattr(gap_evaluation, "finding", None)
        if gap_finding is not None:
            gaps.append(gap_finding.as_dict())

        redaction = getattr(result, "query_redaction", None)
        redaction_payload = (
            redaction.as_dict() if redaction is not None else {}
        )
        citation = getattr(result, "citation_resolution", None)
        evaluations[recurrence_id] = {
            "state": state,
            "retrieval_status": retrieval_status,
            "retrieval_performed": bool(
                getattr(result, "retrieval_performed", False)
            ),
            "citation_status": str(getattr(citation, "status", "") or ""),
            "query_redaction": redaction_payload,
            "documentation_gap_state": gap_state,
        }
        row = rows_by_id.get(recurrence_id)
        if row is not None:
            row["runbook_state"] = state
            row["runbook_matching_available"] = state != "unavailable"
            if match_payload:
                row["runbook_match"] = match_payload

    # A missing per-recurrence result is not a successful no-match.
    missing_evaluations = 0
    for recurrence_id, row in rows_by_id.items():
        if recurrence_id not in evaluations:
            row["runbook_state"] = "unavailable"
            row["runbook_matching_available"] = False
            missing_evaluations += 1

    unavailable_evaluations = sum(
        1 for item in evaluations.values() if item["state"] == "unavailable"
    )
    unavailable_total = unavailable_evaluations + missing_evaluations
    if unavailable_total >= len(recurrence_rows):
        runbook_status = "unavailable"
    elif unavailable_total:
        runbook_status = "degraded"
    else:
        runbook_status = "ok"

    note_handoff = getattr(batch, "note_handoff", {}) or {}
    block = {
        "available": runbook_status != "unavailable",
        "status": runbook_status,
        "matches": matches,
        "evaluations": evaluations,
        "note_handoff": dict(note_handoff),
    }
    health = {
        "status": runbook_status,
        "evaluated": len(evaluations),
        "matches": len(matches),
        "gaps": len(gaps),
        "unavailable": unavailable_total,
        "missing_evaluations": missing_evaluations,
    }
    return block, gaps, health


def _merge_runbook_blocks(existing: Any, produced: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(existing, Mapping):
        return dict(produced)
    result = dict(existing)
    result["matches"] = {
        **dict(existing.get("matches") or {}),
        **dict(produced.get("matches") or {}),
    }
    result["evaluations"] = {
        **dict(existing.get("evaluations") or {}),
        **dict(produced.get("evaluations") or {}),
    }
    # A successfully executed current pipeline is authoritative. If it could not
    # execute (or had no B4 records to evaluate), preserve a valid upstream/native
    # B5 block instead of relabelling its state.
    produced_status = produced.get("status")
    if (
        produced_status in {"ok", "degraded"}
        or not existing
        or (
            produced_status == "unavailable"
            and not existing.get("available")
        )
    ):
        for key, value in produced.items():
            if key not in {"matches", "evaluations"}:
                result[key] = value
    return result


def build_cloud_ops_runtime(
    org_id: str,
    sn_data: Optional[Mapping[str, Any]],
    *,
    bridge_records: Iterable[Mapping[str, Any]] = (),
    bridge_health: Optional[Mapping[str, Any]] = None,
    recurrence_fn: Callable[..., Any] = find_recurrences,
    ping_pong_fn: Callable[..., Any] = find_ping_pong,
    runbook_batch_fn: Callable[..., Any] = evaluate_runbook_recurrences,
) -> CloudOpsRuntimeResult:
    """Build the live Cloud Operations detector input for one org.

    Each stage degrades independently. Existing native/precomputed block entries
    are retained and merged by stable signature, which keeps the adapter additive
    for deployments that already provide one of these shapes.
    """

    org = str(org_id or "").strip()
    if not org:
        raise ValueError("org_id is required")
    payload = sn_data or {}
    payload_org = _text(payload.get("org_id"))
    if payload_org and payload_org != org:
        raise ValueError("ServiceNow payload does not belong to the requested org")
    existing = payload.get("cloud_ops")
    block: Dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}

    incidents = _incident_rows(payload)
    incidents_by_id = {
        incident_id: incident
        for incident in incidents
        if (incident_id := _incident_id(incident))
    }

    recurrence_objects: List[Any] = []
    recurrence_rows: List[Dict[str, Any]] = []
    try:
        recurrence_objects = list(recurrence_fn(payload, org_id=org))
        recurrence_rows = [
            _adapt_recurrence(record, incidents_by_id)
            for record in recurrence_objects
        ]
        recurrence_health = {
            "status": "ok",
            "records": len(recurrence_rows),
        }
    except Exception as exc:
        logger.warning(
            "cloud-ops recurrence assembly unavailable for org %s: [%s]",
            org,
            type(exc).__name__,
        )
        recurrence_health = {
            "status": "unavailable",
            "records": 0,
            "reason": type(exc).__name__,
        }

    ping_pong_rows: List[Dict[str, Any]] = []
    try:
        ping_pong_rows = [
            _adapt_ping_pong(finding, incidents_by_id)
            for finding in ping_pong_fn(payload, org_id=org)
        ]
        ping_pong_health = {"status": "ok", "records": len(ping_pong_rows)}
    except Exception as exc:
        logger.warning(
            "cloud-ops routing-loop assembly unavailable for org %s: [%s]",
            org,
            type(exc).__name__,
        )
        ping_pong_health = {
            "status": "unavailable",
            "records": 0,
            "reason": type(exc).__name__,
        }

    if recurrence_health["status"] == "ok":
        runbook_block, gap_rows, runbook_health = _runbook_outputs(
            org,
            payload,
            recurrence_rows,
            runbook_batch_fn=runbook_batch_fn,
        )
    else:
        runbook_block = {
            "available": False,
            "status": "unavailable",
            "reason": "recurrence_input_unavailable",
            "matches": {},
            "evaluations": {},
        }
        gap_rows = []
        runbook_health = {
            "status": "unavailable",
            "evaluated": 0,
            "matches": 0,
            "gaps": 0,
            "reason": "recurrence_input_unavailable",
        }

    try:
        event_rows, event_health = _aggregate_event_signatures(
            bridge_records,
            payload,
            org_id=org,
        )
    except Exception as exc:
        # The runner validates bridge records before checkpointing, so this guard
        # is for non-runner callers and keeps B4/B5 useful if B8 assembly fails.
        logger.warning(
            "cloud-ops bridge-event assembly unavailable for org %s: [%s]",
            org,
            type(exc).__name__,
        )
        event_rows = []
        event_health = {
            "status": "unavailable",
            "bridge_records": 0,
            "event_signatures": 0,
            "reason": type(exc).__name__,
        }
    if isinstance(bridge_health, Mapping):
        event_health["ingest"] = dict(bridge_health)
        ingest_status = str(bridge_health.get("status") or "")
        if ingest_status in {"degraded", "unavailable"}:
            event_health["status"] = (
                "degraded" if event_rows else "unavailable"
            )

    block["recurrence_records"] = _merge_keyed(
        block.get("recurrence_records"),
        recurrence_rows,
        key="signature",
        produced_wins=True,
    )
    block["oscillation_records"] = _merge_keyed(
        block.get("oscillation_records"),
        ping_pong_rows,
        key="signature",
        produced_wins=True,
    )
    # A native connector's event row may carry richer current-run correlation
    # state than its bridge twin, so retain it on a signature collision.
    block["event_signatures"] = _merge_keyed(
        block.get("event_signatures"),
        event_rows,
        key="signature",
        produced_wins=False,
    )
    block["runbook_matching"] = _merge_runbook_blocks(
        block.get("runbook_matching"),
        runbook_block,
    )
    block["documentation_gaps"] = _merge_keyed(
        block.get("documentation_gaps"),
        gap_rows,
        key="recurrence_id",
        produced_wins=True,
    )

    health = {
        "status": (
            "degraded"
            if any(
                stage.get("status") in {"unavailable", "degraded"}
                for stage in (
                    recurrence_health,
                    ping_pong_health,
                    runbook_health,
                    event_health,
                )
            )
            else "ok"
        ),
        "b4_recurrence": recurrence_health,
        "b4_routing": ping_pong_health,
        "b5_runbook_matching": runbook_health,
        "b8_event_bridge": event_health,
    }
    prior_runtime = block.get("runtime")
    block["runtime"] = {
        **(dict(prior_runtime) if isinstance(prior_runtime, Mapping) else {}),
        **health,
    }
    return CloudOpsRuntimeResult(block=block, health=health)


__all__ = [
    "CloudOpsRuntimeResult",
    "build_cloud_ops_runtime",
    "operational_event_from_bridge_record",
]
