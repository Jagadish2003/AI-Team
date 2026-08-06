"""2.0-B4 T2 — helpers every connector mapper shares.

Written once here for the reason ``discovery/ingest/operational_signals.py`` exists:
the Java and .NET ingestors each held a private copy of the same extraction until it
drifted. The provenance spine, the timestamp normalisation and the group-reference
rule are identical for every connector, so they live in one place and every mapper
imports them.

Nothing here reads the environment, opens a connection, or imports ``app`` beyond the
pure ``EvidencePointer`` dataclass — the concept layer stays offline-safe and
deterministic, which is what lets a golden fixture pin a mapper's whole output.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

try:
    from discovery.concepts import model as m
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m


class MappingInputError(ValueError):
    """The record handed to a mapper cannot be mapped, with the reason named.

    Raised rather than returning ``None`` when the record is malformed in a way the
    caller should know about (no id, for instance). A mapper returns ``None`` only
    for a record that is legitimately out of scope for that concept — the two cases
    are different and collapsing them hides real ingestion faults.
    """


def concept_provenance(
    source_system: str,
    source_artifact: str,
    source_timestamp: Optional[str],
    observed_at: str,
) -> Dict[str, Any]:
    """The OBSERVED EvidencePointer spine every concept observation carries.

    ``origin`` is always ``observed``: a mapper reports what a source record said, so
    there is nothing inferred to declare and no ``extraction_job_id`` to supply. A
    concept produced by reasoning over other concepts would be ``inferred`` — and it
    would not come from here.

    ``source_artifact_type='record_id'`` because every mapper keys on a stable source
    id (sys_id, issue key, Salesforce Id), never on a name. A name-keyed pointer is
    ``canonical_name`` and cannot be looked up in the source system later.
    """
    return EvidencePointer.observed(
        source_system=source_system,
        source_artifact=source_artifact,
        source_timestamp=source_timestamp or observed_at,
        source_artifact_type="record_id",
    ).to_dict()


def group_ref(
    source_system: str,
    source_record_id: Optional[str],
    display_name: Optional[str] = None,
) -> Optional[m.EntityReference]:
    """An ``EntityReference`` to a GROUP, or ``None`` when the source gave none.

    The single funnel for every group-shaped field on every concept
    (``assigned_group``, ``approver_group``, ``actor_group``, ``assigned_to``), so the
    platform's standing "groups, queues and processes — never individuals" rule has
    one place to hold rather than a dozen.

    Returns ``None`` on a missing id rather than falling back to the display name:
    a name-keyed reference is not traceable to a source record, and a group whose
    identity is a label silently merges two groups that happen to share one.
    """
    record_id = (str(source_record_id).strip() if source_record_id else "")
    if not record_id:
        return None
    return m.EntityReference(
        entity_type="team",
        source_system=source_system,
        source_record_id=record_id,
        display_name=(str(display_name).strip() or None) if display_name else None,
    )


def record_ref(
    entity_type: str,
    source_system: str,
    source_record_id: Optional[str],
    display_name: Optional[str] = None,
) -> Optional[m.EntityReference]:
    """An ``EntityReference`` to a non-group record (a work item, a system, a process).

    ``entity_id`` is deliberately never set here. Resolution is the graph's decision
    (``app/entity_resolution.py`` / 2.0-B2's cross-source resolution), and a mapper
    that filled it in would be claiming a resolution nobody made — the exact
    dishonesty B2's whole design refuses.
    """
    record_id = (str(source_record_id).strip() if source_record_id else "")
    if not record_id:
        return None
    return m.EntityReference(
        entity_type=entity_type,
        source_system=source_system,
        source_record_id=record_id,
        display_name=(str(display_name).strip() or None) if display_name else None,
    )


def text(value: Any) -> Optional[str]:
    """Trimmed text, or ``None`` for anything empty. Never the string ``'None'``."""
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def require_id(value: Any, what: str) -> str:
    """A stable source id, or a named error.

    Every concept observation needs ``signal_id``; a record with no id cannot be
    traced back, so mapping it would produce an untraceable observation. That is a
    fault worth surfacing, not a record to skip quietly.
    """
    result = text(value)
    if not result:
        raise MappingInputError(f"{what} is required to map this record — no stable id")
    return result


def iso_or_none(value: Any) -> Optional[str]:
    """Pass a timestamp through as text without reformatting it.

    Mappers deliberately do NOT parse and re-render timestamps. Each connector's own
    ingestor already resolves the source's canonical form (ServiceNow's raw
    ``YYYY-MM-DD HH:MM:SS`` UTC rather than the instance-formatted display value, for
    example), and a second normalisation here would be a second place for that
    trap to be got wrong.
    """
    return text(value)


__all__ = [
    "MappingInputError",
    "concept_provenance",
    "group_ref",
    "record_ref",
    "text",
    "require_id",
    "iso_or_none",
]
