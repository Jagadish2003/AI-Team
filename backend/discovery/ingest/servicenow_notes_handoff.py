"""MSP-B4 T6 — redaction-before-indexing for ServiceNow resolution notes.

The seam where an incident's resolution note is handed to the R18-B1 retrieval
substrate for B5 (runbook matching / semantic retrieval) preparation. Engineers
routinely paste credentials, tokens, or connection strings into ServiceNow work
/ resolution notes, so the note MUST be redacted before it can reach any
retrievable artifact.

One redaction path, reused — not a second one
---------------------------------------------
Redaction goes through the SAME choke point every content producer uses:
:func:`discovery.ingest.secret_redaction.scan_and_redact` (R18-A2 / AT-531, the
"redact before index, always" rule). B4 introduces NO new redaction behaviour —
it reuses the existing signature set and the existing ``ingestion.secret_redacted``
telemetry event, exactly as ``git_content.py`` does for committed secrets.

Where the raw note lives
------------------------
The redaction runs BEFORE anything that could persist note content — before the
:class:`~app.retrieval.ingest.ContentArtifact` is built, before chunking,
indexing, logging, and telemetry. The retrieval index therefore receives
SANITIZED text only. The raw note is never persisted by AgentIQ: it stays
reachable only through the incident's **evidence pointer** and its
access-controlled ServiceNow source path (``source_url``), which every handed-off
artifact carries in provenance so an authorized user can still trace a retrieval
hit back to the source record.

Org scoping
-----------
``org_id`` is explicit and every artifact is written into that org's partition
and no other (the substrate's ``ingest_content`` guarantee, R17-D3). This module
never trusts an org id carried on the content.

B5 wiring
---------
This is the callable seam; it is deliberately NOT auto-run inside the main
ServiceNow ``ingest()`` (that would write to the retrieval store before B5 exists
to consume it). B5 invokes :func:`ingest_resolution_notes`; the redaction
guarantee holds whenever it does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.provenance import EvidencePointer

from .secret_redaction import RedactionOutcome, scan_and_redact

logger = logging.getLogger(__name__)

#: Substrate source vocabulary for a retrieval hit that came from a ServiceNow
#: incident's resolution note (so provenance shows the correct source system).
RESOLUTION_NOTE_SOURCE_SYSTEM = "servicenow"
#: Resolution notes are free-text prose (the substrate's prose chunking policy).
RESOLUTION_NOTE_CONTENT_TYPE = "prose"
#: The connector id stamped on the reused ``ingestion.secret_redacted`` event.
RESOLUTION_NOTE_CONNECTOR_ID = "servicenow_resolution_notes"

# Result-type aliases kept loose so tests can inject a fake substrate entry point.
IngestFn = Callable[[str, List[Any]], Any]
RecordEventFn = Callable[[str, Dict[str, Any]], Any]


def _note_text(value: Any) -> Optional[str]:
    """Read a resolution-note scalar, tolerating a ``display_value=all`` object."""
    if isinstance(value, dict):
        value = (
            value.get("display_value")
            or value.get("value")
            or value.get("displayValue")
        )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _incident_identity(incident: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return the incident's stable ``(sys_id, number)`` for evidence + keying."""
    resolution = incident.get("resolution") if isinstance(incident, dict) else None
    sys_id = None
    if isinstance(resolution, dict):
        sys_id = resolution.get("incident_sys_id") or None
    sys_id = sys_id or _note_text(incident.get("sys_id"))
    number = _note_text(incident.get("number")) or _note_text(incident.get("id"))
    return sys_id, number


def _evidence_pointer(incident: Dict[str, Any], sys_id: Optional[str],
                      number: Optional[str]) -> Dict[str, Any]:
    """Reuse T1's evidence pointer when present, else build the same observed one.

    Keeping the pointer identical to the resolution block's means a retrieval hit
    and the finding both trace to the exact same source record.
    """
    resolution = incident.get("resolution") if isinstance(incident, dict) else None
    if isinstance(resolution, dict):
        pointer = resolution.get("notes_evidence") or resolution.get("evidence")
        if isinstance(pointer, dict):
            return dict(pointer)
    artifact = sys_id or number or ""
    return EvidencePointer.observed(
        source_system=RESOLUTION_NOTE_SOURCE_SYSTEM,
        source_artifact=artifact,
        source_timestamp=_source_timestamp(incident),
        source_artifact_type="record_id" if sys_id else None,
    ).to_dict()


def _source_timestamp(incident: Dict[str, Any]) -> Optional[str]:
    resolution = incident.get("resolution") if isinstance(incident, dict) else None
    if isinstance(resolution, dict):
        ts = (
            resolution.get("resolved_at")
            or resolution.get("closed_at")
            or resolution.get("created_at")
        )
        if ts:
            return ts
    return _note_text(incident.get("source_timestamp"))


@dataclass
class ResolutionNoteHandoffResult:
    """Outcome of one :func:`ingest_resolution_notes` run.

    ``notes_seen`` counts incidents that carried a resolution note; ``redacted``
    counts how many of those had at least one secret removed before hand-off.
    ``ingest_result`` is whatever the substrate entry point returned (or ``None``
    when there was nothing to hand off).
    """

    org_id: str
    notes_seen: int = 0
    artifacts_handed_off: int = 0
    redacted: int = 0
    secrets_redacted: int = 0
    pattern_types: List[str] = field(default_factory=list)
    ingest_result: Any = None


