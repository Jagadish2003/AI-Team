"""2.0-B4 T2 — Salesforce → normalised concepts.

Mapped from the objects the connector already queries: ``Case``, ``CaseHistory``,
``ProcessInstance`` / ``ProcessInstanceWorkitem``.

The one genuinely nice property of this source
----------------------------------------------
Salesforce's ``OwnerId`` can be EITHER a user or a queue, and its key prefix says
which — ``005`` is a User, ``00G`` is a Group (which is what a Queue is). So the
group-vs-individual question that forces a gap on Jira is answerable here
DETERMINISTICALLY, from the id itself, with no name matching and no inference:

* owner prefix ``00G`` → a real queue → ``assigned_group`` is populated;
* owner prefix ``005`` → an individual → ``assigned_group`` stays ``None``.

That second branch is the important one. A case owned by a person is not a case owned
by a team, and the mapper reports it as unassigned-to-a-group rather than inventing a
group from the user's name. The consequence — that person-owned cases carry no group —
is recorded as a field gap in ``conformance.py`` so a pack author reads it before
building a queue-concentration detector on this source.

Approvals
---------
``ProcessInstance`` is a genuine first-class approval record, which is why Salesforce
supports the ``approval`` concept where Jira cannot. ``pending`` is mapped explicitly
(from ``Status='Pending'``) because an undecided approval is precisely what an
approval-bottleneck detector measures — the contract requires it to be emitted rather
than omitted.
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
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.mappers import maps
    from backend.discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, group_ref, iso_or_none, record_ref,
        require_id, text,
    )


SOURCE = "salesforce"

#: Salesforce key prefixes. Fixed by the platform (not per-org), which is what makes
#: the owner decision deterministic rather than a heuristic.
QUEUE_ID_PREFIX = "00G"   # Group — a Queue is a Group with Type='Queue'
USER_ID_PREFIX = "005"    # User — an individual

#: Standard ``Case.Status`` picklist → normalised category. Salesforce case statuses
#: ARE per-org configurable, so an unmapped value raises for the same reason a
#: ServiceNow state does: a custom status silently read as ``'other'`` changes what an
#: ageing detector counts as open. The pack's own config
#: (``financial_services_cloud_pack_config.json`` carries a ``closed_statuses`` list)
#: is the precedent for making this per-org configurable, and the honest position
#: until then is to refuse rather than guess.
_CASE_STATUS: Dict[str, str] = {
    "new": "open",
    "open": "open",
    "working": "in_progress",
    "in progress": "in_progress",
    "escalated": "in_progress",
    "on hold": "waiting",
    "waiting on customer": "waiting",
    "pending": "waiting",
    "closed": "closed",
    "closed - resolved": "closed",
    "resolved": "resolved",
    "closed - no response": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "duplicate": "cancelled",
}

#: ``Case.Priority`` → normalised urgency.
_CASE_PRIORITY: Dict[str, str] = {
    "critical": "critical", "high": "high", "medium": "medium", "low": "low",
}

#: ``ProcessInstance.Status`` → normalised decision. ``Removed`` is Salesforce's term
#: for an approval withdrawn before decision, which is ``withdrawn`` and NOT
#: ``rejected`` — conflating them would report a cancelled request as a refusal.
_APPROVAL_DECISION: Dict[str, str] = {
    "pending": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "removed": "withdrawn",
    "reassigned": "delegated",
}


def _case_status(value: Any) -> str:
    status = (text(value) or "").strip().lower()
    if not status:
        raise MappingInputError("Salesforce Case has no Status — cannot map a category")
    category = _CASE_STATUS.get(status)
    if category is None:
        raise MappingInputError(
            f"Salesforce Case.Status {status!r} has no mapping onto "
            f"{sorted(m.STATUS_CATEGORIES)}. Case statuses are per-org configurable — "
            f"add the mapping rather than defaulting, because a custom status read as "
            f"'other' changes what counts as outstanding work."
        )
    return category


def owner_group_ref(owner_id: Any, owner_name: Any = None) -> Optional[m.EntityReference]:
    """A group reference for a Case owner, or ``None`` when the owner is a person.

    The deterministic branch described in the module docstring. An id that is neither
    a User nor a Group prefix returns ``None`` too: an unrecognised owner type is not
    evidence of a queue.
    """
    owner = text(owner_id)
    if not owner or not owner.startswith(QUEUE_ID_PREFIX):
        return None
    return group_ref(SOURCE, owner, text(owner_name))


@maps(SOURCE, m.CONCEPT_WORK_ITEM)
def map_case_work_item(org_id: str, record: Dict[str, Any]) -> m.WorkItem:
    """A Salesforce ``Case`` → :class:`WorkItem`."""
    case_id = require_id(record.get("Id"), "Salesforce Case Id")
    modified = iso_or_none(record.get("LastModifiedDate"))
    created = iso_or_none(record.get("CreatedDate"))
    priority = (text(record.get("Priority")) or "").strip().lower()

    return m.WorkItem(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=case_id,
        observed_at=modified or created or "",
        provenance=concept_provenance(SOURCE, case_id, modified or created, modified or created or ""),
        native_type="Case",
        work_item_type="case",
        status_category=_case_status(record.get("Status")),
        native_status=text(record.get("Status")) or "",
        priority=_CASE_PRIORITY.get(priority, "none"),
        reference=text(record.get("CaseNumber")) or "",
        title=text(record.get("Subject")),
        opened_at=created,
        # Salesforce records ClosedDate only; a resolved-but-open state is expressed
        # through Status, so resolved_at stays None rather than mirroring ClosedDate.
        resolved_at=None,
        closed_at=iso_or_none(record.get("ClosedDate")),
        assigned_group=owner_group_ref(record.get("OwnerId"), record.get("OwnerName")),
        attributes=_case_attributes(record),
    )


def _case_attributes(record: Dict[str, Any]) -> Dict[str, Any]:
    """Source detail with no normalised home — never the owner's name."""
    extra: Dict[str, Any] = {}
    for source_key, out_key in (("Type", "case_type"), ("Origin", "origin"), ("Reason", "reason")):
        value = text(record.get(source_key))
        if value:
            extra[out_key] = value
    # Whether the owner is a queue or a person is a FACT about the record and is what
    # explains an absent assigned_group, so it is recorded. The person is not named.
    owner = text(record.get("OwnerId")) or ""
    if owner.startswith(USER_ID_PREFIX):
        extra["owner_is_individual"] = True
    return extra


