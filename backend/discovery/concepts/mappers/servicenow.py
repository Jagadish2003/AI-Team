"""2.0-B4 T2 — ServiceNow → normalised concepts.

The richest mapping in the set, and the one that decides whether the concept layer is
worth having: an ITSM tool carries work items, queues, state changes and assignment
history natively, so if the concepts cannot express ServiceNow they cannot express
anything.

Mapped from the fields the connector ALREADY reads — ``INCIDENT_CI_FIELDS`` +
``INCIDENT_RESOLUTION_FIELDS`` for incidents, ``CMDB_FIELDS`` for configuration items,
and the ``sys_audit`` assignment-group history (MSP-B4 T4) for movement. Nothing here
widens the connector's read scope: a mapper that needed a new field would be a
connector change, and mapping a field nobody fetches produces a concept that is
always empty.

The ``{value, display_value}`` trap
-----------------------------------
Under ``sysparm_display_value=all`` every field arrives as a two-value envelope and the
halves are NOT interchangeable — display for names/states, RAW for ``sys_class_name``
and for every datetime (raw is canonical ``YYYY-MM-DD HH:MM:SS`` UTC; display is
rendered in the instance's format and the user's timezone). This module imports
``servicenow.py``'s own accessors rather than re-deriving them, deliberately: the trap
is documented in CLAUDE.md precisely because it was got wrong once, and a second
private copy of the rule is how it gets got wrong again. That the names are
underscore-prefixed is a smaller cost than a divergent second implementation.

State mapping is EXPLICIT, never inferred
-----------------------------------------
``_STATE_CATEGORY`` maps ServiceNow's numeric incident states and their default
display labels onto the coarse ``STATUS_CATEGORIES``. An unrecognised state raises
rather than defaulting to ``"other"``: ServiceNow states are per-instance
configurable, so an unknown value means this deployment has a custom state nobody has
classified — and a silent ``"other"`` would put custom states in the same bucket as
genuinely-other ones, quietly changing what every ageing detector counts as open.
The refusal is what sends an implementer to declare the mapping.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from discovery.concepts import model as m
    from discovery.concepts.mappers import maps
    from discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, group_ref, iso_or_none, record_ref,
        require_id, text,
    )
    from discovery.ingest.servicenow import (
        _optional_sn_raw_text as sn_raw,
        _optional_sn_text as sn_display,
    )
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.mappers import maps
    from backend.discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, group_ref, iso_or_none, record_ref,
        require_id, text,
    )
    from backend.discovery.ingest.servicenow import (
        _optional_sn_raw_text as sn_raw,
        _optional_sn_text as sn_display,
    )


SOURCE = "servicenow"

#: ServiceNow incident ``state`` → normalised category. Both the numeric raw values
#: and the out-of-box display labels are listed, because which one a caller holds
#: depends on which accessor the connector used for that field.
#:
#: Note 7 ("Closed") and 6 ("Resolved") are kept DISTINCT, and 8 ("Cancelled") is
#: never folded into either. The contract's rule — "'cancelled' is NOT 'resolved'" —
#: exists because counting abandoned work as completed overstates throughput for every
#: detector downstream, and ITSM tools are exactly where that mistake is available.
_STATE_CATEGORY: Dict[str, str] = {
    "1": "open",           "new": "open",
    "2": "in_progress",    "in progress": "in_progress", "active": "in_progress",
    "3": "waiting",        "on hold": "waiting", "pending": "waiting",
    "4": "waiting",        "awaiting problem": "waiting",
    "5": "waiting",        "awaiting user info": "waiting",
    "6": "resolved",       "resolved": "resolved",
    "7": "closed",         "closed": "closed", "closed complete": "closed",
    "8": "cancelled",      "cancelled": "cancelled", "canceled": "cancelled",
}

#: ServiceNow priority (1-5) and its default labels → normalised urgency.
_PRIORITY: Dict[str, str] = {
    "1": "critical", "1 - critical": "critical", "critical": "critical",
    "2": "high",     "2 - high": "high",         "high": "high",
    "3": "medium",   "3 - moderate": "medium",   "moderate": "medium", "medium": "medium",
    "4": "low",      "4 - low": "low",           "low": "low",
    "5": "low",      "5 - planning": "low",      "planning": "low",
}

#: Which normalised work-item type each read table produces. Explicit per table
#: rather than guessed from a record's shape.
_TABLE_WORK_ITEM_TYPE: Dict[str, str] = {
    "incident": "incident",
    "change_request": "change",
    "problem": "problem",
    "sc_task": "task",
    "sc_req_item": "request",
}


def _category(raw_state: Optional[str], display_state: Optional[str]) -> str:
    """Normalise an incident state, or raise naming the unmapped value."""
    for candidate in (raw_state, display_state):
        if candidate is None:
            continue
        key = str(candidate).strip().lower()
        if key in _STATE_CATEGORY:
            return _STATE_CATEGORY[key]
    raise MappingInputError(
        f"ServiceNow state {display_state or raw_state!r} has no mapping onto "
        f"{sorted(m.STATUS_CATEGORIES)}. ServiceNow states are per-instance "
        f"configurable — add the mapping to _STATE_CATEGORY rather than letting it "
        f"default, because a custom state silently read as 'other' changes what every "
        f"ageing detector counts as open."
    )


def _priority(raw: Optional[str], display: Optional[str]) -> str:
    """Normalise priority. Absent priority is ``'none'`` — an honest 'not set'."""
    for candidate in (raw, display):
        if candidate is None:
            continue
        key = str(candidate).strip().lower()
        if key in _PRIORITY:
            return _PRIORITY[key]
    if raw is None and display is None:
        return "none"
    raise MappingInputError(
        f"ServiceNow priority {display or raw!r} has no mapping onto "
        f"{sorted(m.PRIORITY_LEVELS)}"
    )


@maps(SOURCE, m.CONCEPT_WORK_ITEM)
def map_incident_work_item(
    org_id: str, record: Dict[str, Any], *, table: str = "incident"
) -> m.WorkItem:
    """A ServiceNow task-table record → :class:`WorkItem`.

    ``assigned_group`` carries the assignment GROUP only. ``assigned_to`` (an
    individual) is read by the connector for other purposes and is deliberately NOT
    mapped anywhere on the concept — see the ``assigned_to`` field gap in
    ``conformance.py``. Dropping it is the point: the concept set offers no field that
    could carry it, so no detector built on concepts can surface a person.
    """
    sys_id = require_id(sn_raw(record.get("sys_id")), "ServiceNow sys_id")
    number = sn_display(record.get("number"))
    # Datetimes and sys_class-style identifiers take the RAW half; names and states
    # take the display half. See the module docstring.
    updated = iso_or_none(sn_raw(record.get("sys_updated_on")))
    opened = iso_or_none(sn_raw(record.get("opened_at")) or sn_raw(record.get("sys_created_on")))

    work_item_type = _TABLE_WORK_ITEM_TYPE.get(table)
    if work_item_type is None:
        raise MappingInputError(
            f"table {table!r} has no declared work-item type; the mapped tables are "
            f"{sorted(_TABLE_WORK_ITEM_TYPE)}"
        )

    return m.WorkItem(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=sys_id,
        observed_at=updated or opened or "",
        provenance=concept_provenance(SOURCE, sys_id, updated or opened, updated or opened or ""),
        native_type=table,
        work_item_type=work_item_type,
        status_category=_category(sn_raw(record.get("state")), sn_display(record.get("state"))),
        native_status=sn_display(record.get("state")) or "",
        priority=_priority(sn_raw(record.get("priority")), sn_display(record.get("priority"))),
        reference=number or "",
        title=sn_display(record.get("short_description")),
        opened_at=opened,
        resolved_at=iso_or_none(sn_raw(record.get("resolved_at"))),
        closed_at=iso_or_none(sn_raw(record.get("closed_at"))),
        assigned_group=group_ref(
            SOURCE,
            _ref_sys_id(record.get("assignment_group")),
            sn_display(record.get("assignment_group")),
        ),
        attributes=_incident_attributes(record),
    )


def _incident_attributes(record: Dict[str, Any]) -> Dict[str, Any]:
    """Source detail with no normalised home, kept off the contract's shape.

    Deliberately excludes ``assigned_to`` and ``caller_id``: ``attributes`` is not a
    side door for the individual-level fields the concept set refuses to model.
    """
    extra: Dict[str, Any] = {}
    for key in ("category", "subcategory", "close_code"):
        value = sn_display(record.get(key))
        if value:
            extra[key] = value
    return extra


def _ref_sys_id(value: Any) -> Optional[str]:
    """The sys_id of a ServiceNow reference field.

    A reference arrives as ``{"value": "<sys_id>", "display_value": "<name>"}``, so
    the RAW half is the identity and the display half is the label. Falling back to
    the label would key a group on its name, which merges two groups that share one.
    """
    if isinstance(value, dict):
        return text(value.get("value"))
    return text(value)


@maps(SOURCE, m.CONCEPT_ACTOR_GROUP)
def map_assignment_group(org_id: str, record: Dict[str, Any]) -> m.ActorGroup:
    """A ``sys_user_group`` record → :class:`ActorGroup`.

    ``group_type`` is ``queue``: a ServiceNow assignment group IS a work queue — work
    is routed to it and drawn from it — which is the distinction the vocabulary draws
    against a Slack channel's ``team`` (a container, with no queue semantics).

    ``member_count`` is populated only when the caller supplies an aggregate count.
    There is no member list on the concept at any version, so a roster cannot leak.
    """
    sys_id = require_id(_ref_sys_id(record.get("sys_id")), "ServiceNow group sys_id")
    name = sn_display(record.get("name"))
    if not name:
        raise MappingInputError("ServiceNow group has no name — cannot map ActorGroup")
    updated = iso_or_none(sn_raw(record.get("sys_updated_on")))
    count = record.get("member_count")
    return m.ActorGroup(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=sys_id,
        observed_at=updated or "",
        provenance=concept_provenance(SOURCE, sys_id, updated, updated or ""),
        native_type="sys_user_group",
        group_type="queue",
        name=name,
        member_count=int(count) if isinstance(count, (int, float)) else None,
    )


@maps(SOURCE, m.CONCEPT_ASSIGNMENT)
def map_assignment_history(
    org_id: str, record: Dict[str, Any], *, hop_index: Optional[int] = None
) -> m.Assignment:
    """One ``sys_audit`` assignment-group change → :class:`Assignment`.

    The audit row the connector already reads for MSP-B4 T4 movement
    (``ASSIGNMENT_HISTORY_FIELDS``: documentkey / oldvalue / newvalue / sys_created_on).

    ``assignment_type`` is derived from ``oldvalue`` ALONE and nothing else: no prior
    group means this is the ``initial`` assignment; any prior group means the work was
    passed on, which is ``reassignment``. ``escalation`` and ``delegation`` are
    deliberately NOT guessed — ServiceNow's audit row records that the group changed,
    not why, and inferring escalation from (say) a group name containing "L2" is
    exactly the naming heuristic the platform refuses. A connector that later reads a
    real escalation field can map it then.

    ``hop_index`` is supplied by the caller because it is a property of the ORDERED
    history, not of one row; a mapper handed a single row cannot know its position and
    must not invent one.
    """
    audit_id = require_id(_ref_sys_id(record.get("sys_id")), "ServiceNow audit sys_id")
    changed_at = iso_or_none(sn_raw(record.get("sys_created_on")))
    old_group = text(record.get("oldvalue"))
    new_group = text(record.get("newvalue"))
    return m.Assignment(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=audit_id,
        observed_at=changed_at or "",
        provenance=concept_provenance(SOURCE, audit_id, changed_at, changed_at or ""),
        native_type="sys_audit:assignment_group",
        assignment_type="initial" if not old_group else "reassignment",
        work_item=record_ref("process", SOURCE, text(record.get("documentkey"))),
        # The audit row stores the group's NAME in oldvalue/newvalue, not its sys_id,
        # so this reference is name-keyed by the source's own construction. Recorded
        # as a field gap rather than papered over with a fabricated id.
        assigned_to=group_ref(SOURCE, new_group, new_group),
        assigned_at=changed_at,
        hop_index=hop_index,
        attributes={"from_group": old_group} if old_group else {},
    )


@maps(SOURCE, m.CONCEPT_STATE_TRANSITION)
def map_state_transition(
    org_id: str, record: Dict[str, Any], *, work_item_sys_id: Optional[str] = None
) -> m.StateTransition:
    """One state change → :class:`StateTransition`.

    ``reopen`` is detected structurally — a transition whose FROM category is
    terminal (``resolved``/``closed``) and whose TO category is not — rather than by
    reading a native "reopened" flag that most instances do not set. The contract
    requires this distinction because rework is the signal a detector hunts, and a
    reopen recorded as a plain ``status_change`` erases it.
    """
    audit_id = require_id(_ref_sys_id(record.get("sys_id")), "ServiceNow audit sys_id")
    changed_at = iso_or_none(sn_raw(record.get("sys_created_on")))
    from_native = text(record.get("oldvalue"))
    to_native = text(record.get("newvalue"))
    from_category = _category(from_native, from_native) if from_native else None
    to_category = _category(to_native, to_native) if to_native else None

    transition_type = "status_change"
    if from_category in ("resolved", "closed") and to_category not in ("resolved", "closed", None):
        transition_type = "reopen"

    return m.StateTransition(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=audit_id,
        observed_at=changed_at or "",
        provenance=concept_provenance(SOURCE, audit_id, changed_at, changed_at or ""),
        native_type="sys_audit:state",
        transition_type=transition_type,
        work_item=record_ref(
            "process", SOURCE, work_item_sys_id or text(record.get("documentkey"))
        ),
        from_status_category=from_category,
        to_status_category=to_category,
        from_native_status=from_native,
        to_native_status=to_native,
        transitioned_at=changed_at,
    )


@maps(SOURCE, m.CONCEPT_ENTITY_REFERENCE)
def map_cmdb_ci_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A CMDB configuration item → :class:`EntityReference`.

    ``entity_type='system'`` matches what MSP-B3's CMDB ingestion writes to the graph,
    so a concept reference and a graph entity agree about what kind of thing a CI is.

    ``entity_id`` stays ``None``. A CI's graph id is the resolver's output, and a
    mapper asserting one would claim a resolution nobody performed — 2.0-B2's
    central discipline.
    """
    sys_id = require_id(_ref_sys_id(record.get("sys_id")), "CMDB sys_id")
    return m.EntityReference(
        entity_type="system",
        source_system=SOURCE,
        source_record_id=sys_id,
        display_name=sn_display(record.get("name")),
    )


def map_incident_stream(
    org_id: str, records: List[Dict[str, Any]], *, table: str = "incident"
) -> List[m.WorkItem]:
    """Map a batch of task records, skipping none silently.

    A record that cannot be mapped raises: the caller decides whether one malformed
    row should fail a run, and a helper that swallowed them would make a partial
    ingest look complete — the reporting failure MSP-B7 and 2.0-D3 T4 both had to fix.
    """
    return [map_incident_work_item(org_id, r, table=table) for r in records]


__all__ = [
    "SOURCE",
    "map_incident_work_item",
    "map_assignment_group",
    "map_assignment_history",
    "map_state_transition",
    "map_cmdb_ci_reference",
    "map_incident_stream",
]
