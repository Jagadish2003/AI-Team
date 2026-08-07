"""2.0-B4 T2 — Jira → normalised concepts.

Jira is the interesting case, because two of its concepts map cleanly and two cannot
map at all — which is what makes it the proof that the conformance record is doing
real work rather than rubber-stamping.

What maps
---------
``work_item`` and ``artifact``. Jira's own ``statusCategory`` is a normalised
lifecycle field (``new`` / ``indeterminate`` / ``done``), so the status mapping reads a
real field rather than pattern-matching per-project status NAMES — the per-instance
configurability that makes ServiceNow's state mapping fragile does not bite here.

The one trap, handled explicitly
--------------------------------
``statusCategory='done'`` covers both completed and ABANDONED work: "Won't Do",
"Duplicate" and "Cannot Reproduce" are all ``done``. Mapping ``done`` straight to
``closed`` would therefore count abandoned issues as completed work, which is exactly
the failure the work-item contract names ("'cancelled' is NOT 'resolved'"). So the
mapping reads ``fields.resolution.name`` as well and routes the abandoning resolutions
to ``cancelled``. This is a field the connector already requests.

What does NOT map, and why that is recorded rather than approximated
-------------------------------------------------------------------
``actor_group`` and ``assignment`` are declared GAPS for Jira (T1), and this module is
where that decision becomes visible in code: **there is deliberately no mapper for
either**. Jira assigns to individuals; its project roles and components are the
nearest group-shaped things and neither is a work queue. A mapper could trivially
produce an ``ActorGroup`` named after an assignee, and it would be wrong in the way
that matters — every finding built on it would name a person while appearing to name a
team. ``approval`` is likewise absent: approval in Jira is a per-project workflow
convention, not a record, so reading one reliably needs per-org configuration this
connector does not have.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from discovery.concepts import model as m
    from discovery.concepts.mappers import maps
    from discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, iso_or_none, require_id, text,
    )
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.mappers import maps
    from backend.discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, iso_or_none, require_id, text,
    )


SOURCE = "jira"

#: Jira's own ``statusCategory.key`` → normalised category. Three values, fixed by
#: Jira rather than by the customer, which is why this mapping is stable where a
#: status-NAME mapping would not be.
_STATUS_CATEGORY: Dict[str, str] = {
    "new": "open",
    "indeterminate": "in_progress",
    "done": "closed",
}

#: Resolutions that mean the work was ABANDONED, not completed. Jira files all of
#: these under ``statusCategory='done'``, so without this set a "Won't Do" issue would
#: be counted as delivered work by every throughput and ageing detector.
_ABANDONING_RESOLUTIONS = frozenset({
    "won't do", "wont do", "won't fix", "wont fix", "duplicate",
    "cannot reproduce", "can't reproduce", "incomplete", "declined", "abandoned",
})

#: Default Jira issue types → normalised type. An UNRECOGNISED type falls back to
#: ``'other'``, and that fallback is safe in a way a status fallback would not be:
#: nothing branches open-vs-closed on ``work_item_type``, whereas a mis-defaulted
#: ``status_category`` would change what counts as outstanding work. Jira issue types
#: are freely invented per project, so refusing an unknown one would fail on ordinary
#: customer configuration rather than on a real fault.
_ISSUE_TYPE: Dict[str, str] = {
    "bug": "issue", "story": "issue", "epic": "issue", "improvement": "issue",
    "new feature": "issue", "spike": "issue",
    "task": "task", "sub-task": "task", "subtask": "task",
    "incident": "incident", "problem": "problem", "change": "change",
    "service request": "request", "support request": "request",
}

#: Jira priority names → normalised urgency. Jira ships these five; a customer scheme
#: that renames them falls through to ``'none'`` (not-set) rather than being guessed.
_PRIORITY: Dict[str, str] = {
    "highest": "critical", "blocker": "critical", "critical": "critical", "p1": "critical",
    "high": "high", "major": "high", "p2": "high",
    "medium": "medium", "normal": "medium", "p3": "medium",
    "low": "low", "minor": "low", "p4": "low",
    "lowest": "low", "trivial": "low", "p5": "low",
}


def _nested(record: Dict[str, Any], *path: str) -> Any:
    """Walk a Jira nested field, tolerating a ``null`` relationship at any level.

    Jira returns ``"assignee": null`` / ``"resolution": null`` routinely, so every
    level must tolerate ``None`` — the same defence ``discovery/ingest/fsc.py``
    documents for Salesforce parent traversal.
    """
    current: Any = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _status_category(fields: Dict[str, Any]) -> str:
    """Normalise a Jira status, routing abandoning resolutions to ``cancelled``."""
    key = text(_nested(fields, "status", "statusCategory", "key"))
    if not key:
        raise MappingInputError(
            "Jira issue has no status.statusCategory.key — the connector requests the "
            "status field, so its absence means a malformed record rather than a "
            "mapping gap"
        )
    category = _STATUS_CATEGORY.get(key.strip().lower())
    if category is None:
        raise MappingInputError(
            f"Jira statusCategory {key!r} is not one of {sorted(_STATUS_CATEGORY)}"
        )
    if category == "closed":
        resolution = (text(_nested(fields, "resolution", "name")) or "").strip().lower()
        if resolution in _ABANDONING_RESOLUTIONS:
            return "cancelled"
    return category


@maps(SOURCE, m.CONCEPT_WORK_ITEM)
def map_issue_work_item(org_id: str, record: Dict[str, Any]) -> m.WorkItem:
    """A Jira issue → :class:`WorkItem`.

    ``assigned_group`` is always ``None``: Jira assigns to a person, and the contract
    forbids synthesising a group from an assignee. The connector's ``JIRA_TEAM_FIELD``
    custom field can carry a team, but it is per-deployment configuration rather than
    a field Jira guarantees, so this mapper does not read it — a mapping that works
    only on some customers' instances would make conformance mean different things for
    different deployments.
    """
    fields = record.get("fields") or {}
    issue_id = require_id(record.get("id") or record.get("key"), "Jira issue id")
    key = text(record.get("key"))
    updated = iso_or_none(fields.get("updated"))
    created = iso_or_none(fields.get("created"))

    native_type = text(_nested(fields, "issuetype", "name")) or ""
    priority_name = (text(_nested(fields, "priority", "name")) or "").strip().lower()

    return m.WorkItem(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=issue_id,
        observed_at=updated or created or "",
        provenance=concept_provenance(SOURCE, issue_id, updated or created, updated or created or ""),
        native_type=native_type,
        work_item_type=_ISSUE_TYPE.get(native_type.strip().lower(), "other"),
        status_category=_status_category(fields),
        native_status=text(_nested(fields, "status", "name")) or "",
        priority=_PRIORITY.get(priority_name, "none"),
        reference=key or "",
        title=text(fields.get("summary")),
        opened_at=created,
        resolved_at=iso_or_none(fields.get("resolutiondate")),
        # Jira has no separate close timestamp — resolution IS the close event. Left
        # None rather than duplicating resolved_at, so a detector measuring the gap
        # between resolve and close does not read a manufactured zero.
        closed_at=None,
        assigned_group=None,
        attributes=_issue_attributes(fields),
    )


def _issue_attributes(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Source detail with no normalised home.

    Excludes ``assignee`` and ``reporter``: ``attributes`` is not a side door for the
    individual-level fields the concept set refuses to model.
    """
    extra: Dict[str, Any] = {}
    project = text(_nested(fields, "project", "key"))
    if project:
        extra["project_key"] = project
    labels = [text(l) for l in (fields.get("labels") or [])]
    labels = [l for l in labels if l]
    if labels:
        extra["labels"] = labels
    resolution = text(_nested(fields, "resolution", "name"))
    if resolution:
        extra["resolution"] = resolution
    return extra