@maps(SOURCE, m.CONCEPT_STATE_TRANSITION)
def map_case_history_transition(
    org_id: str, record: Dict[str, Any]
) -> Optional[m.StateTransition]:
    """A ``CaseHistory`` row for a Status change → :class:`StateTransition`.

    Returns ``None`` for a history row about any OTHER field. That is a legitimate
    out-of-scope record rather than a fault, which is why it returns ``None`` instead
    of raising — the distinction ``_common.MappingInputError`` documents.
    """
    if (text(record.get("Field")) or "").strip().lower() != "status":
        return None
    history_id = require_id(record.get("Id"), "Salesforce CaseHistory Id")
    changed_at = iso_or_none(record.get("CreatedDate"))
    from_native = text(record.get("OldValue"))
    to_native = text(record.get("NewValue"))
    from_category = _case_status(from_native) if from_native else None
    to_category = _case_status(to_native) if to_native else None

    transition_type = "status_change"
    if from_category in ("resolved", "closed") and to_category not in ("resolved", "closed", None):
        transition_type = "reopen"

    return m.StateTransition(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=history_id,
        observed_at=changed_at or "",
        provenance=concept_provenance(SOURCE, history_id, changed_at, changed_at or ""),
        native_type="CaseHistory:Status",
        transition_type=transition_type,
        work_item=record_ref("process", SOURCE, text(record.get("CaseId"))),
        from_status_category=from_category,
        to_status_category=to_category,
        from_native_status=from_native,
        to_native_status=to_native,
        transitioned_at=changed_at,
    )


@maps(SOURCE, m.CONCEPT_ASSIGNMENT)
def map_case_owner_assignment(
    org_id: str, record: Dict[str, Any], *, hop_index: Optional[int] = None
) -> Optional[m.Assignment]:
    """A ``CaseHistory`` row for an Owner change → :class:`Assignment`.

    ``assigned_to`` is populated only when the new owner is a QUEUE. A case handed to
    a person still produces the Assignment — the hop happened, and a
    reassignment-churn detector counts hops — but with no ``assigned_to``, because
    naming the recipient would name an individual. Counting the hop is not the same as
    identifying who took it.
    """
    if (text(record.get("Field")) or "").strip().lower() not in ("owner", "ownerid"):
        return None
    history_id = require_id(record.get("Id"), "Salesforce CaseHistory Id")
    changed_at = iso_or_none(record.get("CreatedDate"))
    old_value = text(record.get("OldValue"))
    new_value = text(record.get("NewValue"))

    return m.Assignment(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=history_id,
        observed_at=changed_at or "",
        provenance=concept_provenance(SOURCE, history_id, changed_at, changed_at or ""),
        native_type="CaseHistory:Owner",
        assignment_type="initial" if not old_value else "reassignment",
        work_item=record_ref("process", SOURCE, text(record.get("CaseId"))),
        assigned_to=owner_group_ref(new_value),
        assigned_at=changed_at,
        hop_index=hop_index,
        attributes=(
            {"to_owner_is_individual": True}
            if (new_value or "").startswith(USER_ID_PREFIX) else {}
        ),
    )


