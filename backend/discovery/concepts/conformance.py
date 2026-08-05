"""2.0-B4 T1 — per-connector conformance declarations (AC1, third clause).

AC1 requires that "each connector declares its conformance". This module is that
declaration, and the design question that decides whether it is worth anything is:
**conformance to what — the connector's DATA or its CODE?**

Both, kept apart, because conflating them is how a conformance registry becomes a
lie. A connector whose source genuinely carries approvals but has no mapper yet is
in a completely different position from one whose source has no notion of approval
at all — and a single boolean would report them identically.

So every (connector, concept) pair carries a :data:`STATUSES` value:

* ``supported`` — the source carries it AND a mapper exists. Only this claims
  conformance. A test refuses this status unless a mapper is registered, so the
  strongest claim cannot be made by editing a comment.
* ``declared`` — the source carries it; the mapper is not built yet. An honest
  statement of intent, and the work list for 2.0-B4 T2/T3.
* ``gap`` — the source cannot supply it, with a REQUIRED reason. This is the
  entry AC5 ("unmappable concepts recorded as declared gaps, never silently
  approximated") is built on: the alternative to a recorded gap is a mapper that
  invents the missing field, which is the failure mode the whole story exists to
  prevent.
* ``not_applicable`` — the concept does not apply to this source family. Distinct
  from ``gap`` on purpose: a cloud-event stream having no approvals is not a
  shortcoming to be fixed, whereas an ITSM tool whose approvals we cannot read is.

2.0-B4 T2 — field-level gaps
----------------------------
T1's four statuses answer "can this connector produce this concept at all?". Building
the mappers exposed that the question a pack author actually asks is finer: *ServiceNow
supports ``state_transition``, but can it tell me which GROUP moved the item?* The
answer is no — the audit row records the change, not the mover — and a concept-level
``supported`` says nothing about it.

So a declaration also carries :class:`FieldGap` entries: contract FIELDS this connector
cannot populate, each with a reason and a ``kind``:

* ``absent`` — never populated by this connector, for any record;
* ``partial`` — populated for some records only, with the condition stated (a
  Salesforce case owned by a queue carries ``assigned_group``; one owned by a person
  does not, because there is no group to name).

This is the level AC5 actually bites at. A concept-level gap is easy to spot — the
concept produces nothing. A field-level gap is exactly the kind of thing that gets
quietly approximated instead, because a plausible-looking value is available right
next to the missing one (the assignee's name where a group belongs). Recording it
makes "this field is empty on purpose" distinguishable from "this field is empty
because ingestion is broken", which is otherwise indistinguishable to a pack author.

A ``FieldGap`` on a REQUIRED contract field is refused when the status is
``supported``: a connector that cannot populate a required field does not support the
concept, and must say ``declared`` or ``gap`` instead.

**T2 status.** The mappers exist now (``discovery/concepts/mappers/``), so the
connectors whose sources genuinely carry a concept AND have a mapper read
``supported`` and name it. What remains ``declared`` is a real work list, not a
formality: each entry names a source that carries the concept and a read the connector
does not yet perform.

The connector list is anchored on ``app.connector_roadmap.SHIPPED_CONNECTOR_IDS``
(R191-R1's honesty rule): a connector whose ingestion does not ship cannot declare
conformance, because there is nothing to conform. A test asserts the two stay in
step, so a newly-shipped connector cannot arrive with no declaration at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from discovery.concepts import model as m
    from discovery.concepts.contracts import CONCEPT_SET_VERSION, get_contract
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.contracts import CONCEPT_SET_VERSION, get_contract


STATUS_SUPPORTED = "supported"
STATUS_DECLARED = "declared"
STATUS_GAP = "gap"
STATUS_NOT_APPLICABLE = "not_applicable"

#: Closed set — an unrecognised status would make the registry unreadable by the
#: conformance validator and the B4 T4 CI gate that follows it.
STATUSES: Tuple[str, ...] = (
    STATUS_SUPPORTED, STATUS_DECLARED, STATUS_GAP, STATUS_NOT_APPLICABLE,
)

#: Statuses that REQUIRE a reason. A gap with no reason is indistinguishable from an
#: oversight, and "not applicable" without a reason invites a later reader to
#: "fix" something that was a deliberate decision.
_REASON_REQUIRED = (STATUS_GAP, STATUS_NOT_APPLICABLE)


#: A field gap is either never populated, or populated only under a stated condition.
GAP_ABSENT = "absent"
GAP_PARTIAL = "partial"
FIELD_GAP_KINDS: Tuple[str, ...] = (GAP_ABSENT, GAP_PARTIAL)


class ConformanceError(ValueError):
    """A conformance declaration is malformed or claims more than it can."""


@dataclass(frozen=True)
class FieldGap:
    """One contract FIELD a connector cannot populate faithfully (2.0-B4 T2).

    The reason is mandatory and, for a ``partial`` gap, is where the CONDITION is
    stated — "only when the owner is a queue" is the useful half of that record, and a
    bare ``partial`` with no condition tells a pack author nothing they can act on.
    """

    field: str
    kind: str
    reason: str

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ConformanceError("FieldGap.field is required")
        if self.kind not in FIELD_GAP_KINDS:
            raise ConformanceError(
                f"FieldGap.kind must be one of {list(FIELD_GAP_KINDS)}, got {self.kind!r}"
            )
        if not self.reason.strip():
            raise ConformanceError(
                f"field gap on {self.field!r} requires a reason — an unexplained empty "
                f"field is indistinguishable from broken ingestion, which is the exact "
                f"ambiguity a recorded gap removes"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "kind": self.kind, "reason": self.reason}


@dataclass(frozen=True)
class ConceptConformance:
    """One connector's position on one concept."""

    concept: str
    status: str
    reason: str = ""
    #: Where the mapping lives, when it exists. Recorded so a `supported` claim
    #: points at something a reviewer can read.
    mapper: Optional[str] = None
    #: Contract fields this connector cannot populate faithfully (2.0-B4 T2).
    field_gaps: Tuple[FieldGap, ...] = ()

    def __post_init__(self) -> None:
        if self.concept not in m.CONCEPT_SET:
            raise ConformanceError(
                f"{self.concept!r} is not a normalised concept; the set is "
                f"{sorted(m.CONCEPT_SET)}"
            )
        if self.status not in STATUSES:
            raise ConformanceError(
                f"status must be one of {list(STATUSES)}, got {self.status!r}"
            )
        if self.status in _REASON_REQUIRED and not self.reason.strip():
            raise ConformanceError(
                f"{self.concept}: status {self.status!r} requires a reason — an "
                f"unexplained gap is indistinguishable from an oversight"
            )
        if self.status == STATUS_SUPPORTED and not (self.mapper or "").strip():
            raise ConformanceError(
                f"{self.concept}: {STATUS_SUPPORTED!r} must name the mapper that "
                f"implements it, so the claim points at readable code"
            )
        self._validate_field_gaps()

    def _validate_field_gaps(self) -> None:
        """A field gap must name a real contract field, and not a required one.

        Both halves matter. A gap naming a field the contract does not have is a stale
        record that will mislead the first pack author who reads it; a gap on a
        REQUIRED field contradicts the ``supported`` claim beside it, and a
        contradictory registry is worse than none because a reader cannot tell which
        half to believe.
        """
        if not self.field_gaps:
            return
        contract = get_contract(self.concept)
        known = {f.name for f in contract.fields}
        required = set(contract.required_fields)
        seen = set()
        for gap in self.field_gaps:
            if gap.field not in known:
                raise ConformanceError(
                    f"{self.concept}: field gap names {gap.field!r}, which is not a "
                    f"field of this contract ({sorted(known)})"
                )
            if gap.field in seen:
                raise ConformanceError(
                    f"{self.concept}: repeats a field gap on {gap.field!r}"
                )
            seen.add(gap.field)
            if self.status == STATUS_SUPPORTED and gap.field in required:
                raise ConformanceError(
                    f"{self.concept}: cannot claim {STATUS_SUPPORTED!r} with a gap on "
                    f"the REQUIRED field {gap.field!r} — a connector that cannot "
                    f"populate a required field does not support the concept; declare "
                    f"{STATUS_DECLARED!r} or {STATUS_GAP!r}"
                )

    @property
    def conforms(self) -> bool:
        """Only ``supported`` is conformance. ``declared`` is intent."""
        return self.status == STATUS_SUPPORTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "status": self.status,
            "reason": self.reason,
            "mapper": self.mapper,
            "conforms": self.conforms,
            "field_gaps": [g.to_dict() for g in self.field_gaps],
        }