def build_resolution_note_artifact(
    incident: Dict[str, Any],
) -> Optional[Tuple[Any, RedactionOutcome, Dict[str, Any]]]:
    """Build a substrate artifact for one incident's resolution note, REDACTED.

    Returns ``(ContentArtifact, RedactionOutcome, provenance)`` or ``None`` when
    the incident carries no resolution note. The redaction happens HERE, before
    the artifact exists — so no unredacted note text is ever placed on a
    substrate-bound object.
    """
    from app.retrieval.ingest import ContentArtifact

    note = _note_text(incident.get("close_notes"))
    if not note:
        return None

    # Redact BEFORE building the artifact — sanitized text only from here on.
    outcome = scan_and_redact(note)

    sys_id, number = _incident_identity(incident)
    source_artifact = sys_id or number
    if not source_artifact:
        return None
    evidence_pointer = _evidence_pointer(incident, sys_id, number)

    provenance: Dict[str, Any] = {
        "origin": "observed",
        "source_system": RESOLUTION_NOTE_SOURCE_SYSTEM,
        "incident_sys_id": sys_id,
        "incident_number": number,
        "category": _note_text(incident.get("category")),
        "source_url": _note_text(incident.get("source_url"))
        or evidence_pointer.get("source_url"),
        # The evidence pointer travels with every chunk so an authorized user can
        # trace a retrieval hit back to the access-controlled source record.
        "evidence_pointer": evidence_pointer,
    }

    artifact = ContentArtifact(
        source_system=RESOLUTION_NOTE_SOURCE_SYSTEM,
        source_artifact=str(source_artifact),
        content=outcome.text,  # SANITIZED text — never the raw note
        content_type=RESOLUTION_NOTE_CONTENT_TYPE,
        source_timestamp=_source_timestamp(incident),
        provenance=provenance,
    )
    return artifact, outcome, provenance


def ingest_resolution_notes(
    org_id: str,
    incidents: Iterable[Dict[str, Any]],
    *,
    ingest_fn: Optional[IngestFn] = None,
    record_event_fn: Optional[RecordEventFn] = None,
) -> ResolutionNoteHandoffResult:
    """Redact every incident's resolution note and hand the sanitized text off.

    For each incident that carries a resolution note:

    1. redact through the shared R18-A2 scanner (BEFORE any artifact/log/chunk),
    2. record an ``ingestion.secret_redacted`` telemetry event + WARNING when a
       secret was removed (pattern types + counts only, never the value),
    3. hand the sanitized :class:`ContentArtifact` to the retrieval substrate.

    ``ingest_fn`` defaults to the real ``retrieval.ingest.ingest_content`` and is
    injectable for tests. ``org_id`` scopes every write to that org's partition.
    """
    if org_id is None or not str(org_id).strip():
        raise ValueError("org_id is required")
    if ingest_fn is None:
        from app.retrieval.ingest import ingest_content

        ingest_fn = ingest_content

    result = ResolutionNoteHandoffResult(org_id=org_id)
    artifacts: List[Any] = []
    for incident in incidents or []:
        if not isinstance(incident, dict):
            continue
        built = build_resolution_note_artifact(incident)
        if built is None:
            continue
        artifact, outcome, _prov = built
        result.notes_seen += 1
        if outcome.redacted:
            result.redacted += 1
            result.secrets_redacted += outcome.count
            result.pattern_types.extend(outcome.pattern_types)
            _record_redaction(org_id, artifact, outcome, record_event_fn)
        artifacts.append(artifact)

    if artifacts:
        result.artifacts_handed_off = len(artifacts)
        result.ingest_result = ingest_fn(org_id, artifacts)
    return result


def _record_redaction(
    org_id: str,
    artifact: Any,
    outcome: RedactionOutcome,
    record_event_fn: Optional[RecordEventFn],
) -> None:
    """Record one note's secret redaction for run-health visibility (reused AC5).

    Emits the SAME ``ingestion.secret_redacted`` event git content uses —
    identifiers, pattern types, and counts only, NEVER the secret value.
    Fire-and-forget: an observability failure never blocks the redaction.
    """
    pattern_types = sorted(set(outcome.pattern_types))
    try:
        if record_event_fn is None:
            from app.telemetry import record_event

            record_event_fn = record_event
        from app.provenance import utc_now_iso

        record_event_fn(
            "ingestion.secret_redacted",
            {
                "org_id": org_id,
                "connector_id": RESOLUTION_NOTE_CONNECTOR_ID,
                "source_system": RESOLUTION_NOTE_SOURCE_SYSTEM,
                "source_artifact": getattr(artifact, "source_artifact", ""),
                "content_type": getattr(artifact, "content_type", ""),
                "redaction_count": outcome.count,
                "pattern_types": pattern_types,
                "observed_at": utc_now_iso(),
            },
        )
    except Exception:  # pragma: no cover — observability must not break ingestion
        logger.warning(
            "servicenow_notes: failed to record secret-redaction event for %s",
            getattr(artifact, "source_artifact", "?"),
        )
    logger.warning(
        "servicenow_notes: redacted %d secret(s) [%s] from incident %s before "
        "indexing (org=%s)",
        outcome.count,
        ",".join(pattern_types),
        getattr(artifact, "source_artifact", "?"),
        org_id,
    )
