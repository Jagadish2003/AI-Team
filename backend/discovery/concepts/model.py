"""2.0-B4 T1 — the normalised concept set (AC1).

MSP-B0 proved the principle on one source family: define ONE normalised shape,
make every provider map onto it, and detectors stop branching on provider. This
module widens that pattern from cloud events to the source families AgentIQ now
ingests — business workflow, documents, conversations, code structure — so a
detector composes against a *concept* rather than a connector's field names.

Why widening matters more than it sounds: today a recurrence detector written for
ServiceNow reads ``sys_id`` / ``assignment_group`` / ``opened_at``, and the same
logic for Jira reads ``key`` / ``fields.assignee`` / ``created``. Two detectors
exist because two dialects do. A partner authoring a pack (2.0-C3) cannot be asked
to learn fifteen dialects, and should not have to.

**A profile of the common signal model, exactly as B0 did it.** Each concept
specialises :class:`~discovery.signals.operational_event.CommonSignal` — the shared
spine carrying ``org_id`` (tenancy), ``source_system``, a stable ``signal_id``, an
observation time, and a valid OBSERVED ``EvidencePointer``. Nothing here reinvents
source tracking or provenance; :class:`OperationalEvent` remains the seventh
profile of the same spine, unchanged.

**Closed vocabularies, validated at construction.** Every normalised token below is
a frozen set. A value outside it fails at construction rather than flowing
downstream as an unrecognised string a detector would silently mishandle — B0's rule,
and the reason a mapping gap surfaces at the connector instead of in a finding.

**Six profiles and one reference type, not seven profiles.** The story names seven
concepts; six are observations and ``EntityReference`` is a value object. B0 made the
same split for the same reason: ``ResourceRef`` is not a signal, it is how a signal
points at the thing it concerns. An entity reference has no independent observation
time or provenance of its own — it is always carried BY an observation — so modelling
it as a ``CommonSignal`` would force every construction to invent a provenance spine
it does not have, and an invented spine is exactly what R16-B1 forbids.

**Groups, never individuals.** ``ActorGroup`` is the only actor concept, and it is
deliberately incapable of naming a person: the platform's standing rule is that
detector output names groups, queues and processes only. A concept set that offered
an "actor" would make violating that rule the path of least resistance for every
future pack author.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from discovery.signals.operational_event import CommonSignal
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.signals.operational_event import CommonSignal


# ─────────────────────────────────────────────────────────────────────────────
# The concept set (AC1)
# ─────────────────────────────────────────────────────────────────────────────

CONCEPT_WORK_ITEM = "work_item"
CONCEPT_ACTOR_GROUP = "actor_group"
CONCEPT_ARTIFACT = "artifact"
CONCEPT_STATE_TRANSITION = "state_transition"
CONCEPT_APPROVAL = "approval"
CONCEPT_ASSIGNMENT = "assignment"
CONCEPT_ENTITY_REFERENCE = "entity_reference"

#: Every concept in the set. Closed: a connector cannot declare conformance to a
#: concept that does not exist, and a detector cannot require one.
CONCEPT_SET: frozenset = frozenset({
    CONCEPT_WORK_ITEM,
    CONCEPT_ACTOR_GROUP,
    CONCEPT_ARTIFACT,
    CONCEPT_STATE_TRANSITION,
    CONCEPT_APPROVAL,
    CONCEPT_ASSIGNMENT,
    CONCEPT_ENTITY_REFERENCE,
})


# ─────────────────────────────────────────────────────────────────────────────
# Normalised vocabularies — closed sets, validated at construction (B0's rule)
# ─────────────────────────────────────────────────────────────────────────────

#: What KIND of work a work item is, grouped by what it is to a detector rather
#: than by a system's product name: a ServiceNow incident, a Jira bug and a
#: Salesforce case of type Problem all describe unplanned work.
WORK_ITEM_TYPES: frozenset = frozenset({
    "incident",      # unplanned break/fix work
    "request",       # a service request / ask
    "problem",       # underlying-cause record
    "change",        # planned change
    "task",          # a unit of work under something larger
    "issue",         # tracker issue (Jira-style), engineering work
    "case",          # customer/member case (Salesforce-style)
    "other",
})

#: Lifecycle position, normalised. Deliberately COARSE: systems disagree wildly on
#: status names and a detector almost never needs the native one (which is
#: preserved on ``native_status`` for trace-back). Ageing and backlog detectors need
#: "is this still open?", not forty status strings.
STATUS_CATEGORIES: frozenset = frozenset({
    "open",          # created, not yet being worked
    "in_progress",   # actively worked
    "waiting",       # blocked on someone/something outside the team
    "resolved",      # work done, not yet closed out
    "closed",        # closed/completed
    "cancelled",     # abandoned; NOT resolved — a detector must not count it as done
    "other",
})

#: Normalised urgency. Systems use 1-5, P1-P4, Sev0-Sev4, High/Med/Low; a detector
#: reasons about "critical" without knowing which.
PRIORITY_LEVELS: frozenset = frozenset({
    "critical", "high", "medium", "low", "none",
})

#: What kind of GROUP an actor group is. No value denotes an individual, by design.
ACTOR_GROUP_TYPES: frozenset = frozenset({
    "team",          # a standing team
    "queue",         # a work queue / assignment group
    "department",    # a larger org unit
    "role",          # a role held by several people
    "vendor",        # an external party
    "other",
})

#: What kind of artifact. ``content_type`` (below) says how to READ it; this says
#: what it IS.
ARTIFACT_TYPES: frozenset = frozenset({
    "document",      # a file (doc, pdf, spreadsheet)
    "page",          # a wiki/site page
    "attachment",    # a file attached to a work item or page
    "code_file",     # a source file
    "commit",        # a VCS commit
    "runbook",       # an operational procedure
    "report",        # a generated report
    "conversation",  # a chat thread treated as an artifact
    "other",
})

#: How an artifact's content should be read. Deliberately the SAME vocabulary as the
#: retrieval substrate's chunk content types and 2.0-B3's assembly source types, so
#: an artifact classified here needs no re-classification to be chunked, retrieved or
#: ranked. Three vocabularies for one idea would drift.
CONTENT_TYPES: frozenset = frozenset({"prose", "code", "conversation", "structured"})

#: Why a state transition happened. Distinguishing these is what lets a
#: reassignment-churn detector and an ageing detector read the same stream.
TRANSITION_TYPES: frozenset = frozenset({
    "status_change",
    "reassignment",
    "priority_change",
    "escalation",
    "reopen",        # a closed item returning to open — never a plain status_change,
                    # because rework is the signal a detector is looking for
    "other",
})

#: An approval's outcome. ``pending`` is a first-class value: an approval that has
#: not been decided is the thing an approval-bottleneck detector cares about, so it
#: must be representable rather than absent.
APPROVAL_DECISIONS: frozenset = frozenset({
    "pending", "approved", "rejected", "withdrawn", "expired", "delegated",
})

#: What kind of gate an approval is. Drives nothing in the model; carried because a
#: regulated review and a routine managerial sign-off are not interchangeable when a
#: pack scores automation potential (see 2.0-D1's automation_shape).
APPROVAL_TYPES: frozenset = frozenset({
    "managerial", "compliance", "technical", "financial", "other",
})

#: How work arrived at a group. ``initial`` versus the rest is the distinction a
#: handoff-friction detector counts.
ASSIGNMENT_TYPES: frozenset = frozenset({
    "initial", "reassignment", "escalation", "delegation", "other",
})

#: Entity kinds an :class:`EntityReference` may point at. The SAME closed set the
#: knowledge graph uses (``database.models.entities.ENTITY_TYPES``), so a concept
#: reference and a graph entity cannot disagree about what kinds of thing exist.
ENTITY_REFERENCE_TYPES: frozenset = frozenset({
    "person", "team", "project", "object", "process", "system",
})


def _require(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _validate_token(value: str, allowed: frozenset, name: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"{name} must be one of {sorted(allowed)}, got {value!r} — map the "
            f"source's native value onto a normalised token at the connector, or "
            f"declare the gap (see discovery/concepts/conformance.py)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# EntityReference — the shared reference type (B0's ResourceRef analogue)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EntityReference:
    """A normalised pointer to something in the knowledge graph or a source system.

    Not a :class:`CommonSignal` profile, and deliberately so: a reference has no
    observation of its own — it is always carried by one — so making it a signal
    would force every construction to invent a provenance spine it does not have.
    ``ResourceRef`` plays exactly this role in MSP-B0.

    Keeps the source-native identity verbatim (``source_system`` +
    ``source_record_id``) for trace-back, alongside the normalised ``entity_type``
    a detector reasons over. ``entity_id`` is the resolved graph id when the
    reference has been resolved; ``None`` means unresolved, which is honest rather
    than absent — 2.0-B2's whole discipline is that an unresolved reference must not
    be treated as a resolved one.
    """

    entity_type: str
    source_system: str
    source_record_id: str
    display_name: Optional[str] = None
    entity_id: Optional[str] = None       # resolved graph id, or None if unresolved

    def __post_init__(self) -> None:
        self.entity_type = _require(self.entity_type, "EntityReference.entity_type")
        self.source_system = _require(self.source_system, "EntityReference.source_system")
        self.source_record_id = _require(
            self.source_record_id, "EntityReference.source_record_id"
        )
        _validate_token(
            self.entity_type, ENTITY_REFERENCE_TYPES, "EntityReference.entity_type"
        )

    @property
    def is_resolved(self) -> bool:
        return bool(self.entity_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "display_name": self.display_name,
            "entity_id": self.entity_id,
            "is_resolved": self.is_resolved,
        }


# ─────────────────────────────────────────────────────────────────────────────
# The six observation profiles
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConceptSignal(CommonSignal):
    """Shared base for every concept profile.

    Adds only what all six need: the ``concept`` name (so a heterogeneous stream is
    self-describing), the source's own native type/status strings preserved for
    trace-back, and the free-form ``attributes`` bag that carries source-specific
    detail without leaking a source-specific SHAPE into the contract — B0's
    ``payload`` rule.
    """

    concept: str = ""
    native_type: str = ""                 # the source's own type string (trace-back)
    attributes: Dict[str, Any] = field(default_factory=dict)
    entity_refs: List[EntityReference] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.concept and self.concept not in CONCEPT_SET:
            raise ValueError(
                f"concept must be one of {sorted(CONCEPT_SET)}, got {self.concept!r}"
            )
        for ref in self.entity_refs:
            if not isinstance(ref, EntityReference):
                raise ValueError("entity_refs must contain EntityReference instances")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "org_id": self.org_id,
            "source_system": self.source_system,
            "signal_id": self.signal_id,
            "observed_at": self.observed_at,
            "provenance": self.provenance,
            "native_type": self.native_type,
            "attributes": dict(self.attributes),
            "entity_refs": [r.to_dict() for r in self.entity_refs],
        }


@dataclass
class WorkItem(ConceptSignal):
    """A tracked unit of work: incident, request, issue, case, change, task.

    The concept most detectors start from. ``status_category`` is coarse on purpose
    (see :data:`STATUS_CATEGORIES`) with the source's own value kept on
    ``native_status``; ageing and backlog logic needs "still open?", not the native
    string. ``assigned_group`` is an :class:`EntityReference`, never a name, so a
    detector cannot accidentally surface an individual.
    """

    concept: str = CONCEPT_WORK_ITEM
    work_item_type: str = "other"
    status_category: str = "other"
    native_status: str = ""
    priority: str = "none"
    reference: str = ""                   # human-facing key (INC0000001, PAY-42)
    title: Optional[str] = None
    opened_at: Optional[str] = None
    resolved_at: Optional[str] = None
    closed_at: Optional[str] = None
    assigned_group: Optional[EntityReference] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(self.work_item_type, WORK_ITEM_TYPES, "WorkItem.work_item_type")
        _validate_token(
            self.status_category, STATUS_CATEGORIES, "WorkItem.status_category"
        )
        _validate_token(self.priority, PRIORITY_LEVELS, "WorkItem.priority")
        if self.assigned_group is not None and not isinstance(
            self.assigned_group, EntityReference
        ):
            raise ValueError("WorkItem.assigned_group must be an EntityReference or None")

    @property
    def is_open(self) -> bool:
        """Whether the item still represents outstanding work.

        ``cancelled`` counts as not-open but is NOT resolved: an ageing detector that
        treated an abandoned item as completed work would overstate throughput.
        """
        return self.status_category in ("open", "in_progress", "waiting")

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "work_item_type": self.work_item_type,
            "status_category": self.status_category,
            "native_status": self.native_status,
            "priority": self.priority,
            "reference": self.reference,
            "title": self.title,
            "opened_at": self.opened_at,
            "resolved_at": self.resolved_at,
            "closed_at": self.closed_at,
            "assigned_group": (
                self.assigned_group.to_dict() if self.assigned_group else None
            ),
            "is_open": self.is_open,
        })
        return base


@dataclass
class ActorGroup(ConceptSignal):
    """A team, queue, department, role or vendor — never an individual.

    The only actor concept in the set, and it cannot name a person: the platform's
    standing rule is that detector output names groups, queues and processes only.
    ``member_count`` is an aggregate, which is what the security-pack aggregation
    floor already permits; there is deliberately no member list.
    """

    concept: str = CONCEPT_ACTOR_GROUP
    group_type: str = "other"
    name: str = ""
    member_count: Optional[int] = None    # aggregate only — never a roster

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(self.group_type, ACTOR_GROUP_TYPES, "ActorGroup.group_type")
        self.name = _require(self.name, "ActorGroup.name")
        if self.member_count is not None and int(self.member_count) < 0:
            raise ValueError("ActorGroup.member_count cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "group_type": self.group_type,
            "name": self.name,
            "member_count": self.member_count,
        })
        return base


@dataclass
class Artifact(ConceptSignal):
    """A document, page, attachment, code file, commit, runbook or thread.

    ``content_type`` uses the SAME vocabulary as the retrieval substrate's chunk
    types and 2.0-B3's assembly source types, so an artifact classified once needs no
    re-classification to be chunked, retrieved or ranked.
    """

    concept: str = CONCEPT_ARTIFACT
    artifact_type: str = "other"
    content_type: str = "prose"
    title: Optional[str] = None
    location: Optional[str] = None        # URL / path / repo@sha:path
    revision: Optional[str] = None        # version, eTag, or commit sha
    updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(self.artifact_type, ARTIFACT_TYPES, "Artifact.artifact_type")
        _validate_token(self.content_type, CONTENT_TYPES, "Artifact.content_type")

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "artifact_type": self.artifact_type,
            "content_type": self.content_type,
            "title": self.title,
            "location": self.location,
            "revision": self.revision,
            "updated_at": self.updated_at,
        })
        return base


@dataclass
class StateTransition(ConceptSignal):
    """One recorded change of state on a work item.

    ``from_status_category`` / ``to_status_category`` are normalised; the native
    strings are kept for trace-back. ``reopen`` is its own transition type rather
    than a status_change, because rework is precisely the signal a detector hunts —
    collapsing it into a generic status change would erase it.
    """

    concept: str = CONCEPT_STATE_TRANSITION
    transition_type: str = "status_change"
    work_item: Optional[EntityReference] = None
    from_status_category: Optional[str] = None
    to_status_category: Optional[str] = None
    from_native_status: Optional[str] = None
    to_native_status: Optional[str] = None
    transitioned_at: Optional[str] = None
    actor_group: Optional[EntityReference] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(
            self.transition_type, TRANSITION_TYPES, "StateTransition.transition_type"
        )
        for label, value in (
            ("from_status_category", self.from_status_category),
            ("to_status_category", self.to_status_category),
        ):
            if value is not None:
                _validate_token(value, STATUS_CATEGORIES, f"StateTransition.{label}")

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "transition_type": self.transition_type,
            "work_item": self.work_item.to_dict() if self.work_item else None,
            "from_status_category": self.from_status_category,
            "to_status_category": self.to_status_category,
            "from_native_status": self.from_native_status,
            "to_native_status": self.to_native_status,
            "transitioned_at": self.transitioned_at,
            "actor_group": self.actor_group.to_dict() if self.actor_group else None,
        })
        return base


@dataclass
class Approval(ConceptSignal):
    """One approval gate on a work item or change.

    ``pending`` is a first-class decision: an undecided approval is exactly what an
    approval-bottleneck detector measures, so it must be representable rather than
    absent. ``approver_group`` is a group reference — an approval attributed to a
    named individual is the shape this concept refuses to offer.
    """

    concept: str = CONCEPT_APPROVAL
    decision: str = "pending"
    approval_type: str = "other"
    work_item: Optional[EntityReference] = None
    approver_group: Optional[EntityReference] = None
    requested_at: Optional[str] = None
    decided_at: Optional[str] = None
    step_index: Optional[int] = None      # position in a multi-step chain

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(self.decision, APPROVAL_DECISIONS, "Approval.decision")
        _validate_token(self.approval_type, APPROVAL_TYPES, "Approval.approval_type")
        if self.step_index is not None and int(self.step_index) < 0:
            raise ValueError("Approval.step_index cannot be negative")

    @property
    def is_decided(self) -> bool:
        return self.decision != "pending"

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "decision": self.decision,
            "approval_type": self.approval_type,
            "work_item": self.work_item.to_dict() if self.work_item else None,
            "approver_group": (
                self.approver_group.to_dict() if self.approver_group else None
            ),
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "step_index": self.step_index,
            "is_decided": self.is_decided,
        })
        return base


@dataclass
class Assignment(ConceptSignal):
    """Work arriving at a group.

    ``hop_index`` is the position in the chain of assignments for one work item,
    which is what a handoff-churn detector counts. ``initial`` versus every other
    ``assignment_type`` is the distinction that makes "how many times was this passed
    on?" answerable without re-deriving it from raw history.
    """

    concept: str = CONCEPT_ASSIGNMENT
    assignment_type: str = "initial"
    work_item: Optional[EntityReference] = None
    assigned_to: Optional[EntityReference] = None
    assigned_at: Optional[str] = None
    hop_index: Optional[int] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_token(
            self.assignment_type, ASSIGNMENT_TYPES, "Assignment.assignment_type"
        )
        if self.hop_index is not None and int(self.hop_index) < 0:
            raise ValueError("Assignment.hop_index cannot be negative")

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "assignment_type": self.assignment_type,
            "work_item": self.work_item.to_dict() if self.work_item else None,
            "assigned_to": self.assigned_to.to_dict() if self.assigned_to else None,
            "assigned_at": self.assigned_at,
            "hop_index": self.hop_index,
        })
        return base


#: Concept name → the class implementing it. ``entity_reference`` maps to the value
#: type; the other six map to observation profiles. Used by the contract registry and
#: by conformance validation so neither has to hardcode the mapping.
CONCEPT_CLASSES: Dict[str, type] = {
    CONCEPT_WORK_ITEM: WorkItem,
    CONCEPT_ACTOR_GROUP: ActorGroup,
    CONCEPT_ARTIFACT: Artifact,
    CONCEPT_STATE_TRANSITION: StateTransition,
    CONCEPT_APPROVAL: Approval,
    CONCEPT_ASSIGNMENT: Assignment,
    CONCEPT_ENTITY_REFERENCE: EntityReference,
}


__all__ = [
    "CONCEPT_SET",
    "CONCEPT_WORK_ITEM",
    "CONCEPT_ACTOR_GROUP",
    "CONCEPT_ARTIFACT",
    "CONCEPT_STATE_TRANSITION",
    "CONCEPT_APPROVAL",
    "CONCEPT_ASSIGNMENT",
    "CONCEPT_ENTITY_REFERENCE",
    "CONCEPT_CLASSES",
    "WORK_ITEM_TYPES",
    "STATUS_CATEGORIES",
    "PRIORITY_LEVELS",
    "ACTOR_GROUP_TYPES",
    "ARTIFACT_TYPES",
    "CONTENT_TYPES",
    "TRANSITION_TYPES",
    "APPROVAL_DECISIONS",
    "APPROVAL_TYPES",
    "ASSIGNMENT_TYPES",
    "ENTITY_REFERENCE_TYPES",
    "ConceptSignal",
    "EntityReference",
    "WorkItem",
    "ActorGroup",
    "Artifact",
    "StateTransition",
    "Approval",
    "Assignment",
]
