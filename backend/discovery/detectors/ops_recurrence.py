"""MSP-B4 T3 — deterministic incident-resolution recurrence detector.

Incidents group only when their structured ``incident_identity_signature`` and
``resolution_signature`` are exactly equal.  There is no semantic similarity,
embedding lookup, free-text scoring, or resolution-note inspection here.  The
detector evaluates a configurable recent window and emits one auditable
``RecurrenceRecord`` per signature pair that meets the configured floor.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from app.provenance import EvidencePointer

from ..models import DetectorResult, make_detector_evaluation
from ..signals.resolution_signature import (
    compute_incident_identity_signature,
    compute_resolution_signature,
    incident_identity_signature_components,
    resolution_signature_components,
)
from .ops_recurrence_joins import (
    build_cmdb_index,
    build_ci_location_join,
    build_event_signature_join,
    build_evidence_trace,
    extract_event_signatures,
)

DETECTOR_ID = "OPS_RESOLUTION_RECURRENCE"

DEFAULT_RECURRENCE_FLOOR = 3
DEFAULT_RECURRENCE_WINDOW_DAYS = 30
DEFAULT_RECURRENCE_MAX_EXAMPLES = 3

RECURRENCE_FLOOR_ENV = "MSP_B4_RECURRENCE_FLOOR"
RECURRENCE_WINDOW_DAYS_ENV = "MSP_B4_RECURRENCE_WINDOW_DAYS"
RECURRENCE_MAX_EXAMPLES_ENV = "MSP_B4_RECURRENCE_MAX_EXAMPLES"

SIGNAL_METRICS = [
    "recurrence_loop_count",
    "max_recurrence_count",
    "max_median_time_to_resolve_seconds",
]

_POINTER_FIELDS = frozenset(EvidencePointer.__dataclass_fields__)


@dataclass(frozen=True)
class RecurrenceConfig:
    """Tunable recurrence sensitivity without detector-code changes."""

    floor: int = DEFAULT_RECURRENCE_FLOOR
    window_days: int = DEFAULT_RECURRENCE_WINDOW_DAYS
    max_examples: int = DEFAULT_RECURRENCE_MAX_EXAMPLES

    def __post_init__(self) -> None:
        values = {
            "floor": self.floor,
            "window_days": self.window_days,
            "max_examples": self.max_examples,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if self.floor < 2:
            raise ValueError("floor must be at least 2 for recurrence detection")
        if self.window_days < 1:
            raise ValueError("window_days must be at least 1")
        if not 1 <= self.max_examples <= 10:
            raise ValueError("max_examples must be between 1 and 10")


@dataclass(frozen=True)
class RecurrenceRecord:
    """One explainable, group-level operational recurrence."""

    record_id: str
    detector_id: str
    org_id: Optional[str]
    title: str
    explanation: str
    recurrence_count: int
    recurrence_floor: int
    evaluated_window: Dict[str, Any]
    median_time_to_resolve_seconds: Optional[float]
    measured_ttr_count: int
    total_time_to_resolve_seconds: int
    incident_identity_signature: str
    resolution_signature: str
    matched_fields: Tuple[str, ...]
    signature_components: Dict[str, Any]
    examples: Tuple[Dict[str, Any], ...]
    example_evidence_pointers: Tuple[Dict[str, Any], ...]
    # MSP-B4 T5 — soft enrichment joins (both optional; a recurrence still emits
    # unlocated/unlinked). ``ci_location`` / ``event_signature_link`` are the two
    # hop traces; ``evidence_trace`` is the assembled hop-by-hop provenance
    # showing which hops were present (and, when absent, not_available vs failed).
    ci_location: Dict[str, Any]
    event_signature_link: Dict[str, Any]
    evidence_trace: Dict[str, Any]
    # MSP-B5 T1 — the explicit runbook citations MSP-B4 mined from the resolution
    # notes, surfaced for deterministic runbook matching. ``cited_runbook_refs`` is
    # the sorted union of runbook identifiers across the whole loop;
    # ``runbook_citations`` records, per citing incident, its cited identifiers and
    # the observed evidence pointer back to that incident. Both default empty so a
    # recurrence with no cited runbook is unchanged (the documentation-gap case).
    cited_runbook_refs: Tuple[str, ...] = ()
    runbook_citations: Tuple[Dict[str, Any], ...] = ()

    @property
    def count(self) -> int:
        """Concise contract alias used by the MSP-B4 document."""
        return self.recurrence_count

    @property
    def window(self) -> Dict[str, Any]:
        """Concise contract alias used by the MSP-B4 document."""
        return dict(self.evaluated_window)

    @property
    def median_ttr(self) -> Optional[float]:
        """Concise contract alias used by the MSP-B4 document."""
        return self.median_time_to_resolve_seconds

    @property
    def grouped_signatures(self) -> Dict[str, str]:
        return {
            "incident_identity_signature": self.incident_identity_signature,
            "resolution_signature": self.resolution_signature,
        }

    def citing_incident_pointers(self) -> Tuple[Dict[str, Any], ...]:
        """Evidence pointers for the incidents that cited a runbook (MSP-B5 T1).

        One pointer per citing incident, in ``runbook_citations`` order — the
        "incidents that cited the runbook" side of a deterministic runbook match.
        """
        return tuple(
            dict(citation["evidence"])
            for citation in self.runbook_citations
            if isinstance(citation.get("evidence"), Mapping)
        )

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["matched_fields"] = list(self.matched_fields)
        payload["examples"] = [dict(example) for example in self.examples]
        payload["example_evidence_pointers"] = [
            dict(pointer) for pointer in self.example_evidence_pointers
        ]
        payload["cited_runbook_refs"] = list(self.cited_runbook_refs)
        payload["runbook_citations"] = [
            dict(citation) for citation in self.runbook_citations
        ]
        payload["count"] = self.count
        payload["window"] = self.window
        payload["median_ttr"] = self.median_ttr
        payload["grouped_signatures"] = self.grouped_signatures
        return payload


@dataclass(frozen=True)
class _IncidentCandidate:
    incident_sys_id: str
    incident_number: Optional[str]
    org_id: Optional[str]
    resolved_at: datetime
    time_to_resolve_seconds: Optional[int]
    incident_identity_signature: str
    resolution_signature: str
    identity_components: Dict[str, Any]
    resolution_components: Dict[str, Any]
    evidence: Dict[str, Any]
    source_url: Optional[str]
    # MSP-B4 T5 — soft-join inputs carried per candidate so the joins operate on
    # the exact incidents that formed the recurrence (never a broader scan).
    ci_reference: Optional[str]
    affected_ci_ids: Tuple[str, ...]
    event_signatures: Tuple[str, ...]
    # MSP-B5 T1 — the explicitly-cited runbook identifiers MSP-B4 already mined
    # from the (redacted) resolution note. Carried per candidate so the recurrence
    # can surface WHICH incidents cited a runbook, for deterministic matching.
    runbook_references: Tuple[str, ...] = ()


def _text(value: Any) -> Optional[str]:
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


def _reference_id(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("sys_id")
    return _text(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value)
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _safe_pointer(value: Any) -> Optional[Dict[str, Any]]:
    """Allow-list the shared evidence spine; never pass through source payloads."""
    if not isinstance(value, Mapping):
        return None
    pointer = EvidencePointer.from_dict(
        {key: value.get(key) for key in _POINTER_FIELDS if key in value}
    )
    return pointer.to_dict() if pointer.is_valid() else None


def _config_integer(
    mapping: Mapping[str, Any],
    keys: Tuple[str, ...],
    env_name: str,
    default: int,
) -> int:
    value: Any = None
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            value = mapping.get(key)
            break
    if value is None:
        value = os.getenv(env_name)
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{keys[0]} must be an integer")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{keys[0]} must be an integer") from exc


def resolve_recurrence_config(
    sn_data: Optional[Mapping[str, Any]] = None,
    config: Optional[Union[RecurrenceConfig, Mapping[str, Any]]] = None,
) -> RecurrenceConfig:
    """Resolve explicit, payload, or environment recurrence settings.

    Precedence: explicit ``config`` -> the current org's ServiceNow payload
    ``recurrence_config`` -> environment defaults for standalone/demo execution
    -> product defaults.  No module-level environment snapshot is used, so two
    runs can provide different org-scoped payload settings safely.
    """
    if isinstance(config, RecurrenceConfig):
        return config
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("config must be RecurrenceConfig or a mapping")

    source: Mapping[str, Any]
    if isinstance(config, Mapping):
        source = config
    else:
        payload_config = (sn_data or {}).get("recurrence_config")
        source = payload_config if isinstance(payload_config, Mapping) else {}

    return RecurrenceConfig(
        floor=_config_integer(
            source,
            ("floor", "recurrence_floor"),
            RECURRENCE_FLOOR_ENV,
            DEFAULT_RECURRENCE_FLOOR,
        ),
        window_days=_config_integer(
            source,
            ("window_days", "recurrence_window_days"),
            RECURRENCE_WINDOW_DAYS_ENV,
            DEFAULT_RECURRENCE_WINDOW_DAYS,
        ),
        max_examples=_config_integer(
            source,
            ("max_examples", "recurrence_max_examples"),
            RECURRENCE_MAX_EXAMPLES_ENV,
            DEFAULT_RECURRENCE_MAX_EXAMPLES,
        ),
    )


def _incidents(sn_data: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    metrics = (sn_data or {}).get("incident_metrics") or {}
    incidents = metrics.get("incidents") if isinstance(metrics, Mapping) else None
    if incidents is None:
        incidents = (sn_data or {}).get("incidents")
    if not isinstance(incidents, Sequence) or isinstance(incidents, (str, bytes)):
        return []
    return [incident for incident in incidents if isinstance(incident, Mapping)]


def _effective_org(
    sn_data: Optional[Mapping[str, Any]], org_id: Optional[str]
) -> Optional[str]:
    if org_id:
        return _text(org_id)
    payload_org = _text((sn_data or {}).get("org_id"))
    if payload_org:
        return payload_org
    metrics = (sn_data or {}).get("incident_metrics") or {}
    return _text(metrics.get("org_id")) if isinstance(metrics, Mapping) else None


def _candidate(
    incident: Mapping[str, Any],
    *,
    effective_org: Optional[str],
) -> Optional[_IncidentCandidate]:
    incident_org = _text(incident.get("org_id"))
    if effective_org and incident_org and incident_org != effective_org:
        return None

    resolution = incident.get("resolution")
    resolution = resolution if isinstance(resolution, Mapping) else {}
    is_resolved = bool(
        resolution.get("is_resolved")
        or resolution.get("resolution_signature")
        or incident.get("resolved_at")
        or incident.get("closed_at")
        or incident.get("close_code")
    )
    if not is_resolved:
        return None

    resolved_at = _parse_datetime(
        resolution.get("resolved_at")
        or resolution.get("closed_at")
        or incident.get("resolved_at")
        or incident.get("closed_at")
        or incident.get("source_timestamp")
    )
    if resolved_at is None:
        return None

    category = resolution.get("resolution_category", incident.get("category"))
    close_code = resolution.get("close_code", incident.get("close_code"))
    resolved_by_group = resolution.get(
        "resolved_by_group", incident.get("assignment_group")
    )
    ci_class = (
        resolution.get("ci_class")
        or incident.get("ci_class")
        or incident.get("cmdb_ci_class")
    )
    ci_id = _reference_id(incident.get("cmdb_ci"))
    short_description = incident.get("short_description")

    identity_signature = _text(resolution.get("incident_identity_signature"))
    if not identity_signature:
        identity_signature = compute_incident_identity_signature(
            category=category,
            short_description=short_description,
            ci_class=ci_class,
            ci_id=ci_id,
        )
    resolution_sig = _text(resolution.get("resolution_signature"))
    if not resolution_sig:
        resolution_sig = compute_resolution_signature(
            category=category,
            close_code=close_code,
            resolved_by_group=resolved_by_group,
            ci_class=ci_class,
            ci_id=ci_id,
        )

    incident_sys_id = _reference_id(
        incident.get("sys_id") or incident.get("id")
    ) or ""
    incident_number = _text(incident.get("number"))
    pointer = _safe_pointer(resolution.get("evidence")) or _safe_pointer(
        incident.get("evidence")
    )
    if pointer is None and incident_sys_id:
        pointer = EvidencePointer.observed(
            source_system="servicenow",
            source_artifact=incident_sys_id,
            source_timestamp=_format_datetime(resolved_at),
            source_artifact_type="record_id",
        ).to_dict()
    if pointer is None:
        return None

    raw_ttr = resolution.get(
        "time_to_resolve_seconds", incident.get("time_to_resolve_seconds")
    )
    time_to_resolve_seconds: Optional[int] = None
    if isinstance(raw_ttr, (int, float)) and not isinstance(raw_ttr, bool):
        if raw_ttr >= 0:
            time_to_resolve_seconds = int(raw_ttr)

    identity_components = incident_identity_signature_components(
        category=category,
        short_description=short_description,
        ci_class=ci_class,
        ci_id=ci_id,
    )
    # AC4 privacy: do not emit short-description tokens; a token can itself be
    # a person's name.  The stable signature and evidence pointer explain the
    # match without copying person-like free text into detector output.
    short_description_token_count = len(
        identity_components.pop("short_description_tokens", [])
    )
    identity_components["short_description_token_count"] = (
        short_description_token_count
    )

    # MSP-B4 T5 — soft-join inputs. The primary CI reference (its stable sys_id)
    # is the B3 CI-location key; ``affected_ci_references`` is the documented
    # fallback CI source. Explicit event signatures (stamped upstream by the
    # B0/B7 event bridge) are read from the incident and its resolution block —
    # explicit deterministic links only, never derived here.
    affected_ci_ids = tuple(
        ref_id
        for ref in (
            incident.get("affected_ci_references")
            if isinstance(incident.get("affected_ci_references"), Sequence)
            and not isinstance(incident.get("affected_ci_references"), (str, bytes))
            else []
        )
        if isinstance(ref, Mapping)
        and (ref_id := _reference_id(ref.get("ci_sys_id") or ref.get("ci_item")))
    )
    event_signatures = extract_event_signatures(incident, resolution)

    # MSP-B5 T1 — the deterministic runbook identifiers B4 mined from the note.
    # Read only; deduplicated + ordered so the recurrence is reproducible. No
    # free-text is read here (the raw note never reaches this record).
    runbook_references = _runbook_references(resolution.get("runbook_references"))

    return _IncidentCandidate(
        incident_sys_id=incident_sys_id,
        incident_number=incident_number,
        org_id=effective_org or incident_org,
        resolved_at=resolved_at,
        time_to_resolve_seconds=time_to_resolve_seconds,
        incident_identity_signature=identity_signature,
        resolution_signature=resolution_sig,
        identity_components=identity_components,
        resolution_components=resolution_signature_components(
            category=category,
            close_code=close_code,
            resolved_by_group=resolved_by_group,
            ci_class=ci_class,
            ci_id=ci_id,
        ),
        evidence=pointer,
        source_url=_text(incident.get("source_url")),
        ci_reference=ci_id or None,
        affected_ci_ids=affected_ci_ids,
        event_signatures=event_signatures,
        runbook_references=runbook_references,
    )


def _select_as_of(
    sn_data: Optional[Mapping[str, Any]],
    candidates: Sequence[_IncidentCandidate],
    explicit: Any,
) -> Optional[datetime]:
    if explicit is not None:
        return _parse_datetime(explicit)
    payload_as_of = (sn_data or {}).get("as_of")
    if payload_as_of is None:
        metrics = (sn_data or {}).get("incident_metrics") or {}
        if isinstance(metrics, Mapping):
            payload_as_of = metrics.get("as_of")
    parsed = _parse_datetime(payload_as_of)
    if parsed is not None:
        return parsed
    return max((candidate.resolved_at for candidate in candidates), default=None)


def _runbook_references(value: Any) -> Tuple[str, ...]:
    """Normalise a resolution block's ``runbook_references`` to a clean tuple.

    MSP-B4 captures these as already-uppercased structured tokens; here we only
    coerce to strings, drop blanks, deduplicate, and sort so the recurrence is
    deterministic regardless of source ordering. No parsing/guessing.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs = {text for ref in value if (text := _text(ref))}
    return tuple(sorted(refs))


