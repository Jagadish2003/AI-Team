"""Normalised-concept signal model for the primitive library — 2.0-C3 T2 (AT-837).

What a primitive reads
----------------------
A primitive never sees a connector payload. It reads :class:`ConceptRecord`s — one
normalised fact each, tagged with the normalised concept it instantiates (the
`platform_capabilities` vocabulary, which 2.0-B4 generalises). That is what makes a
manifest-authored detector portable: the author names a concept, not a connector.

Individual-free by construction, not by review
-----------------------------------------------
The constructor REFUSES a record carrying an individual-person field
(``assignee``, ``caller``, ``user_email``, …) or an email-shaped value, reusing the
denylist and sweep the operational packs already enforce at the finding boundary.
Checking only at the finding boundary would be too late in one specific way: a
partner pack could group by an individual and emit a "group" whose identity IS a
person. Refusing at admission means no primitive can ever see one.

Deterministic time, never the wall clock
-----------------------------------------
Age and window arithmetic resolve against an ``as_of`` the caller supplies (see
:meth:`SignalSet.default_as_of`, which derives it from the DATA — the latest
observed record — rather than reading the clock). A primitive that read
``datetime.now()`` would produce a different finding every day from the same
fixture, which would make the authoring harness (2.0-C3 §3) and reproducibility
both impossible.

Dependency-free of ``app``: pure data, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..cloud_ops_finding import INDIVIDUAL_FIELD_DENYLIST, find_individual_references
from ..platform_capabilities import is_concept_known

#: Source systems whose content is conversational. Corroboration from these alone
#: never lifts a finding above MEDIUM — the standing platform ceiling, applied
#: inside the primitive library so an authored pack inherits it (see contract.py).
CONVERSATION_SOURCE_SYSTEMS = frozenset({"slack", "teams"})

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class SignalError(ValueError):
    """A record cannot be admitted as normalised signal."""


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (or pass a ``datetime`` through), UTC-normalised.

    Tolerant of the shapes this repo's connectors actually produce — a trailing
    ``Z``, an explicit offset, a naive value (read as UTC), and the ServiceNow
    ``YYYY-MM-DD HH:MM:SS`` raw form. Returns ``None`` for anything unparseable,
    and callers treat unparseable time as "cannot participate in a window" rather
    than silently substituting now.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Transition:
    """One state/assignment/ownership change on a work item.

    ``participant`` is an actor GROUP or queue — never a person (enforced by the
    same admission sweep as the record itself).
    """

    kind: str
    at: Optional[datetime] = None
    participant: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat() if self.at else "",
            "participant": self.participant,
        }


@dataclass(frozen=True)
class ConceptRecord:
    """One normalised fact a primitive can read.

    ``concept``           normalised concept id this record instantiates.
    ``record_id``         stable id of the underlying source record.
    ``source_system``     the connector/system it was observed in.
    ``observed_at``       when the fact was observed (the window anchor).
    ``opened_at`` /       lifecycle timestamps, for the ageing primitive.
    ``last_state_change_at`` / ``due_at``
    ``signature``         deterministic recurrence fingerprint, when the source
                          provides one (MSP-B0/B4 signatures).
    ``actor_group``       the group/queue that holds the work.
    ``artifact``          the artifact the record is about (a queue, a document).
    ``entity_reference``  the entity/CI it touches — the concentration anchor.
    ``state``             normalised lifecycle state.
    ``metrics``           numeric measures, including ``*_baseline`` companions.
    ``transitions``       ordered transitions, for the oscillation primitive.
    ``attributes``        anything else the mapping recorded, individual-free.
    """

    concept: str
    record_id: str
    source_system: str
    observed_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    last_state_change_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    signature: str = ""
    actor_group: str = ""
    artifact: str = ""
    entity_reference: str = ""
    state: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    transitions: Tuple[Transition, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def timestamp(self, name: str) -> Optional[datetime]:
        """The named lifecycle timestamp, falling back to ``observed_at``."""
        return {
            "opened_at": self.opened_at,
            "last_state_change_at": self.last_state_change_at,
            "due_at": self.due_at,
            "observed_at": self.observed_at,
        }.get(name) or self.observed_at

    def group_key(self, group_by: str) -> str:
        """The value a primitive groups by. Never an individual — see the module docstring."""
        return {
            "signature": self.signature or self.record_id,
            "artifact": self.artifact,
            "actor_group": self.actor_group,
            "entity_reference": self.entity_reference,
        }.get(group_by, "")

    def metric(self, name: str) -> Optional[float]:
        raw = self.metrics.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)

    def to_artifact(self) -> Dict[str, Any]:
        """The source-trace artifact pointer for this record."""
        pointer: Dict[str, Any] = {
            "type": self.concept,
            "id": self.record_id,
            "source_system": self.source_system,
        }
        if self.observed_at:
            pointer["observed_at"] = self.observed_at.isoformat()
        if self.signature:
            pointer["signature"] = self.signature
        return pointer


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _assert_individual_free(payload: Any, *, where: str) -> None:
    hits = find_individual_references(payload)
    if hits:
        raise SignalError(
            f"{where} references an individual (forbidden — normalised signal "
            f"carries groups, queues, services, and entities only): {hits}"
        )


def concept_record(
    *,
    concept: str,
    record_id: str,
    source_system: str,
    observed_at: Any = None,
    opened_at: Any = None,
    last_state_change_at: Any = None,
    due_at: Any = None,
    signature: str = "",
    actor_group: str = "",
    artifact: str = "",
    entity_reference: str = "",
    state: str = "",
    metrics: Optional[Mapping[str, Any]] = None,
    transitions: Optional[Sequence[Mapping[str, Any]]] = None,
    attributes: Optional[Mapping[str, Any]] = None,
) -> ConceptRecord:
    """Admit one normalised record, refusing anything a primitive must never see.

    Raises :class:`SignalError` for an unknown concept, a missing id/system, or
    any individual-person reference in the record.
    """
    concept_id = _clean_text(concept)
    if not is_concept_known(concept_id):
        raise SignalError(
            f"{concept_id!r} is not a normalised concept this platform provides; "
            f"primitives read the declared concept vocabulary only"
        )
    identifier = _clean_text(record_id)
    system = _clean_text(source_system)
    if not identifier or not system:
        raise SignalError("a normalised record needs a record_id and a source_system")

    for name in (actor_group, artifact, entity_reference):
        if isinstance(name, str) and _EMAIL_RE.search(name):
            raise SignalError(
                "actor_group / artifact / entity_reference must name a group, queue, "
                "service, or entity — never an individual"
            )

    attribute_map = dict(attributes or {})
    for key in attribute_map:
        if str(key).lower() in INDIVIDUAL_FIELD_DENYLIST:
            raise SignalError(
                f"attribute {key!r} names an individual; normalised signal carries "
                f"groups, queues, services, and entities only"
            )
    _assert_individual_free(attribute_map, where=f"record {identifier!r}")

    numeric: Dict[str, float] = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        numeric[str(key)] = float(value)

    parsed_transitions: List[Transition] = []
    for entry in transitions or []:
        if not isinstance(entry, Mapping):
            continue
        participant = _clean_text(entry.get("participant"))
        if participant and _EMAIL_RE.search(participant):
            raise SignalError(
                "a transition participant must be an actor group or queue, never an "
                "individual"
            )
        parsed_transitions.append(
            Transition(
                kind=_clean_text(entry.get("kind")) or "assignment",
                at=parse_timestamp(entry.get("at")),
                participant=participant,
            )
        )

    return ConceptRecord(
        concept=concept_id,
        record_id=identifier,
        source_system=system,
        observed_at=parse_timestamp(observed_at),
        opened_at=parse_timestamp(opened_at),
        last_state_change_at=parse_timestamp(last_state_change_at),
        due_at=parse_timestamp(due_at),
        signature=_clean_text(signature),
        actor_group=_clean_text(actor_group),
        artifact=_clean_text(artifact),
        entity_reference=_clean_text(entity_reference),
        state=_clean_text(state).lower(),
        metrics=numeric,
        transitions=tuple(parsed_transitions),
        attributes=attribute_map,
    )


@dataclass(frozen=True)
class SignalSet:
    """The normalised signal one detector run reads.

    ``dependency_edges`` maps an entity reference to the entities it depends on —
    the graph the concentration primitive traverses (MSP-B3's dependency edges in
    the running platform; a plain mapping in a fixture). Supplying none simply
    means concentration can only see direct references, which is an honest
    degradation rather than an error.
    """

    records: Tuple[ConceptRecord, ...] = ()
    dependency_edges: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def for_concept(self, concept: str) -> List[ConceptRecord]:
        """Records instantiating one concept, in deterministic order."""
        return [record for record in self.records if record.concept == concept]

    def source_systems(self) -> List[str]:
        seen: List[str] = []
        for record in self.records:
            if record.source_system not in seen:
                seen.append(record.source_system)
        return seen

    def default_as_of(self) -> Optional[datetime]:
        """The latest observed instant in the DATA — never the wall clock.

        Deterministic: the same fixture always produces the same evaluation
        instant, so a seeded manifest test cannot start failing on a date nobody
        chose.
        """
        stamps = [
            stamp
            for record in self.records
            for stamp in (
                record.observed_at,
                record.opened_at,
                record.last_state_change_at,
            )
            if stamp is not None
        ]
        return max(stamps) if stamps else None

    def dependents_of(self, entity: str, *, max_depth: int) -> List[str]:
        """Entities reaching ``entity`` within ``max_depth`` hops, deterministically.

        Depth-bounded by contract: the primitive's ``max_depth`` parameter caps at
        3 in the manifest schema, and this traversal never exceeds what it is
        given. Cycles terminate — a visited entity is never re-expanded.
        """
        if max_depth < 1:
            return []
        reverse: Dict[str, List[str]] = {}
        for dependent, dependencies in self.dependency_edges.items():
            for dependency in dependencies:
                reverse.setdefault(dependency, []).append(dependent)

        found: List[str] = []
        visited = {entity}
        frontier = [entity]
        for _ in range(max_depth):
            next_frontier: List[str] = []
            for node in frontier:
                for dependent in sorted(reverse.get(node, [])):
                    if dependent in visited:
                        continue
                    visited.add(dependent)
                    found.append(dependent)
                    next_frontier.append(dependent)
            if not next_frontier:
                break
            frontier = next_frontier
        return found


def signal_set(
    records: Iterable[ConceptRecord],
    *,
    dependency_edges: Optional[Mapping[str, Sequence[str]]] = None,
) -> SignalSet:
    """Build a :class:`SignalSet`, ordered deterministically.

    Ordering is by ``(observed instant, record id)`` so a primitive's output never
    depends on the order a fixture happened to list its records in.
    """
    ordered = sorted(
        records,
        key=lambda record: (
            record.observed_at.timestamp() if record.observed_at else float("-inf"),
            record.record_id,
        ),
    )
    edges = {
        str(key): tuple(str(item) for item in value)
        for key, value in (dependency_edges or {}).items()
    }
    return SignalSet(records=tuple(ordered), dependency_edges=edges)


def records_from_dicts(entries: Iterable[Mapping[str, Any]]) -> List[ConceptRecord]:
    """Admit a list of plain dicts — the shape a fixture (or the authoring
    harness) supplies. Every entry goes through :func:`concept_record`, so fixture
    data is held to the identical admission rules as live signal."""
    return [concept_record(**dict(entry)) for entry in entries]


def signal_set_from_dicts(document: Mapping[str, Any]) -> SignalSet:
    """Build a signal set from ``{"records": [...], "dependencyEdges": {...}}``."""
    return signal_set(
        records_from_dicts(document.get("records") or []),
        dependency_edges=document.get("dependencyEdges") or {},
    )


__all__ = [
    "CONVERSATION_SOURCE_SYSTEMS",
    "ConceptRecord",
    "SignalError",
    "SignalSet",
    "Transition",
    "concept_record",
    "parse_timestamp",
    "records_from_dicts",
    "signal_set",
    "signal_set_from_dicts",
]
