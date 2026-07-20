"""MSP-B11 T5 / AT-700 — redaction-before-indexing for ServiceNow security notes.

The seam where a Security Incident Response (SIR) record's work notes are handed
to the R18-B1 retrieval substrate for later semantic use (MSP-B12 / B5). Security
work notes are the HIGHEST-RISK note corpus on the platform: analysts routinely
paste indicators of compromise (IPs, hashes, domains), copied commands, scanner
fragments, access tokens, and credential dumps into them. The note MUST be
redacted with the extended security-pattern set BEFORE it can reach any
retrievable artifact ("workload, not weakness" — the platform never indexes the
customer's exposure).

One redaction path, extended — not a second one
-----------------------------------------------
Redaction goes through the SAME shared choke point every content producer uses
(:mod:`discovery.ingest.secret_redaction`, R18-A2 / AT-531), via its security
entry point :func:`~discovery.ingest.secret_redaction.scan_and_redact_security`
(base secret set + IOC/artefact indicators). No new redaction mechanism is
introduced and the reused ``ingestion.secret_redacted`` telemetry event records
pattern types + counts only — never the matched value.

Field scope stays intact
-------------------------
This path does NOT widen the SIR workflow-signal field scope (MSP-B11 AC2). The
detector-visible signal built by ``servicenow.py`` still contains workflow fields
only. Notes travel on a SEPARATE, deliberately-invoked seam (like B4's resolution
notes), and only the SANITIZED text plus an access-controlled evidence pointer
ever leave it. The raw note is never persisted by AgentIQ: it stays reachable
only through the incident's evidence pointer and its access-controlled ServiceNow
``source_url``, so an authorized workflow can still trace a retrieval hit back to
the source record without the note content living in the index.

Org scoping
-----------
``org_id`` is explicit and every artifact is written into that org's partition
and no other (the substrate's ``ingest_content`` guarantee). This module never
trusts an org id carried on the content.

B12 wiring
----------
This is the callable seam; it is deliberately NOT auto-run inside the SecOps
``ingest`` path (that would write to the retrieval store before a consumer
exists). B12 / retrieval preparation invokes :func:`ingest_security_notes`; the
redaction guarantee holds whenever it does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.provenance import EvidencePointer

from .secret_redaction import RedactionOutcome, scan_and_redact_security

logger = logging.getLogger(__name__)

#: Substrate source vocabulary for a retrieval hit that came from a ServiceNow
#: security incident's work note (so provenance shows the correct source system).
SECURITY_NOTE_SOURCE_SYSTEM = "servicenow"
#: Source-type marker distinguishing a SIR note from a B4 resolution note.
SECURITY_NOTE_SOURCE_TYPE = "servicenow_security_incident"
#: Security notes are free-text prose (the substrate's prose chunking policy).
SECURITY_NOTE_CONTENT_TYPE = "prose"
#: The connector id stamped on the reused ``ingestion.secret_redacted`` event.
SECURITY_NOTE_CONNECTOR_ID = "servicenow_security_notes"

#: The note-bearing fields on a SIR record, in the order they are concatenated
#: into the single per-incident artifact. Every one of these is a free-text field
#: EXCLUDED from the workflow signal (see ``SIR_FORBIDDEN_FIELDS``); they are read
#: ONLY here, redacted, and only then handed off.
SECURITY_NOTE_FIELDS: Tuple[str, ...] = (
    "work_notes",
    "comments",
    "additional_comments",
    "close_notes",
)

# Result-type aliases kept loose so tests can inject a fake substrate entry point.
IngestFn = Callable[[str, List[Any]], Any]
RecordEventFn = Callable[[str, Dict[str, Any]], Any]


def _note_text(value: Any) -> Optional[str]:
    """Read a note-field scalar, tolerating a ``display_value``/``value`` object."""
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


def _combined_note(incident: Dict[str, Any]) -> Optional[str]:
    """Concatenate the incident's populated note fields into one text block.

    Returns ``None`` when no note field carries text, so an incident with no
    notes is never handed off.
    """
    parts = [
        note
        for field_name in SECURITY_NOTE_FIELDS
        if (note := _note_text(incident.get(field_name)))
    ]
    return "\n\n".join(parts) if parts else None


def _incident_identity(incident: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return the incident's stable ``(sys_id, number)`` for evidence + keying."""
    sys_id = _note_text(incident.get("sys_id"))
    number = _note_text(incident.get("number")) or _note_text(incident.get("id"))
    return sys_id, number


def _source_timestamp(incident: Dict[str, Any]) -> Optional[str]:
    return (
        _note_text(incident.get("sys_updated_on"))
        or _note_text(incident.get("resolved_at"))
        or _note_text(incident.get("closed_at"))
        or _note_text(incident.get("opened_at"))
        or _note_text(incident.get("sys_created_on"))
        or _note_text(incident.get("source_timestamp"))
    )