@dataclass(frozen=True)
class ConnectorConformance:
    """One connector's full declaration across the concept set."""

    connector_id: str
    source_family: str
    concepts: Tuple[ConceptConformance, ...]
    #: The concept-set version this declaration was written against. A declaration
    #: made against an older set is not silently trusted — see :func:`stale_declarations`.
    concept_set_version: int = CONCEPT_SET_VERSION

    def __post_init__(self) -> None:
        if not self.connector_id.strip():
            raise ConformanceError("connector_id is required")
        seen = [c.concept for c in self.concepts]
        duplicated = {c for c in seen if seen.count(c) > 1}
        if duplicated:
            raise ConformanceError(
                f"{self.connector_id}: repeats concept(s) {sorted(duplicated)} — one "
                f"position per concept, or the registry contradicts itself"
            )
        missing = sorted(m.CONCEPT_SET - set(seen))
        if missing:
            raise ConformanceError(
                f"{self.connector_id}: omits {missing}. Every concept must have a "
                f"position — an omitted concept is silently unmapped, which is the "
                f"exact ambiguity this registry exists to remove"
            )

    def position(self, concept: str) -> ConceptConformance:
        for c in self.concepts:
            if c.concept == concept:
                return c
        raise ConformanceError(f"{self.connector_id} has no position on {concept!r}")

    @property
    def supported(self) -> Tuple[str, ...]:
        return tuple(sorted(c.concept for c in self.concepts if c.conforms))

    @property
    def declared(self) -> Tuple[str, ...]:
        return tuple(sorted(c.concept for c in self.concepts if c.status == STATUS_DECLARED))

    @property
    def gaps(self) -> Tuple[ConceptConformance, ...]:
        """Recorded gaps — what AC5's visibility surface reads."""
        return tuple(c for c in self.concepts if c.status == STATUS_GAP)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "source_family": self.source_family,
            "concept_set_version": self.concept_set_version,
            "supported": list(self.supported),
            "declared": list(self.declared),
            "gaps": [c.to_dict() for c in self.gaps],
            "concepts": [c.to_dict() for c in self.concepts],
        }


