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

**Nothing here is `supported` yet, and that is the honest state at T1.** This ticket
defines the concept set, the versioned contracts and this declaration mechanism; the
mappers are T2/T3. Recording `declared` now is what makes the remaining work visible
instead of implied — and the moment a mapper lands, flipping one status to
`supported` is the whole change.

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
    from discovery.concepts.contracts import CONCEPT_SET_VERSION
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.contracts import CONCEPT_SET_VERSION


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


class ConformanceError(ValueError):
    """A conformance declaration is malformed or claims more than it can."""


@dataclass(frozen=True)
class ConceptConformance:
    """One connector's position on one concept."""

    concept: str
    status: str
    reason: str = ""
    #: Where the mapping lives, when it exists. Recorded so a `supported` claim
    #: points at something a reviewer can read.
    mapper: Optional[str] = None

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
    """Build a full concept-set declaration from ``concept=(status, reason, mapper)``.

    Every concept must be named; :class:`ConnectorConformance` refuses a partial
    declaration, so a new concept added to the set breaks every registry entry loudly
    rather than defaulting a dozen connectors to a position nobody chose.
    """
    positions = []
    for concept, spec in by_concept.items():
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
            work_item=STATUS_DECLARED,
            actor_group=STATUS_DECLARED,
            state_transition=STATUS_DECLARED,
            approval=STATUS_DECLARED,
            assignment=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
            artifact=STATUS_DECLARED,
        ),
    ),
    "jira": ConnectorConformance(
        connector_id="jira",
        source_family="engineering_tracker",
        concepts=_decl(
            work_item=STATUS_DECLARED,
            actor_group=(
                STATUS_GAP,
                "Jira assigns to individuals; project roles and components are the "
                "closest group-shaped concepts and neither is a work queue. Mapping "
                "an assignee to a group would fabricate one.",
            ),
            state_transition=STATUS_DECLARED,
            approval=(
                STATUS_GAP,
                "approval is a workflow-transition convention per project, not a "
                "first-class Jira record; reading one reliably needs per-org "
                "configuration this connector does not have",
            ),
            assignment=STATUS_DECLARED,
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
        ),
    ),
    "salesforce": ConnectorConformance(
        connector_id="salesforce",
        source_family="crm",
        concepts=_decl(
            work_item=STATUS_DECLARED,
            actor_group=STATUS_DECLARED,
            state_transition=STATUS_DECLARED,
            approval=STATUS_DECLARED,
            assignment=STATUS_DECLARED,
            artifact=(
                STATUS_NOT_APPLICABLE,
                "the Salesforce ingest surface reads records, not files; attachments "
                "reach the platform through the document path instead",
            ),
            entity_reference=STATUS_DECLARED,
        ),
    ),
    "github": ConnectorConformance(
        connector_id="github",
        source_family="code",
        concepts=_decl(
            work_item=STATUS_DECLARED,
            actor_group=(
                STATUS_GAP,
                "teams exist in GitHub but the ingest surface reads repository and PR "
                "activity, not org team membership",
            ),
            state_transition=STATUS_DECLARED,
            approval=STATUS_DECLARED,
            assignment=(
                STATUS_GAP,
                "PR review requests are per-person; there is no group work queue to "
                "assign to",
            ),
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
        ),
    ),
    "confluence": ConnectorConformance(
        connector_id="confluence",
        source_family="documentation",
        concepts=_decl(
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
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
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
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
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
            actor_group=(
                STATUS_DECLARED,
                "a channel is a group-shaped container ('team' type), NOT a work "
                "queue — a detector must not read queue semantics into it",
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
            artifact=STATUS_DECLARED,
            entity_reference=STATUS_DECLARED,
            actor_group=(
                STATUS_DECLARED,
                "a team/channel is a group-shaped container, NOT a work queue — a "
                "detector must not read queue semantics into it",
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
            entity_reference=STATUS_DECLARED,
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
            entity_reference=STATUS_DECLARED,
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
            entity_reference=STATUS_DECLARED,
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
            entity_reference=STATUS_DECLARED,
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
            entity_reference=STATUS_DECLARED,
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