def _evidence_pointer(
    incident: Dict[str, Any], sys_id: Optional[str], number: Optional[str]
) -> Dict[str, Any]:
    """Reuse an evidence pointer already on the record, else build the observed one.

    The pointer names the source record only — never note content — so it stays a
    safe, access-controlled handle back to ServiceNow.
    """
    existing = incident.get("evidence")
    if (
        isinstance(existing, dict)
        and existing.get("source_system")
        and existing.get("source_artifact")
    ):
        return dict(existing)
    artifact = sys_id or number or ""
    return EvidencePointer.observed(
        source_system=SECURITY_NOTE_SOURCE_SYSTEM,
        source_artifact=artifact,
        source_timestamp=_source_timestamp(incident),
        source_artifact_type="record_id" if sys_id else None,
    ).to_dict()


@dataclass
class SecurityNoteHandoffResult:
    """Outcome of one :func:`ingest_security_notes` run.

    ``notes_seen`` counts incidents that carried at least one note; ``redacted``
    counts how many of those had at least one secret/IOC removed before hand-off.
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


def build_security_note_artifact(
    incident: Dict[str, Any],
) -> Optional[Tuple[Any, RedactionOutcome, Dict[str, Any]]]:
    """Build a substrate artifact for one SIR record's notes, REDACTED.

    Returns ``(ContentArtifact, RedactionOutcome, provenance)`` or ``None`` when
    the incident carries no note text. The security-extended redaction happens
    HERE, before the artifact exists — so no unredacted note text is ever placed
    on a substrate-bound object.
    """
    from app.retrieval.ingest import ContentArtifact

    note = _combined_note(incident)
    if not note:
        return None

    # Redact BEFORE building the artifact — sanitized text only from here on.
    outcome = scan_and_redact_security(note)

    sys_id, number = _incident_identity(incident)
    source_artifact = sys_id or number
    if not source_artifact:
        return None
    evidence_pointer = _evidence_pointer(incident, sys_id, number)

    provenance: Dict[str, Any] = {
        "origin": "observed",
        "source_system": SECURITY_NOTE_SOURCE_SYSTEM,
        "source_type": SECURITY_NOTE_SOURCE_TYPE,
        "incident_sys_id": sys_id,
        "incident_number": number,
        "category": _note_text(incident.get("category")),
        "source_url": _note_text(incident.get("source_url"))
        or evidence_pointer.get("source_url"),
        # The evidence pointer travels with every chunk so an authorized workflow
        # can trace a retrieval hit back to the access-controlled source record.
        "evidence_pointer": evidence_pointer,
    }

    artifact = ContentArtifact(
        source_system=SECURITY_NOTE_SOURCE_SYSTEM,
        source_artifact=str(source_artifact),
        content=outcome.text,  # SANITIZED text — never the raw note
        content_type=SECURITY_NOTE_CONTENT_TYPE,
        source_timestamp=_source_timestamp(incident),
        provenance=provenance,
    )
    return artifact, outcome, provenance


def ingest_security_notes(
    org_id: str,
    incidents: Iterable[Dict[str, Any]],
    *,
    ingest_fn: Optional[IngestFn] = None,
    record_event_fn: Optional[RecordEventFn] = None,
) -> SecurityNoteHandoffResult:
    """Redact every SIR record's notes and hand the sanitized text off.

    For each incident that carries a note:

    1. redact through the shared security scanner (BEFORE any artifact/log/chunk),
    2. record an ``ingestion.secret_redacted`` telemetry event + WARNING when a
       secret/IOC was removed (pattern types + counts only, never the value),
    3. hand the sanitized :class:`ContentArtifact` to the retrieval substrate.

    ``ingest_fn`` defaults to the real ``retrieval.ingest.ingest_content`` and is
    injectable for tests. ``org_id`` scopes every write to that org's partition.
    """
    if org_id is None or not str(org_id).strip():
        raise ValueError("org_id is required")
    if ingest_fn is None:
        from app.retrieval.ingest import ingest_content

        ingest_fn = ingest_content

    result = SecurityNoteHandoffResult(org_id=org_id)
    artifacts: List[Any] = []
    for incident in incidents or []:
        if not isinstance(incident, dict):
            continue
        built = build_security_note_artifact(incident)
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
    """Record one note's redaction for run-health visibility (reused AC5 event).

    Emits the SAME ``ingestion.secret_redacted`` event Git content and B4 use —
    identifiers, pattern types, and counts only, NEVER the secret/IOC value.
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
                "connector_id": SECURITY_NOTE_CONNECTOR_ID,
                "source_system": SECURITY_NOTE_SOURCE_SYSTEM,
                "source_artifact": getattr(artifact, "source_artifact", ""),
                "content_type": getattr(artifact, "content_type", ""),
                "redaction_count": outcome.count,
                "pattern_types": pattern_types,
                "observed_at": utc_now_iso(),
            },
        )
    except Exception:  # pragma: no cover — observability must not break ingestion
        logger.warning(
            "servicenow_security_notes: failed to record secret-redaction event for %s",
            getattr(artifact, "source_artifact", "?"),
        )
    logger.warning(
        "servicenow_security_notes: redacted %d secret/IOC(s) [%s] from security "
        "incident %s before indexing (org=%s)",
        outcome.count,
        ",".join(pattern_types),
        getattr(artifact, "source_artifact", "?"),
        org_id,
    )