def _decl(**by_concept: Any) -> Tuple[ConceptConformance, ...]:
    """Build a full concept-set declaration.

    Each value is either a bare status string, a ``(status, reason)`` pair, a
    ``(status, reason, mapper)`` triple, or a :class:`ConceptConformance` built by
    :func:`_supported` / :func:`_gap` below when field gaps are involved.

    Every concept must be named; :class:`ConnectorConformance` refuses a partial
    declaration, so a new concept added to the set breaks every registry entry loudly
    rather than defaulting a dozen connectors to a position nobody chose.
    """
    positions = []
    for concept, spec in by_concept.items():
        if isinstance(spec, ConceptConformance):
            if spec.concept != concept:
                raise ConformanceError(
                    f"declared under {concept!r} but built for {spec.concept!r}"
                )
            positions.append(spec)
            continue
        if isinstance(spec, str):
            status, reason, mapper = spec, "", None
        elif len(spec) == 2:
            status, reason, mapper = spec[0], spec[1], None
        else:
            status, reason, mapper = spec
        positions.append(
            ConceptConformance(concept=concept, status=status, reason=reason, mapper=mapper)
        )
    return tuple(positions)


def _supported(
    concept: str, mapper: str, *field_gaps: FieldGap, reason: str = ""
) -> ConceptConformance:
    """A ``supported`` position naming its mapper and any field-level gaps (T2).

    ``mapper`` is the ``module:function`` name the mapper registry records.
    ``test_r2_0_b4_t2_connector_mapping.py`` resolves every one of these against
    ``discovery.concepts.mappers.MAPPERS``, so a name that does not point at a
    registered callable fails the build.
    """
    return ConceptConformance(
        concept=concept, status=STATUS_SUPPORTED, reason=reason,
        mapper=mapper, field_gaps=tuple(field_gaps),
    )