@maps(SOURCE, m.CONCEPT_APPROVAL)
def map_process_instance_approval(org_id: str, record: Dict[str, Any]) -> m.Approval:
    """A ``ProcessInstance`` → :class:`Approval`.

    ``approver_group`` is left ``None``: Salesforce approvals are assigned through
    ``ProcessInstanceWorkitem.ActorId``, which is a User in the overwhelmingly common
    configuration, and a queue-based approver cannot be distinguished from a
    person-based one without reading that child object. Declared as a field gap rather
    than filled with the requester or the process name.

    ``approval_type`` stays ``'other'`` — Salesforce does not classify an approval
    process as managerial / compliance / financial, and inferring it from the process
    NAME is the naming heuristic the platform refuses.
    """
    instance_id = require_id(record.get("Id"), "Salesforce ProcessInstance Id")
    created = iso_or_none(record.get("CreatedDate"))
    completed = iso_or_none(record.get("CompletedDate"))
    status = (text(record.get("Status")) or "").strip().lower()
    decision = _APPROVAL_DECISION.get(status)
    if decision is None:
        raise MappingInputError(
            f"Salesforce ProcessInstance.Status {status!r} has no mapping onto "
            f"{sorted(m.APPROVAL_DECISIONS)}"
        )

    return m.Approval(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=instance_id,
        observed_at=completed or created or "",
        provenance=concept_provenance(SOURCE, instance_id, completed or created, completed or created or ""),
        native_type="ProcessInstance",
        decision=decision,
        approval_type="other",
        work_item=record_ref("process", SOURCE, text(record.get("TargetObjectId"))),
        approver_group=None,
        requested_at=created,
        # An undecided approval has no decision time. Left None rather than defaulted
        # to now, which would make every pending approval look instantaneous.
        decided_at=completed if decision != "pending" else None,
        step_index=_step_index(record.get("StepStatus"), record.get("step_index")),
    )


def _step_index(step_status: Any, explicit: Any) -> Optional[int]:
    """The position in a multi-step chain, when the caller knows it.

    Salesforce exposes step ORDER through ``ProcessInstanceStep`` children rather than
    on the instance, so this is ``None`` unless the caller supplies it — the same rule
    as ServiceNow's ``hop_index``: a property of the ordered set cannot be derived from
    one member.
    """
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    return None


@maps(SOURCE, m.CONCEPT_ACTOR_GROUP)
def map_queue_actor_group(org_id: str, record: Dict[str, Any]) -> m.ActorGroup:
    """A Salesforce ``Group`` with ``Type='Queue'`` → :class:`ActorGroup`.

    Refuses any other Group type. Salesforce ``Group`` also covers Roles,
    RoleAndSubordinates and territory groupings, none of which is a work queue, and
    mapping them all to ``queue`` would put an org-chart node in the same concept as a
    real work queue.
    """
    group_id = require_id(record.get("Id"), "Salesforce Group Id")
    group_type = (text(record.get("Type")) or "").strip().lower()
    if group_type != "queue":
        raise MappingInputError(
            f"Salesforce Group.Type {group_type!r} is not a Queue; roles and territory "
            f"groups are not work queues and must not be mapped as one"
        )
    name = text(record.get("Name"))
    if not name:
        raise MappingInputError("Salesforce Queue has no Name — cannot map ActorGroup")
    modified = iso_or_none(record.get("LastModifiedDate"))
    return m.ActorGroup(
        org_id=org_id,
        source_system=SOURCE,
        signal_id=group_id,
        observed_at=modified or "",
        provenance=concept_provenance(SOURCE, group_id, modified, modified or ""),
        native_type="Group:Queue",
        group_type="queue",
        name=name,
        member_count=None,
    )


@maps(SOURCE, m.CONCEPT_ENTITY_REFERENCE)
def map_record_reference(
    record: Dict[str, Any], *, entity_type: str = "process"
) -> m.EntityReference:
    """Any Salesforce record → :class:`EntityReference`, keyed on its 18/15-char Id."""
    record_id = require_id(record.get("Id"), "Salesforce record Id")
    return m.EntityReference(
        entity_type=entity_type,
        source_system=SOURCE,
        source_record_id=record_id,
        display_name=text(record.get("Name") or record.get("CaseNumber")),
    )


def map_case_history_stream(
    org_id: str, records: List[Dict[str, Any]]
) -> Dict[str, List[Any]]:
    """Split a ``CaseHistory`` batch into transitions and assignments.

    One pass over the history produces both concepts, and ``hop_index`` is assigned
    here because this is the first place the ORDER of a case's assignments is known.
    Rows are processed in the order given; the caller is responsible for supplying
    them chronologically, which is how Salesforce returns them when ordered by
    ``CreatedDate``.
    """
    transitions: List[m.StateTransition] = []
    assignments: List[m.Assignment] = []
    hops: Dict[str, int] = {}
    for row in records:
        transition = map_case_history_transition(org_id, row)
        if transition is not None:
            transitions.append(transition)
            continue
        case_id = text(row.get("CaseId")) or ""
        hop = hops.get(case_id, 0)
        assignment = map_case_owner_assignment(org_id, row, hop_index=hop)
        if assignment is not None:
            assignments.append(assignment)
            hops[case_id] = hop + 1
    return {"transitions": transitions, "assignments": assignments}


__all__ = [
    "SOURCE",
    "QUEUE_ID_PREFIX",
    "USER_ID_PREFIX",
    "owner_group_ref",
    "map_case_work_item",
    "map_case_history_transition",
    "map_case_owner_assignment",
    "map_process_instance_approval",
    "map_queue_actor_group",
    "map_record_reference",
    "map_case_history_stream",
]