def _example(candidate: _IncidentCandidate) -> Dict[str, Any]:
    return {
        "incident_sys_id": candidate.incident_sys_id,
        "incident_number": candidate.incident_number,
        "resolved_at": _format_datetime(candidate.resolved_at),
        "time_to_resolve_seconds": candidate.time_to_resolve_seconds,
        "source_url": candidate.source_url,
        "evidence": dict(candidate.evidence),
    }


def find_recurrences(
    sn_data: Optional[Mapping[str, Any]],
    *,
    config: Optional[Union[RecurrenceConfig, Mapping[str, Any]]] = None,
    as_of: Any = None,
    org_id: Optional[str] = None,
) -> List[RecurrenceRecord]:
    """Return qualifying recurrence records for one deterministic run window."""
    resolved_config = resolve_recurrence_config(sn_data, config)
    effective_org = _effective_org(sn_data, org_id)
    candidates = [
        candidate
        for incident in _incidents(sn_data)
        if (candidate := _candidate(incident, effective_org=effective_org)) is not None
    ]
    candidate_orgs = {
        candidate.org_id for candidate in candidates if candidate.org_id
    }
    if effective_org is None:
        if len(candidate_orgs) > 1:
            raise ValueError(
                "org_id is required when recurrence input contains multiple organizations"
            )
        if candidate_orgs:
            effective_org = next(iter(candidate_orgs))
    window_end = _select_as_of(sn_data, candidates, as_of)
    if window_end is None:
        return []
    window_start = window_end - timedelta(days=resolved_config.window_days)

    # MSP-B4 T5 — build the B3 CMDB index once for the whole run (org-scoped).
    # ``None`` means B3 is not available; a dict (possibly empty) means it is.
    cmdb_index = build_cmdb_index(sn_data, org_id=effective_org)

    in_window = [
        candidate
        for candidate in candidates
        if window_start <= candidate.resolved_at <= window_end
    ]
    groups: Dict[Tuple[str, str], List[_IncidentCandidate]] = {}
    for candidate in in_window:
        key = (
            candidate.incident_identity_signature,
            candidate.resolution_signature,
        )
        groups.setdefault(key, []).append(candidate)

    records: List[RecurrenceRecord] = []
    for signature_pair in sorted(groups):
        members = sorted(
            groups[signature_pair],
            key=lambda member: (
                member.resolved_at,
                member.incident_sys_id,
                member.incident_number or "",
            ),
        )
        if len(members) < resolved_config.floor:
            continue

        ttrs = [
            member.time_to_resolve_seconds
            for member in members
            if member.time_to_resolve_seconds is not None
        ]
        median_ttr = float(median(ttrs)) if ttrs else None
        examples = tuple(
            _example(member)
            for member in members[: resolved_config.max_examples]
        )
        example_pointers = tuple(
            dict(example["evidence"]) for example in examples
        )
        # MSP-B5 T1 — surface the explicit runbook citations for deterministic
        # matching. Aggregated over ALL members (not just the capped examples) so
        # no citing incident is dropped: ``runbook_citations`` records, per citing
        # incident, its cited identifiers + the observed evidence pointer back to
        # that incident (the "incidents that cited the runbook" side of a match);
        # ``cited_runbook_refs`` is the sorted union across the loop.
        runbook_citations = tuple(
            {
                "incident_sys_id": member.incident_sys_id,
                "runbook_references": list(member.runbook_references),
                "evidence": dict(member.evidence),
            }
            for member in members
            if member.runbook_references
        )
        cited_runbook_refs = tuple(
            sorted(
                {ref for member in members for ref in member.runbook_references}
            )
        )
        # MSP-B4 T5 — soft enrichment joins over the exact recurrence members.
        ci_join = build_ci_location_join(members, cmdb_index)
        event_join = build_event_signature_join(members)
        evidence_trace = build_evidence_trace(
            members, ci_join, event_join, example_pointers
        )
        identity_signature, resolution_signature = signature_pair
        record_material = {
            "org_id": effective_org or members[0].org_id,
            "identity_signature": identity_signature,
            "resolution_signature": resolution_signature,
            "window_start": _format_datetime(window_start),
            "window_end": _format_datetime(window_end),
        }
        digest = hashlib.sha256(
            json.dumps(
                record_material, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:20]

        recurrence_count = len(members)
        explanation = (
            f"{recurrence_count} incidents had the same structured incident "
            f"identity and resolution pattern within {resolved_config.window_days} "
            "days. "
            + (
                f"Their median time to resolve was {median_ttr:g} seconds."
                if median_ttr is not None
                else "No measured resolution duration was available."
            )
        )
        records.append(
            RecurrenceRecord(
                record_id=f"servicenow:resolution-recurrence:{digest}",
                detector_id=DETECTOR_ID,
                org_id=effective_org or members[0].org_id,
                title="Repeated incidents resolved the same way",
                explanation=explanation,
                recurrence_count=recurrence_count,
                recurrence_floor=resolved_config.floor,
                evaluated_window={
                    "start": _format_datetime(window_start),
                    "end": _format_datetime(window_end),
                    "days": resolved_config.window_days,
                },
                median_time_to_resolve_seconds=median_ttr,
                measured_ttr_count=len(ttrs),
                total_time_to_resolve_seconds=sum(ttrs),
                incident_identity_signature=identity_signature,
                resolution_signature=resolution_signature,
                matched_fields=(
                    "category",
                    "ci_or_ci_class",
                    "normalized_short_description",
                    "close_code",
                    "resolved_by_assignment_group",
                ),
                signature_components={
                    "incident_identity": dict(members[0].identity_components),
                    "resolution": dict(members[0].resolution_components),
                },
                examples=examples,
                example_evidence_pointers=example_pointers,
                ci_location=ci_join.to_trace(),
                event_signature_link=event_join.to_trace(),
                evidence_trace=evidence_trace,
                cited_runbook_refs=cited_runbook_refs,
                runbook_citations=runbook_citations,
            )
        )

    records.sort(
        key=lambda record: (
            -record.recurrence_count,
            record.incident_identity_signature,
            record.resolution_signature,
            record.record_id,
        )
    )
    return records


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Union[RecurrenceConfig, Mapping[str, Any]]] = None,
    as_of: Any = None,
):
    resolved_config = resolve_recurrence_config(sn_data, config)
    records = find_recurrences(
        sn_data,
        config=resolved_config,
        as_of=as_of,
    )
    max_count = max((record.recurrence_count for record in records), default=0)
    medians = [
        record.median_time_to_resolve_seconds
        for record in records
        if record.median_time_to_resolve_seconds is not None
    ]
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(max_count),
        threshold=float(resolved_config.floor),
        fired=bool(records),
        raw_evidence={
            "recurrence_loop_count": len(records),
            "max_recurrence_count": max_count,
            "max_median_time_to_resolve_seconds": max(medians, default=0.0),
            "records": [record.as_dict() for record in records],
        },
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Union[RecurrenceConfig, Mapping[str, Any]]] = None,
    as_of: Any = None,
) -> List[DetectorResult]:
    """Emit one observed DetectorResult per qualifying recurrence record."""
    resolved_config = resolve_recurrence_config(sn_data, config)
    return [
        DetectorResult(
            detector_id=DETECTOR_ID,
            signal_source="servicenow",
            metric_value=float(record.recurrence_count),
            threshold=float(resolved_config.floor),
            raw_evidence=record.as_dict(),
            provenance_type="observed",
        )
        for record in find_recurrences(
            sn_data,
            config=resolved_config,
            as_of=as_of,
        )
    ]