def _absent(field: str, reason: str) -> FieldGap:
    """A contract field this connector never populates."""
    return FieldGap(field=field, kind=GAP_ABSENT, reason=reason)


def _partial(field: str, reason: str) -> FieldGap:
    """A contract field populated only under the stated condition."""
    return FieldGap(field=field, kind=GAP_PARTIAL, reason=reason)


# Mapper module prefix, written once so the registry's mapper names stay readable.
_MAP = "discovery.concepts.mappers"


# Reasons reused across connectors — written once so the same situation reads the
# same way everywhere, and a reader can tell two connectors share a cause.
_NO_APPROVAL_MODEL = (
    "the source has no approval concept — it records content/activity, not gated "
    "decisions"
)
_NO_WORK_ITEM_MODEL = (
    "the source has no unit-of-work record; its signal is content or events, not "
    "tracked work"
)
_GROUPS_NOT_MODELLED = (
    "the source exposes individual actors only, and synthesising a group from a "
    "person's name is exactly the inference the contract forbids"
)
_EVENTS_NOT_WORKFLOW = (
    "a cloud/telemetry event stream carries no workflow state; MSP-B0's "
    "OperationalEvent is the correct profile for this source, not the workflow "
    "concepts"
)


# ─────────────────────────────────────────────────────────────────────────────
# The registry — one entry per SHIPPED connector (R191-R1 anchoring)
# ─────────────────────────────────────────────────────────────────────────────