@maps(SOURCE, m.CONCEPT_ARTIFACT)
def map_issue_attachment(
    org_id: str, record: Dict[str, Any], *, issue_key: Optional[str] = None
) -> m.Artifact:
    """A Jira issue attachment → :class:`Artifact`.

    ``content_type='prose'`` matches what the retrieval substrate would chunk an
    attachment under, per the artifact contract's "classify once" rule. The bytes are
    NOT carried here: an artifact is a reference to content, and content reaches
    retrieval through the substrate's single ingest path, where secret redaction runs.
    """
    attachment_id = require_id(record.get("id"), "Jira attachment id")
    created = iso_or_none(record.get("created"))
    return m.Artifact(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=attachment_id,
        observed_at=created or "",
        provenance=concept_provenance(SOURCE, attachment_id, created, created or ""),
        native_type="attachment",
        artifact_type="attachment",
        content_type="prose",
        title=text(record.get("filename")),
        location=text(record.get("content")),
        updated_at=created,
        attributes={"issue_key": issue_key} if issue_key else {},
    )


@maps(SOURCE, m.CONCEPT_ENTITY_REFERENCE)
def map_issue_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A Jira issue → :class:`EntityReference`.

    Keyed on the numeric issue ``id``, not the ``key``: a Jira key changes when an
    issue moves project, and a reference that stops resolving after a move is not a
    reference. The key travels as ``display_name``.
    """
    issue_id = require_id(record.get("id"), "Jira issue id")
    return m.EntityReference(
        entity_type="process",
        source_system=SOURCE,
        source_record_id=issue_id,
        display_name=text(record.get("key")),
    )


def map_issue_stream(org_id: str, records: List[Dict[str, Any]]) -> List[m.WorkItem]:
    """Map a batch of issues. A malformed record raises rather than being skipped."""
    return [map_issue_work_item(org_id, r) for r in records]


__all__ = [
    "SOURCE",
    "map_issue_work_item",
    "map_issue_attachment",
    "map_issue_reference",
    "map_issue_stream",
]
