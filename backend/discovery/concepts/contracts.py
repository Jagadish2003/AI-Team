"""2.0-B4 T1 — versioned mapping contracts for the normalised concept set (AC1).

A concept definition alone is not a contract. What a connector implementer needs to
know is: **which fields must I populate, which are optional, which vocabulary does
each normalised token come from, and how do I know if the rules changed under me?**
This module is that answer, as data.

Why the contracts are DATA rather than prose in a doc
-----------------------------------------------------
MSP-B0's mapping contract lives in a markdown table plus reference mappers. That
worked for one concept and three providers. At seven concepts across a dozen
connectors, a prose-only contract drifts from the model the moment someone adds a
field — and nothing fails when it does. Here the contract is derived-checkable: a
test asserts every required field a contract names actually exists on the class that
implements the concept, so a contract cannot describe a model that is not there, and
a model field cannot quietly appear with no contract entry.

Versioning (the second half of AC1)
-----------------------------------
Two levels, because they answer different questions:

* :data:`CONCEPT_SET_VERSION` — the version of the SET. Bump when a concept is added
  or removed, i.e. when the vocabulary a pack author builds against changes shape.
* ``MappingContract.version`` — the version of ONE concept's contract. Bump when its
  required fields or vocabularies change, i.e. when a previously-conformant connector
  might no longer be.

The distinction matters at the moment it usually gets blurred: adding a *concept*
does not invalidate any existing connector's conformance, whereas adding a *required
field* to an existing concept invalidates every declaration for it. One version
number could not express both, so a bump would either overstate the breakage or
understate it.

:data:`BREAKING_CHANGE_RULES` states, in the module, what obliges a bump. A rule kept
only in a reviewer's head is a rule that gets forgotten at the point it costs
something.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from discovery.concepts import model as m
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts import model as m


#: Version of the CONCEPT SET — bump when a concept is added or removed.
#: 1: work_item, actor_group, artifact, state_transition, approval, assignment,
#:    entity_reference (2.0-B4 T1).
CONCEPT_SET_VERSION = 1

#: What obliges a version bump. Stated here rather than left to reviewer memory,
#: because the cost of forgetting lands on a connector author who believes they
#: still conform.
BREAKING_CHANGE_RULES: Tuple[str, ...] = (
    "Adding a concept to CONCEPT_SET bumps CONCEPT_SET_VERSION (a pack author's "
    "vocabulary grew) but bumps no contract version — every existing declaration "
    "remains valid.",
    "Removing a concept bumps CONCEPT_SET_VERSION and invalidates every declaration "
    "naming it; the removal must state what replaces it.",
    "Adding a REQUIRED field to a concept bumps that contract's version — every "
    "connector declaring it may no longer conform.",
    "Adding an OPTIONAL field does not bump: an existing mapper stays conformant.",
    "Removing a value from a closed vocabulary bumps that contract's version — a "
    "connector may have been emitting it. ADDING a value does not.",
    "Renaming a field is a removal plus an addition and bumps the contract version; "
    "there is no in-place rename that preserves conformance.",
)


@dataclass(frozen=True)
class FieldContract:
    """One field a connector must or may populate."""

    name: str
    required: bool
    description: str
    #: Name of the closed vocabulary this field's value must come from, if any.
    #: Resolved against :mod:`discovery.concepts.model` so the contract cannot name a
    #: vocabulary that does not exist.
    vocabulary: Optional[str] = None
    #: True when the field carries the source's own value verbatim for trace-back.
    #: Such fields are never normalised, and a mapper must not invent one.
    native_passthrough: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "description": self.description,
            "vocabulary": self.vocabulary,
            "native_passthrough": self.native_passthrough,
        }


@dataclass(frozen=True)
class MappingContract:
    """The versioned contract for mapping a source record onto one concept."""

    concept: str
    version: int
    purpose: str
    fields: Tuple[FieldContract, ...]
    #: Rules a mapper must honour that are not expressible as a field constraint —
    #: the ones that get violated precisely because they are not mechanical.
    rules: Tuple[str, ...] = ()

    @property
    def required_fields(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def optional_fields(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields if not f.required)

    def field(self, name: str) -> Optional[FieldContract]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "version": self.version,
            "concept_set_version": CONCEPT_SET_VERSION,
            "purpose": self.purpose,
            "required_fields": list(self.required_fields),
            "optional_fields": list(self.optional_fields),
            "fields": [f.to_dict() for f in self.fields],
            "rules": list(self.rules),
        }


# ─────────────────────────────────────────────────────────────────────────────
# The spine every observation profile inherits (documented once, not seven times)
# ─────────────────────────────────────────────────────────────────────────────

#: Fields every concept OBSERVATION carries, inherited from ``CommonSignal`` /
#: ``ConceptSignal``. Listed once and spliced into each contract so the seven cannot
#: drift from each other, and so a reader sees the full obligation in one place.
SPINE_FIELDS: Tuple[FieldContract, ...] = (
    FieldContract("org_id", True, "Owning org — every signal is org-scoped (tenancy)."),
    FieldContract("source_system", True, "Connector/source id, e.g. 'servicenow'."),
    FieldContract("signal_id", True, "Stable id within the source system."),
    FieldContract("observed_at", True, "UTC ISO-8601 time the record was observed."),
    FieldContract(
        "provenance", True,
        "A VALID OBSERVED EvidencePointer spine (R16-B1). A concept with no "
        "traceable origin is not persistable.",
    ),
    FieldContract(
        "native_type", False,
        "The source's own type string, kept verbatim for trace-back.",
        native_passthrough=True,
    ),
    FieldContract(
        "attributes", False,
        "Source-specific detail. Carries extra fields WITHOUT leaking a "
        "source-specific shape into the contract (MSP-B0's payload rule).",
    ),
    FieldContract(
        "entity_refs", False,
        "EntityReference list linking this observation to graph entities.",
    ),
)


def _contract(
    concept: str, version: int, purpose: str,
    own_fields: Tuple[FieldContract, ...],
    rules: Tuple[str, ...] = (),
    include_spine: bool = True,
) -> MappingContract:
    fields = (SPINE_FIELDS + own_fields) if include_spine else own_fields
    return MappingContract(
        concept=concept, version=version, purpose=purpose, fields=fields, rules=rules
    )


WORK_ITEM_CONTRACT = _contract(
    m.CONCEPT_WORK_ITEM, 1,
    "A tracked unit of work — the concept most detectors start from.",
    (
        FieldContract("work_item_type", True, "What kind of work.", "WORK_ITEM_TYPES"),
        FieldContract(
            "status_category", True,
            "Coarse lifecycle position. Deliberately coarse: a detector needs "
            "'still open?', not forty native status strings.",
            "STATUS_CATEGORIES",
        ),
        FieldContract(
            "native_status", False, "The source's own status string.",
            native_passthrough=True,
        ),
        FieldContract("priority", False, "Normalised urgency.", "PRIORITY_LEVELS"),
        FieldContract("reference", False, "Human-facing key (INC0000001, PAY-42)."),
        FieldContract("title", False, "Short description."),
        FieldContract("opened_at", False, "UTC ISO-8601 creation time."),
        FieldContract("resolved_at", False, "UTC ISO-8601 resolution time."),
        FieldContract("closed_at", False, "UTC ISO-8601 close time."),
        FieldContract(
            "assigned_group", False,
            "EntityReference to the owning GROUP — never a person's name.",
        ),
    ),
    rules=(
        "Map the native status onto a category; keep the native value on "
        "native_status. Never guess a category from a name you have not mapped — "
        "declare the gap instead.",
        "'cancelled' is NOT 'resolved'. Treating abandoned work as completed "
        "overstates throughput for every detector downstream.",
        "assigned_group is a group reference. If the source only records an "
        "individual assignee, do NOT synthesise a group from their name; leave it "
        "None and declare the gap.",
    ),
)

ACTOR_GROUP_CONTRACT = _contract(
    m.CONCEPT_ACTOR_GROUP, 1,
    "A team, queue, department, role or vendor — never an individual.",
    (
        FieldContract("group_type", True, "What kind of group.", "ACTOR_GROUP_TYPES"),
        FieldContract("name", True, "The group's display name."),
        FieldContract(
            "member_count", False,
            "Aggregate size only. There is deliberately no member list.",
        ),
    ),
    rules=(
        "Never emit an ActorGroup that represents one person. The platform's "
        "standing rule is that output names groups, queues and processes only, and "
        "this concept is the only actor concept precisely so that rule has nowhere "
        "to leak.",
        "member_count is an aggregate; a roster is not part of the contract at any "
        "version.",
    ),
)

ARTIFACT_CONTRACT = _contract(
    m.CONCEPT_ARTIFACT, 1,
    "A document, page, attachment, code file, commit, runbook or thread.",
    (
        FieldContract("artifact_type", True, "What the artifact IS.", "ARTIFACT_TYPES"),
        FieldContract(
            "content_type", True,
            "How its content should be READ. Same vocabulary as the retrieval "
            "substrate's chunk types and 2.0-B3's assembly source types.",
            "CONTENT_TYPES",
        ),
        FieldContract("title", False, "Display title."),
        FieldContract("location", False, "URL / path / repo@sha:path."),
        FieldContract("revision", False, "Version, eTag or commit sha."),
        FieldContract("updated_at", False, "UTC ISO-8601 last-modified time."),
    ),
    rules=(
        "content_type must match the value the retrieval substrate would chunk this "
        "artifact under. Two vocabularies for one idea drift; classify once.",
        "Never place artifact CONTENT in attributes. The artifact concept is a "
        "reference to content, and content reaches retrieval through the substrate's "
        "single ingest path — where secret redaction runs.",
    ),
)

STATE_TRANSITION_CONTRACT = _contract(
    m.CONCEPT_STATE_TRANSITION, 1,
    "One recorded change of state on a work item.",
    (
        FieldContract(
            "transition_type", True, "Why the state changed.", "TRANSITION_TYPES"
        ),
        FieldContract("work_item", False, "EntityReference to the item that changed."),
        FieldContract(
            "from_status_category", False, "Normalised prior state.", "STATUS_CATEGORIES"
        ),
        FieldContract(
            "to_status_category", False, "Normalised new state.", "STATUS_CATEGORIES"
        ),
        FieldContract(
            "from_native_status", False, "Source's own prior status.",
            native_passthrough=True,
        ),
        FieldContract(
            "to_native_status", False, "Source's own new status.",
            native_passthrough=True,
        ),
        FieldContract("transitioned_at", False, "UTC ISO-8601 transition time."),
        FieldContract("actor_group", False, "EntityReference to the group that moved it."),
    ),
    rules=(
        "A closed item returning to open is 'reopen', NOT 'status_change'. Rework is "
        "the signal a detector is looking for; collapsing it erases it.",
        "Emit one transition per recorded change. Do not synthesise transitions the "
        "source did not record — an inferred history is not an observed one.",
    ),
)

APPROVAL_CONTRACT = _contract(
    m.CONCEPT_APPROVAL, 1,
    "One approval gate on a work item or change.",
    (
        FieldContract("decision", True, "Outcome, including 'pending'.", "APPROVAL_DECISIONS"),
        FieldContract("approval_type", False, "What kind of gate.", "APPROVAL_TYPES"),
        FieldContract("work_item", False, "EntityReference to the item being approved."),
        FieldContract(
            "approver_group", False,
            "EntityReference to the approving GROUP — never a named approver.",
        ),
        FieldContract("requested_at", False, "UTC ISO-8601 request time."),
        FieldContract("decided_at", False, "UTC ISO-8601 decision time."),
        FieldContract("step_index", False, "Position in a multi-step approval chain."),
    ),
    rules=(
        "An undecided approval MUST be emitted with decision='pending'. Omitting it "
        "hides exactly what an approval-bottleneck detector measures.",
        "approver_group is a group. A source that records only an individual "
        "approver leaves this None and declares the gap.",
    ),
)

ASSIGNMENT_CONTRACT = _contract(
    m.CONCEPT_ASSIGNMENT, 1,
    "Work arriving at a group.",
    (
        FieldContract(
            "assignment_type", True, "How the work arrived.", "ASSIGNMENT_TYPES"
        ),
        FieldContract("work_item", False, "EntityReference to the assigned item."),
        FieldContract("assigned_to", False, "EntityReference to the receiving group."),
        FieldContract("assigned_at", False, "UTC ISO-8601 assignment time."),
        FieldContract(
            "hop_index", False,
            "Position in this item's assignment chain — what a handoff-churn "
            "detector counts.",
        ),
    ),
    rules=(
        "The first assignment is 'initial'; every later one is a reassignment, "
        "escalation or delegation. That distinction is the whole point of the "
        "concept — a stream of undifferentiated assignments cannot answer 'how many "
        "times was this passed on?'.",
        "assigned_to is a group reference, never an individual.",
    ),
)

ENTITY_REFERENCE_CONTRACT = _contract(
    m.CONCEPT_ENTITY_REFERENCE, 1,
    "A normalised pointer to a graph entity or source record. A VALUE TYPE, not an "
    "observation — it has no provenance of its own because it is always carried by "
    "an observation that does (MSP-B0's ResourceRef plays the same role).",
    (
        FieldContract(
            "entity_type", True,
            "Kind of entity — the SAME closed set the knowledge graph uses.",
            "ENTITY_REFERENCE_TYPES",
        ),
        FieldContract("source_system", True, "Source the record belongs to."),
        FieldContract("source_record_id", True, "Source-native id, kept verbatim.",
                      native_passthrough=True),
        FieldContract("display_name", False, "Human-readable name."),
        FieldContract(
            "entity_id", False,
            "Resolved graph id, or None when unresolved. None is honest, not "
            "missing: 2.0-B2's discipline is that an unresolved reference must never "
            "be treated as resolved.",
        ),
    ),
    rules=(
        "Never fabricate entity_id to make a reference look resolved. Resolution is "
        "the graph's decision (app/cross_source_resolution.py), not a mapper's.",
        "source_record_id is passed through verbatim. A normalised or prettified id "
        "cannot be traced back to the source record.",
    ),
    include_spine=False,  # a value type carries no observation spine
)


#: Every contract, by concept. The registry a connector, a pack author and the
#: conformance validator all read — one source of truth rather than three.
CONTRACTS: Dict[str, MappingContract] = {
    m.CONCEPT_WORK_ITEM: WORK_ITEM_CONTRACT,
    m.CONCEPT_ACTOR_GROUP: ACTOR_GROUP_CONTRACT,
    m.CONCEPT_ARTIFACT: ARTIFACT_CONTRACT,
    m.CONCEPT_STATE_TRANSITION: STATE_TRANSITION_CONTRACT,
    m.CONCEPT_APPROVAL: APPROVAL_CONTRACT,
    m.CONCEPT_ASSIGNMENT: ASSIGNMENT_CONTRACT,
    m.CONCEPT_ENTITY_REFERENCE: ENTITY_REFERENCE_CONTRACT,
}


def get_contract(concept: str) -> MappingContract:
    """The contract for one concept, or a named error listing the valid ones."""
    try:
        return CONTRACTS[concept]
    except KeyError:
        raise KeyError(
            f"{concept!r} is not a normalised concept; the set is "
            f"{sorted(m.CONCEPT_SET)}"
        ) from None


def vocabulary(name: str) -> frozenset:
    """Resolve a vocabulary name a contract references to its closed set.

    Looked up on the model rather than duplicated here, so a contract cannot name a
    vocabulary that does not exist and the two cannot drift.
    """
    value = getattr(m, name, None)
    if not isinstance(value, frozenset):
        raise KeyError(f"{name!r} is not a closed vocabulary in discovery.concepts.model")
    return value


def contract_summary() -> Dict[str, Any]:
    """The whole contract set, serialisable — the audit/documentation surface.

    What a pack author (2.0-C3) reads to know the vocabulary they build against, and
    what a reviewer diffs to see whether a version bump was owed.
    """
    return {
        "concept_set_version": CONCEPT_SET_VERSION,
        "concepts": sorted(m.CONCEPT_SET),
        "contract_versions": {c: CONTRACTS[c].version for c in sorted(CONTRACTS)},
        "breaking_change_rules": list(BREAKING_CHANGE_RULES),
        "contracts": {c: CONTRACTS[c].to_dict() for c in sorted(CONTRACTS)},
    }


__all__ = [
    "CONCEPT_SET_VERSION",
    "BREAKING_CHANGE_RULES",
    "SPINE_FIELDS",
    "FieldContract",
    "MappingContract",
    "CONTRACTS",
    "WORK_ITEM_CONTRACT",
    "ACTOR_GROUP_CONTRACT",
    "ARTIFACT_CONTRACT",
    "STATE_TRANSITION_CONTRACT",
    "APPROVAL_CONTRACT",
    "ASSIGNMENT_CONTRACT",
    "ENTITY_REFERENCE_CONTRACT",
    "get_contract",
    "vocabulary",
    "contract_summary",
]