CONFORMANCE: Dict[str, ConnectorConformance] = {
    "servicenow": ConnectorConformance(
        connector_id="servicenow",
        source_family="itsm",
        concepts=_decl(
            work_item=_supported(
                m.CONCEPT_WORK_ITEM, f"{_MAP}.servicenow:map_incident_work_item",
            ),
            actor_group=_supported(
                m.CONCEPT_ACTOR_GROUP, f"{_MAP}.servicenow:map_assignment_group",
                reason=(
                    "an assignment group IS a work queue — work is routed to it and "
                    "drawn from it — so group_type='queue', unlike a chat channel"
                ),
            ),
            state_transition=_supported(
                m.CONCEPT_STATE_TRANSITION, f"{_MAP}.servicenow:map_state_transition",
                _absent(
                    "actor_group",
                    "the sys_audit row records that the state changed, not who changed "
                    "it. The only mover field available is an individual, which the "
                    "concept set has nowhere to put and the platform must not surface.",
                ),
            ),
            assignment=_supported(
                m.CONCEPT_ASSIGNMENT, f"{_MAP}.servicenow:map_assignment_history",
                _partial(
                    "assigned_to",
                    "the assignment audit row stores the group's NAME in "
                    "oldvalue/newvalue, not its sys_id, so this reference is "
                    "name-keyed by the source's own construction. It resolves to a "
                    "group but cannot be looked up by id, and two groups sharing a "
                    "name are indistinguishable.",
                ),
                _partial(
                    "hop_index",
                    "populated only when the caller maps a case's ORDERED assignment "
                    "history and passes the position in; a mapper handed one audit row "
                    "cannot know where in the chain it sits and must not invent it",
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.servicenow:map_cmdb_ci_reference",
            ),
            approval=(
                STATUS_DECLARED,
                "ServiceNow carries first-class approvals (sysapproval_approver), but "
                "this connector does not read that table — the mapping needs a "
                "connector read, not just a mapper",
            ),
            artifact=(
                STATUS_DECLARED,
                "attachments and knowledge articles exist; the connector reads work "
                "notes (for the redaction-before-indexing seam) but no artifact "
                "surface, so there is nothing to map yet",
            ),
        ),
    ),
    "jira": ConnectorConformance(
        connector_id="jira",
        source_family="engineering_tracker",
        concepts=_decl(
            work_item=_supported(
                m.CONCEPT_WORK_ITEM, f"{_MAP}.jira:map_issue_work_item",
                _absent(
                    "assigned_group",
                    "Jira assigns to an individual. JIRA_TEAM_FIELD can carry a team "
                    "but is per-deployment configuration rather than a field Jira "
                    "guarantees, and a mapping that works on only some customers' "
                    "instances would make conformance mean different things per "
                    "deployment.",
                ),
                _absent(
                    "closed_at",
                    "Jira has no close event separate from resolution; resolved_at IS "
                    "the close. Mirroring it into closed_at would give a "
                    "resolve-to-close detector a manufactured zero.",
                ),
                reason=(
                    "status maps from Jira's own statusCategory, with abandoning "
                    "resolutions (Won't Do, Duplicate) routed to 'cancelled' so "
                    "abandoned work is never counted as delivered"
                ),
            ),
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.jira:map_issue_attachment",
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.jira:map_issue_reference",
                reason=(
                    "keyed on the numeric issue id, not the key: a key changes when an "
                    "issue moves project, and a reference that stops resolving after a "
                    "move is not a reference"
                ),
            ),
            actor_group=(
                STATUS_GAP,
                "Jira assigns to individuals; project roles and components are the "
                "closest group-shaped concepts and neither is a work queue. Mapping "
                "an assignee to a group would fabricate one.",
            ),
            state_transition=(
                STATUS_DECLARED,
                "the changelog carries every status change, but this connector does "
                "not request it (no expand=changelog) — the mapping needs a connector "
                "read first",
            ),
            approval=(
                STATUS_GAP,
                "approval is a workflow-transition convention per project, not a "
                "first-class Jira record; reading one reliably needs per-org "
                "configuration this connector does not have",
            ),
            assignment=(
                STATUS_DECLARED,
                "the changelog carries assignee changes (same unread expand as "
                "state_transition). Note the eventual mapper will not be able to "
                "populate assigned_to: Jira hands work to a person, so the hop is "
                "countable but the recipient group does not exist.",
            ),
        ),
    ),
    "salesforce": ConnectorConformance(
        connector_id="salesforce",
        source_family="crm",
        concepts=_decl(
            work_item=_supported(
                m.CONCEPT_WORK_ITEM, f"{_MAP}.salesforce:map_case_work_item",
                _partial(
                    "assigned_group",
                    "populated only when the case OwnerId is a Queue (key prefix "
                    "00G). A case owned by a User (005) carries no group, because "
                    "there is no group to name — the record instead notes "
                    "owner_is_individual so an empty field is explicable.",
                ),
                _absent(
                    "resolved_at",
                    "Salesforce records ClosedDate only; a resolved-but-open state is "
                    "expressed through Status, so mirroring ClosedDate here would "
                    "invent a resolution time",
                ),
            ),
            state_transition=_supported(
                m.CONCEPT_STATE_TRANSITION,
                f"{_MAP}.salesforce:map_case_history_transition",
                _absent(
                    "actor_group",
                    "CaseHistory records the field change and the user who made it, "
                    "never a group",
                ),
            ),
            assignment=_supported(
                m.CONCEPT_ASSIGNMENT, f"{_MAP}.salesforce:map_case_owner_assignment",
                _partial(
                    "assigned_to",
                    "populated only when the new owner is a Queue (00G). A case handed "
                    "to a person still produces the Assignment — the hop happened and "
                    "a churn detector counts hops — but with no recipient, because "
                    "naming it would name an individual.",
                ),
            ),
            approval=_supported(
                m.CONCEPT_APPROVAL, f"{_MAP}.salesforce:map_process_instance_approval",
                _absent(
                    "approver_group",
                    "the approver is on ProcessInstanceWorkitem.ActorId, which is a "
                    "User in the common configuration; a queue-based approver cannot "
                    "be distinguished from a person-based one without reading that "
                    "child object",
                ),
                reason=(
                    "ProcessInstance is a first-class approval record, which is why "
                    "Salesforce supports this concept where Jira cannot; Status="
                    "'Removed' maps to 'withdrawn', never 'rejected'. NOTE "
                    "approval_type is always 'other': Salesforce does not classify a "
                    "process as managerial / compliance / financial, and inferring it "
                    "from the process NAME is the naming heuristic the platform "
                    "refuses. That is recorded here rather than as a field gap because "
                    "the field IS populated — with the vocabulary's neutral value — so "
                    "calling it absent would be false."
                ),
            ),
            actor_group=_supported(
                m.CONCEPT_ACTOR_GROUP, f"{_MAP}.salesforce:map_queue_actor_group",
                reason=(
                    "only Group records with Type='Queue'. Roles, "
                    "RoleAndSubordinates and territory groups are also Salesforce "
                    "Groups and are refused: an org-chart node is not a work queue."
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.salesforce:map_record_reference",
            ),
            artifact=(
                STATUS_NOT_APPLICABLE,
                "the Salesforce ingest surface reads records, not files; attachments "
                "reach the platform through the document path instead",
            ),
        ),
    ),
    "github": ConnectorConformance(
        connector_id="github",
        source_family="code",
        concepts=_decl(
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.content:map_git_artifact",
                reason=(
                    "commits and code files, the two surfaces R18-A2's git content "
                    "path reads. content_type follows the retrieval substrate: 'code' "
                    "for a file, 'conversation' for a commit message."
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.content:map_repo_reference",
            ),
            work_item=(
                STATUS_DECLARED,
                "pull requests are tracked work and the connector reads PR activity, "
                "but as aggregate metrics rather than per-PR records — the mapping "
                "needs a per-record read",
            ),
            state_transition=(
                STATUS_DECLARED,
                "PR open/review/merge/close is a real transition stream on the same "
                "unread per-record surface as work_item",
            ),
            approval=(
                STATUS_DECLARED,
                "a PR review approval is a genuine approval record; the connector does "
                "not read the reviews endpoint yet",
            ),
            actor_group=(
                STATUS_GAP,
                "teams exist in GitHub but the ingest surface reads repository and PR "
                "activity, not org team membership",
            ),
            assignment=(
                STATUS_GAP,
                "PR review requests are per-person; there is no group work queue to "
                "assign to",
            ),
        ),
    ),
    "confluence": ConnectorConformance(
        connector_id="confluence",
        source_family="documentation",
        concepts=_decl(
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.content:map_confluence_page",
                reason=(
                    "content_type='prose' — the value the retrieval substrate chunks "
                    "these under, so an artifact is classified once rather than twice"
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.content:map_confluence_reference",
            ),
            work_item=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            actor_group=(STATUS_GAP, _GROUPS_NOT_MODELLED),
            state_transition=(
                STATUS_GAP,
                "page status (current/archived/trashed) is read for deletion "
                "propagation but is not modelled as a transition stream",
            ),
            approval=(STATUS_NOT_APPLICABLE, _NO_APPROVAL_MODEL),
            assignment=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
        ),
    ),
    "sharepoint": ConnectorConformance(
        connector_id="sharepoint",
        source_family="documentation",
        concepts=_decl(
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.content:map_sharepoint_item",
                reason=(
                    "content_type='prose' — the value the retrieval substrate chunks "
                    "these under, so an artifact is classified once rather than twice"
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.content:map_sharepoint_reference",
            ),
            work_item=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            actor_group=(STATUS_GAP, _GROUPS_NOT_MODELLED),
            state_transition=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            approval=(STATUS_NOT_APPLICABLE, _NO_APPROVAL_MODEL),
            assignment=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
        ),
    ),
    "slack": ConnectorConformance(
        connector_id="slack",
        source_family="conversation",
        concepts=_decl(
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.content:map_slack_thread",
                _absent(
                    "revision",
                    "a conversation thread has no version; an edit re-renders the whole "
                    "thread rather than producing a new revision (R18-A4's refresh "
                    "model), so a revision number would be invented",
                ),
                reason=(
                    "a thread, keyed on the same thread artifact id the R18-A4 "
                    "conversation model uses, so a concept and a retrieval chunk point "
                    "at one artifact rather than two ids for one thread"
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.content:map_slack_reference",
            ),
            actor_group=_supported(
                m.CONCEPT_ACTOR_GROUP, f"{_MAP}.content:map_slack_channel",
                reason=(
                    "a Slack channel is a group-shaped container mapped as group_type='team', "
                    "NOT 'queue' — work is not routed to or drawn from it, and a "
                    "queue-ageing detector reading it as a queue would report backlog "
                    "that does not exist"
                ),
            ),
            work_item=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            state_transition=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            approval=(STATUS_NOT_APPLICABLE, _NO_APPROVAL_MODEL),
            assignment=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
        ),
    ),
    "teams": ConnectorConformance(
        connector_id="teams",
        source_family="conversation",
        concepts=_decl(
            artifact=_supported(
                m.CONCEPT_ARTIFACT, f"{_MAP}.content:map_teams_thread",
                _absent(
                    "revision",
                    "a conversation thread has no version; an edit re-renders the whole "
                    "thread rather than producing a new revision (R18-A4's refresh "
                    "model), so a revision number would be invented",
                ),
                reason=(
                    "a thread, keyed on the same thread artifact id the R18-A4 "
                    "conversation model uses, so a concept and a retrieval chunk point "
                    "at one artifact rather than two ids for one thread"
                ),
            ),
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.content:map_teams_reference",
            ),
            actor_group=_supported(
                m.CONCEPT_ACTOR_GROUP, f"{_MAP}.content:map_teams_channel",
                reason=(
                    "a Teams team/channel is a group-shaped container mapped as group_type='team', "
                    "NOT 'queue' — work is not routed to or drawn from it, and a "
                    "queue-ageing detector reading it as a queue would report backlog "
                    "that does not exist"
                ),
            ),
            work_item=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            state_transition=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
            approval=(STATUS_NOT_APPLICABLE, _NO_APPROVAL_MODEL),
            assignment=(STATUS_NOT_APPLICABLE, _NO_WORK_ITEM_MODEL),
        ),
    ),
    "aws_events": ConnectorConformance(
        connector_id="aws_events",
        source_family="cloud_events",
        concepts=_decl(
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.cloud_events:map_aws_resource_reference",
                reason=(
                    "the resource an event concerns, with entity_type='system' to "
                    "match what resource_graph.py writes for the same event — that "
                    "agreement is what lets a CMDB CI and a cloud resource be "
                    "recognised as one thing by 2.0-B2's resolution"
                ),
            ),
            work_item=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            actor_group=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            artifact=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            state_transition=(
                STATUS_NOT_APPLICABLE,
                "a resource state change is an OperationalEvent (MSP-B0), not a "
                "work-item state transition; the two must not be conflated because "
                "detectors treat them differently",
            ),
            approval=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            assignment=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
        ),
    ),
    "azure_events": ConnectorConformance(
        connector_id="azure_events",
        source_family="cloud_events",
        concepts=_decl(
            entity_reference=_supported(
                m.CONCEPT_ENTITY_REFERENCE, f"{_MAP}.cloud_events:map_azure_resource_reference",
                reason=(
                    "the resource an event concerns, with entity_type='system' to "
                    "match what resource_graph.py writes for the same event — that "
                    "agreement is what lets a CMDB CI and a cloud resource be "
                    "recognised as one thing by 2.0-B2's resolution"
                ),
            ),
            work_item=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            actor_group=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            artifact=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            state_transition=(
                STATUS_NOT_APPLICABLE,
                "a resource state change is an OperationalEvent (MSP-B0), not a "
                "work-item state transition",
            ),
            approval=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
            assignment=(STATUS_NOT_APPLICABLE, _EVENTS_NOT_WORKFLOW),
        ),
    ),
    "postgresql": ConnectorConformance(
        connector_id="postgresql",
        source_family="database",
        concepts=_decl(
            entity_reference=(
                STATUS_DECLARED,
                "a table row has a primary key that could key a reference, but which "
                "table and which column identify a real entity is per-customer scope "
                "configuration — the mapping needs that config, not just a mapper",
            ),
            work_item=(
                STATUS_GAP,
                "a native DB connector reads operational signal from tables whose "
                "schema is per-customer; whether a table holds work items cannot be "
                "known without org-specific scope configuration",
            ),
            actor_group=(STATUS_GAP, "same per-customer schema problem as work_item"),
            artifact=(STATUS_NOT_APPLICABLE, "the DB surface reads rows, not documents"),
            state_transition=(STATUS_GAP, "same per-customer schema problem as work_item"),
            approval=(STATUS_GAP, "same per-customer schema problem as work_item"),
            assignment=(STATUS_GAP, "same per-customer schema problem as work_item"),
        ),
    ),
    "sql_server": ConnectorConformance(
        connector_id="sql_server",
        source_family="database",
        concepts=_decl(
            entity_reference=(
                STATUS_DECLARED,
                "a table row has a primary key that could key a reference, but which "
                "table and which column identify a real entity is per-customer scope "
                "configuration — the mapping needs that config, not just a mapper",
            ),
            work_item=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            actor_group=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            artifact=(STATUS_NOT_APPLICABLE, "the DB surface reads rows, not documents"),
            state_transition=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            approval=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            assignment=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
        ),
    ),
    "oracle_db": ConnectorConformance(
        connector_id="oracle_db",
        source_family="database",
        concepts=_decl(
            entity_reference=(
                STATUS_DECLARED,
                "a table row has a primary key that could key a reference, but which "
                "table and which column identify a real entity is per-customer scope "
                "configuration — the mapping needs that config, not just a mapper",
            ),
            work_item=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            actor_group=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            artifact=(STATUS_NOT_APPLICABLE, "the DB surface reads rows, not documents"),
            state_transition=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            approval=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
            assignment=(STATUS_GAP, "per-customer schema; see the postgresql entry"),
        ),
    ),
}


