"""MSP-B5 end-to-end orchestration from B4 recurrences to B6 output.

The task modules deliberately keep citation resolution, semantic retrieval,
scoring, lifecycle decisions, composite presentation, and documentation gaps
small and testable. This module is the production seam that composes them in the
required order and preserves the story's origin discipline:

1. explicit citations resolve first and produce observed matches;
2. only an unresolved recurrence reaches runbook-scoped semantic retrieval;
3. semantic candidates can produce proposed matches, never observed facts;
4. persisted analyst state determines the current B6 presentation; and
5. a gap is emitted only when the current lifecycle has no active match and both
   matching paths completed successfully.

Resolution-note text is passed through the shared B4 secret scanner immediately
before query construction. Raw text is never stored on a pipeline result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from app.runbook_match_decisions import (
    RunbookMatchDecisionStore,
    build_current_runbook_composite,
)
from discovery.ingest.secret_redaction import scan_and_redact
from discovery.ingest.servicenow_notes_handoff import ingest_resolution_notes
from discovery.signals.evidence_store import OrgScopeError

from .ops_recurrence import RecurrenceConfig, RecurrenceRecord, find_recurrences
from .runbook_composite import (
    RUNBOOK_ABSENT,
    DocumentedRepeatedManualFinding,
)
from .runbook_documentation_gap import (
    DocumentationGapConfig,
    DocumentationGapEvaluation,
    evaluate_documentation_gap,
)
from .runbook_match import (
    CITATION_RESOLUTION_OK,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    CitationResolutionResult,
    RunbookLibrary,
    RunbookMatch,
    RunbookRetrievalConfig,
    RunbookRetrievalResult,
    RunbookScoringConfig,
    default_runbook_library,
    propose_runbook_match,
    resolve_runbook_citations,
    retrieve_runbook_candidates,
)

logger = logging.getLogger(__name__)


def _require_org(org_id: Any) -> str:
    org = str(org_id).strip() if org_id is not None else ""
    if not org:
        raise OrgScopeError("org_id is required for runbook matching")
    return org


def _recurrence_id(rec: Any) -> str:
    value = str(getattr(rec, "record_id", "") or "").strip()
    if not value:
        raise ValueError("recurrence_id is required for runbook matching")
    return value


def _validate_recurrence(org: str, rec: Any) -> str:
    recurrence_id = _recurrence_id(rec)
    rec_org = str(getattr(rec, "org_id", "") or "").strip()
    if not rec_org:
        raise OrgScopeError("recurrence org_id is required for runbook matching")
    if rec_org != org:
        raise OrgScopeError(
            f"recurrence belongs to org {rec_org!r}, cannot match under {org!r}"
        )
    count = int(getattr(rec, "recurrence_count", 0) or 0)
    floor = int(getattr(rec, "recurrence_floor", 0) or 0)
    if floor > 0 and count < floor:
        raise ValueError("recurrence does not meet its configured composite floor")
    return recurrence_id


@dataclass(frozen=True)
class QueryRedactionSummary:
    """Safe summary of query-text redaction; raw text is intentionally absent."""

    input_count: int
    output_count: int
    notes_redacted: int
    secrets_redacted: int
    pattern_types: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "input_count": self.input_count,
            "output_count": self.output_count,
            "notes_redacted": self.notes_redacted,
            "secrets_redacted": self.secrets_redacted,
            "pattern_types": list(self.pattern_types),
        }


@dataclass(frozen=True)
class _RedactedQueryTexts:
    texts: Tuple[str, ...]
    summary: QueryRedactionSummary


def _redact_query_texts(values: Sequence[Any]) -> _RedactedQueryTexts:
    sanitized = []
    seen = set()
    input_count = 0
    notes_redacted = 0
    secrets_redacted = 0
    pattern_types = set()
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        input_count += 1
        outcome = scan_and_redact(text)
        if outcome.redacted:
            notes_redacted += 1
            secrets_redacted += outcome.count
            pattern_types.update(outcome.pattern_types)
        cleaned = " ".join(outcome.text.split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            sanitized.append(cleaned)
    return _RedactedQueryTexts(
        texts=tuple(sanitized),
        summary=QueryRedactionSummary(
            input_count=input_count,
            output_count=len(sanitized),
            notes_redacted=notes_redacted,
            secrets_redacted=secrets_redacted,
            pattern_types=tuple(sorted(pattern_types)),
        ),
    )


@dataclass(frozen=True)
class RunbookPipelineResult:
    org_id: str
    recurrence_id: str
    state: str
    retrieval_performed: bool
    query_redaction: QueryRedactionSummary
    citation_resolution: CitationResolutionResult
    retrieval: RunbookRetrievalResult
    detected_match: Optional[RunbookMatch]
    composite: DocumentedRepeatedManualFinding
    documentation_gap: DocumentationGapEvaluation

    def as_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "recurrence_id": self.recurrence_id,
            "state": self.state,
            "retrieval_performed": self.retrieval_performed,
            "query_redaction": self.query_redaction.as_dict(),
            "citation_resolution": self.citation_resolution.as_dict(),
            "retrieval": self.retrieval.as_dict(),
            "detected_match": (
                self.detected_match.as_dict() if self.detected_match else None
            ),
            "composite": self.composite.as_dict(),
            "documentation_gap": self.documentation_gap.as_dict(),
        }


@dataclass(frozen=True)
class RunbookBatchResult:
    org_id: str
    note_handoff: Dict[str, Any]
    recurrences: Tuple[RunbookPipelineResult, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "note_handoff": dict(self.note_handoff),
            "recurrences": [result.as_dict() for result in self.recurrences],
        }


def _effective_match(
    composite: DocumentedRepeatedManualFinding,
) -> Optional[RunbookMatch]:
    payload = composite.runbook_match
    return RunbookMatch.from_dict(payload) if payload else None


def evaluate_runbook_recurrence(
    org_id: str,
    rec: RecurrenceRecord,
    *,
    resolution_texts: Sequence[Any] = (),
    citation_library: Optional[RunbookLibrary] = None,
    retrieval_config: Optional[RunbookRetrievalConfig] = None,
    scoring_config: Optional[RunbookScoringConfig] = None,
    gap_config: Optional[DocumentationGapConfig] = None,
    retrieve_fn: Optional[Callable[..., list]] = None,
    embedding_available_fn: Optional[Callable[[str], bool]] = None,
    decision_store: Optional[RunbookMatchDecisionStore] = None,
) -> RunbookPipelineResult:
    """Run one recurrence through every MSP-B5 stage in the required order."""
    org = _require_org(org_id)
    recurrence_id = _validate_recurrence(org, rec)
    library = citation_library or default_runbook_library()
    redacted = _redact_query_texts(resolution_texts)

    citation = resolve_runbook_citations(org, rec, library)
    detected_match = citation.match
    retrieval_performed = False

    if citation.match is not None:
        # An explicit citation is already observed source truth. Semantic retrieval
        # is neither necessary nor allowed to weaken or relabel it.
        retrieval = RunbookRetrievalResult(
            status=RETRIEVAL_OK,
            query="",
            candidates=(),
        )
    elif citation.status != CITATION_RESOLUTION_OK:
        retrieval = RunbookRetrievalResult(
            status=RETRIEVAL_UNAVAILABLE,
            query="",
            candidates=(),
        )
    else:
        retrieval_performed = True
        retrieval = retrieve_runbook_candidates(
            org,
            rec,
            config=retrieval_config,
            redacted_texts=redacted.texts,
            retrieve_fn=retrieve_fn,
            embedding_available_fn=embedding_available_fn,
        )
        if retrieval.status == RETRIEVAL_OK:
            detected_match = propose_runbook_match(
                org,
                rec,
                retrieval.candidates,
                config=scoring_config,
            )

    composite = build_current_runbook_composite(
        org,
        rec,
        runbook_match=detected_match,
        retrieval_status=retrieval.status,
        store=decision_store,
    )
    current_match = _effective_match(composite)
    proposal_dismissed = bool(
        detected_match is not None
        and detected_match.match_state == "proposed"
        and composite.runbook_state == RUNBOOK_ABSENT
    )
    documentation_gap = evaluate_documentation_gap(
        org,
        rec,
        retrieval_result=retrieval,
        citation_library=library,
        scoring_config=scoring_config,
        config=gap_config,
        effective_match=current_match,
        proposal_dismissed=proposal_dismissed,
    )
    return RunbookPipelineResult(
        org_id=org,
        recurrence_id=recurrence_id,
        state=composite.runbook_state,
        retrieval_performed=retrieval_performed,
        query_redaction=redacted.summary,
        citation_resolution=citation,
        retrieval=retrieval,
        detected_match=detected_match,
        composite=composite,
        documentation_gap=documentation_gap,
    )


def _payload_incidents(sn_data: Optional[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    payload = sn_data or {}
    metrics = payload.get("incident_metrics")
    incidents = metrics.get("incidents") if isinstance(metrics, Mapping) else None
    if incidents is None:
        incidents = payload.get("incidents")
    if not isinstance(incidents, Sequence) or isinstance(incidents, (str, bytes)):
        return []
    return [item for item in incidents if isinstance(item, Mapping)]


def _scalar(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("display_value") or value.get("value")
    text = str(value or "").strip()
    return text or None


def _incident_id(incident: Mapping[str, Any]) -> Optional[str]:
    resolution = incident.get("resolution")
    if isinstance(resolution, Mapping):
        resolved = _scalar(resolution.get("incident_sys_id"))
        if resolved:
            return resolved
    return _scalar(incident.get("sys_id") or incident.get("id"))


def _resolution_texts_for(
    rec: RecurrenceRecord,
    incidents: Sequence[Mapping[str, Any]],
) -> Tuple[str, ...]:
    by_id = {
        incident_id: incident
        for incident in incidents
        if (incident_id := _incident_id(incident))
    }
    texts = []
    for example in rec.examples:
        incident = by_id.get(str(example.get("incident_sys_id") or ""))
        if incident is None:
            continue
        note = _scalar(incident.get("close_notes"))
        if note:
            texts.append(note)
    return tuple(texts)


def _handoff_summary(result: Any, *, status: str) -> Dict[str, Any]:
    return {
        "status": status,
        "notes_seen": int(getattr(result, "notes_seen", 0) or 0),
        "artifacts_handed_off": int(
            getattr(result, "artifacts_handed_off", 0) or 0
        ),
        "notes_redacted": int(getattr(result, "redacted", 0) or 0),
        "secrets_redacted": int(getattr(result, "secrets_redacted", 0) or 0),
        "pattern_types": sorted(set(getattr(result, "pattern_types", ()) or ())),
    }


def evaluate_runbook_recurrences(
    org_id: str,
    sn_data: Optional[Mapping[str, Any]],
    *,
    recurrence_config: Optional[Union[RecurrenceConfig, Mapping[str, Any]]] = None,
    as_of: Any = None,
    citation_library: Optional[RunbookLibrary] = None,
    retrieval_config: Optional[RunbookRetrievalConfig] = None,
    scoring_config: Optional[RunbookScoringConfig] = None,
    gap_config: Optional[DocumentationGapConfig] = None,
    retrieve_fn: Optional[Callable[..., list]] = None,
    embedding_available_fn: Optional[Callable[[str], bool]] = None,
    decision_store: Optional[RunbookMatchDecisionStore] = None,
    note_ingest_fn: Optional[Callable[[str, list], Any]] = None,
    record_event_fn: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
) -> RunbookBatchResult:
    """Run MSP-B5 from a current org's ServiceNow payload through B6 output.

    B4's resolution-note handoff runs once for the batch and is deliberately
    degradable. Matching continues with the safe structured recurrence pattern if
    note indexing is unavailable; the failure never invents a documentation gap
    because semantic retrieval still reports its own availability independently.
    """
    org = _require_org(org_id)
    payload_org = _scalar((sn_data or {}).get("org_id"))
    if payload_org and payload_org != org:
        raise OrgScopeError(
            f"ServiceNow payload belongs to org {payload_org!r}, not {org!r}"
        )
    incidents = _payload_incidents(sn_data)
    try:
        handoff = ingest_resolution_notes(
            org,
            incidents,
            ingest_fn=note_ingest_fn,
            record_event_fn=record_event_fn,
        )
        handoff_summary = _handoff_summary(handoff, status="ok")
    except Exception as exc:  # B5 is intentionally degradable; never expose note text.
        logger.warning(
            "ServiceNow resolution-note handoff unavailable for org %s: [%s]",
            org,
            type(exc).__name__,
        )
        handoff_summary = _handoff_summary(None, status="unavailable")

    library = citation_library or default_runbook_library()
    recurrences = find_recurrences(
        sn_data,
        config=recurrence_config,
        as_of=as_of,
        org_id=org,
    )
    results = tuple(
        evaluate_runbook_recurrence(
            org,
            rec,
            resolution_texts=_resolution_texts_for(rec, incidents),
            citation_library=library,
            retrieval_config=retrieval_config,
            scoring_config=scoring_config,
            gap_config=gap_config,
            retrieve_fn=retrieve_fn,
            embedding_available_fn=embedding_available_fn,
            decision_store=decision_store,
        )
        for rec in recurrences
    )
    return RunbookBatchResult(
        org_id=org,
        note_handoff=handoff_summary,
        recurrences=results,
    )


__all__ = [
    "QueryRedactionSummary",
    "RunbookBatchResult",
    "RunbookPipelineResult",
    "evaluate_runbook_recurrence",
    "evaluate_runbook_recurrences",
]