def get_conformance(connector_id: str) -> ConnectorConformance:
    """One connector's declaration, or a named error."""
    try:
        return CONFORMANCE[connector_id]
    except KeyError:
        raise KeyError(
            f"{connector_id!r} has no conformance declaration; declared connectors "
            f"are {sorted(CONFORMANCE)}"
        ) from None


def connectors_supporting(concept: str) -> Tuple[str, ...]:
    """Connectors that actually CONFORM to a concept (``supported``, not intent).

    The read a portable detector makes: "which sources can I run against?" Returning
    ``declared`` here would let a detector run against a connector with no mapper and
    silently find nothing.
    """
    if concept not in m.CONCEPT_SET:
        raise KeyError(f"{concept!r} is not a normalised concept")
    return tuple(sorted(
        cid for cid, decl in CONFORMANCE.items()
        if decl.position(concept).conforms
    ))


def declared_gaps() -> Dict[str, Tuple[ConceptConformance, ...]]:
    """Every recorded gap, by connector — the surface B4 AC5 makes visible.

    Only ``gap`` (a shortcoming to be fixed), never ``not_applicable`` (a deliberate
    decision). Reporting both as gaps would produce a backlog full of items nobody
    intends to do, and a backlog like that gets ignored wholesale.
    """
    return {
        cid: decl.gaps for cid, decl in sorted(CONFORMANCE.items()) if decl.gaps
    }


def stale_declarations() -> Tuple[str, ...]:
    """Connectors declared against an older concept-set version.

    A declaration written before a concept existed cannot have a position on it, so
    trusting it silently would report conformance nobody assessed.
    """
    return tuple(sorted(
        cid for cid, decl in CONFORMANCE.items()
        if decl.concept_set_version != CONCEPT_SET_VERSION
    ))


def conformance_summary() -> Dict[str, Any]:
    """The whole registry, serialisable — the documentation/audit surface."""
    return {
        "concept_set_version": CONCEPT_SET_VERSION,
        "connectors": {cid: d.to_dict() for cid, d in sorted(CONFORMANCE.items())},
        "supported_by_concept": {
            c: list(connectors_supporting(c)) for c in sorted(m.CONCEPT_SET)
        },
        "gap_count": sum(len(g) for g in declared_gaps().values()),
        "stale_declarations": list(stale_declarations()),
    }


__all__ = [
    "STATUS_SUPPORTED",
    "STATUS_DECLARED",
    "STATUS_GAP",
    "STATUS_NOT_APPLICABLE",
    "STATUSES",
    "ConformanceError",
    "ConceptConformance",
    "ConnectorConformance",
    "CONFORMANCE",
    "get_conformance",
    "connectors_supporting",
    "declared_gaps",
    "stale_declarations",
    "conformance_summary",
]
