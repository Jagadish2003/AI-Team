"""
cross_source_resolution.py — Release 2.0-B2 T1: the ranked resolution engine.

The knowledge graph holds entities from individual sources. The same service,
application, team, or customer therefore appears in ServiceNow, Jira, Salesforce,
code, and cloud events as several unrelated entities — which caps how much
cross-system corroboration is honestly possible (a finding "corroborated across
ServiceNow and Jira" is only meaningful if both sides are about the same thing).

This module decides, for one entity, whether another entity IS the same real
thing — and it decides it in three tiers RANKED BY THE STRENGTH OF THE EVIDENCE:

  1. :data:`TIER_EXPLICIT_REFERENCE` — the source data itself says so: a CI
     reference, an external id, an integration key pointing at the other entity's
     own ``(source_system, source_record_id)`` identity, or both entities citing
     the SAME third-party identity. Machine-stated fact → **auto-merge**.
  2. :data:`TIER_ALIAS_MAPPING` — the org's Owner-managed alias table
     (:mod:`app.entity_alias_mappings`) says so. Human-stated fact, recorded and
     reversible → **auto-merge**.
  3. :data:`TIER_NAME_SIMILARITY` — the two entities merely share a name (exact
     canonical equality) and a corroborating observed relationship. Evidence of
     *possible* identity, not of identity → **propose only, never merge**.

**Why tier 3 can never merge, structurally.** A wrongly merged entity corrupts
every finding built on it, and the corruption is invisible — the worst failure
mode in the platform. So the merge boundary is not a convention or a config
default: :data:`AUTO_MERGE_TIERS` is a frozenset that excludes
:data:`TIER_NAME_SIMILARITY`, :func:`action_for_tier` is the only place an action
is decided, :class:`ResolutionPolicy` carries no field that can move a tier
across that boundary, and there is deliberately no env var and no ``force``
parameter anywhere in this module. A test asserts that no combination of policy
values ever produces a merge from a name match.

Four gates apply before any tier is consulted, each of which exists to stop a
class of wrong merge outright:

  * **org** — a candidate from another org is dropped and counted, never matched
    (cross-tenant identity leakage would be both wrong and a breach);
  * **entity type** — a ``team`` named "Payments" is never the ``system`` named
    "Payments";
  * **self** — an entity never resolves to itself;
  * **status** — only a ``resolved`` candidate is a merge target. An
    ``ambiguous`` row is the standing engine's recorded uncertainty
    (:mod:`app.entity_resolution`); resolving onto it would launder that
    uncertainty into a confident merge.

And two rules make the outcome trustworthy rather than merely available:

  * **Ambiguity never merges.** Two or more distinct targets at a tier →
    ``ambiguous``, recorded with every candidate so a human can see exactly what
    collided (the N+1 discipline the standing engine already applies). An
    ambiguous tier STOPS resolution — it does not fall through to a weaker tier,
    because "the explicit references disagree" is a data problem to fix at the
    source, not licence to merge on a weaker signal.
  * **Determinism.** Candidates are considered in a stable sorted order and every
    tie-break is explicit, so the same inputs always produce the same decision —
    which is what makes a merge reviewable and a proposal reproducible.

Layering mirrors :mod:`app.trace_graph`: the engine (everything above
"DB-backed loaders") is PURE — no DB, no writes, no clock — and the loaders at the
bottom are the convenience wrapper that feeds it from the entities/relationships
tables. Nothing here writes: T1 DECIDES. Persisting a merge with its provenance,
the proposal review workflow, unmerge, and the corroboration uplift are the
later 2.0-B2 tasks that consume these decisions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from database.models.entities import Entity

from .entity_alias_mappings import AliasIndex, AliasMapping, build_alias_index
from .entity_resolution import canonical_name_for

logger = logging.getLogger(__name__)

# ── Tiers, ranked strongest first ───────────────────────────────────────────

TIER_EXPLICIT_REFERENCE = "explicit_reference"
TIER_ALIAS_MAPPING = "alias_mapping"
TIER_NAME_SIMILARITY = "name_similarity"

#: Tier 4 — a LEADING-WORD name match ("Payment Operations" vs "Payment
#: Escalations"). Deliberately its OWN tier rather than a loosening of tier 3,
#: and OFF unless a deployment opts in via ``ResolutionPolicy.name_prefix_words``.
#:
#: Why a separate tier. Tier 3's exactness is depended on elsewhere — most
#: importantly ``app.corroboration_identity_gate``, which consults a tier-3
#: re-derivation before letting a finding reach HIGH. Widening tier 3 would let a
#: finding be elevated on a shared first word, which is a confidence claim the
#: evidence cannot support. Keeping this separate means every existing consumer of
#: tier 3 keeps the guarantee it was written against.
#:
#: Why it exists at all. Some deployments cannot align names across systems: an
#: operational ServiceNow assignment group and a Salesforce queue may be
#: differently named by real organisational history, and renaming either is not
#: available to the people who need the match reviewed. For those cases this
#: proposes the pair for a HUMAN to judge — never merges it.
#:
#: The cost is real and is why it ships disabled: leading-word matching is
#: combinatorial. Measured on one real org, the word "case" appears in 61 entity
#: names (1,830 candidate pairs) and "test" in 60 (1,770). A review queue nobody
#: can finish is a review queue nobody uses, so the corroborating-relationship
#: requirement and ``max_proposals`` cap both still apply, undiminished.
TIER_NAME_PREFIX = "name_prefix_similarity"

#: Rank of each tier — LOWER is stronger. The engine consults tiers in this
#: order and the first one to produce a decision wins; a weaker tier can never
#: override a stronger one's answer.
TIER_RANK: Dict[str, int] = {
    TIER_EXPLICIT_REFERENCE: 1,
    TIER_ALIAS_MAPPING: 2,
    TIER_NAME_SIMILARITY: 3,
    TIER_NAME_PREFIX: 4,
}

TIERS_BY_RANK: Tuple[str, ...] = tuple(sorted(TIER_RANK, key=lambda t: TIER_RANK[t]))

#: The ONLY tiers permitted to merge without a human. Name similarity is absent
#: by design — see the module docstring. Do not add to this set: a new tier that
#: needs to auto-merge must be a machine- or human-STATED identity, not a
#: similarity signal.
AUTO_MERGE_TIERS: FrozenSet[str] = frozenset({
    TIER_EXPLICIT_REFERENCE,
    TIER_ALIAS_MAPPING,
})

# Actions the engine can return for a match.
ACTION_MERGE = "merge"      # auto-merge: the evidence STATES identity
ACTION_PROPOSE = "propose"  # surface for human confirmation; never applied
ACTION_NONE = "none"

# Decision statuses. Deliberately the same vocabulary as the entities table's
# resolution_status where they overlap, so a downstream writer can store one.
STATUS_RESOLVED = "resolved"      # exactly one auto-merge target
STATUS_PROPOSED = "proposed"      # candidate(s) surfaced for confirmation only
STATUS_AMBIGUOUS = "ambiguous"    # 2+ targets at a tier — recorded, never merged
STATUS_UNRESOLVED = "unresolved"  # nothing matched

# Confidence per tier. Mirrors app.entity_resolution's tiers: a stable machine id
# is 1.0, a name-based match is 0.8 there — a cross-source name match is weaker
# still (it spans systems that may legitimately reuse a word), hence 0.7.
CONFIDENCE_EXPLICIT_REFERENCE = 1.0
CONFIDENCE_ALIAS_MAPPING = 0.95
CONFIDENCE_NAME_SIMILARITY = 0.7
#: Tier 4. Strictly below tier 3: a shared leading word is weaker evidence than a
#: whole matching name, and the number a reviewer sees should say so.
CONFIDENCE_NAME_PREFIX = 0.5
CONFIDENCE_AMBIGUOUS = 0.6
CONFIDENCE_NONE = 0.0

# Reasons a name-tier candidate was NOT proposed, recorded so the engine's
# silence is explainable rather than mysterious.
REASON_NO_CORROBORATION = "name matched but no corroborating observed relationship"
REASON_SAME_SOURCE = "name matched within the same source system"

# ── Cross-reference metadata convention ─────────────────────────────────────
#
# Tier 1 needs the source data's OWN statement of identity. An entity carries its
# identity in (source_system, source_record_id); it carries references to OTHER
# systems' records in its metadata, and this is the documented convention for
# where. Nothing is guessed: an unrecognised metadata key is ignored.

#: Preferred, first-class form — a list a producer populates explicitly:
#: ``metadata["cross_references"] = [{"system": "jira", "record_id": "PROJ-1",
#: "field": "correlation_id"}, ...]``
METADATA_CROSS_REFERENCES = "cross_references"

#: Convenience map form: ``metadata["external_ids"] = {"jira": "PROJ-1"}``.
METADATA_EXTERNAL_IDS = "external_ids"

#: Single-field keys whose NAME states the target system unambiguously. This is an
#: explicit enumeration, not a heuristic — a key not listed here is never read as
#: a cross-reference, however reference-like it looks. (``correlation_id`` is
#: deliberately absent: it is a free-form field whose target system is configured
#: per org, so a producer must publish it through ``cross_references`` with the
#: system named.)
KNOWN_CROSS_REFERENCE_KEYS: Dict[str, str] = {
    "jira_issue_key": "jira",
    "jira_key": "jira",
    "ci_sys_id": "servicenow",
    "cmdb_ci_sys_id": "servicenow",
    "servicenow_sys_id": "servicenow",
    "salesforce_id": "salesforce",
    "github_repo_id": "github",
    "repo_id": "git",
}


@dataclass(frozen=True)
class ExternalRef:
    """One explicit reference from an entity to a record in another system.

    ``system`` and ``record_id`` are the referenced record's identity — the same
    pair an entity carries as ``(source_system, source_record_id)``, which is what
    makes the match exact rather than inferred. ``field`` records WHERE the
    reference was read from, so a merge can name its evidence.
    """

    system: str
    record_id: str
    field_name: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.system, self.record_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "system": self.system,
            "record_id": self.record_id,
            "field": self.field_name,
        }


@dataclass(frozen=True)
class ResolutionEntity:
    """The neutral view of one graph entity the resolver reasons about.

    Built from an ``entities`` row via :func:`resolution_entity_from_entity`, or
    directly in tests. ``canonical_name`` is always the shared canonicalisation
    (:func:`app.entity_resolution.canonical_name_for`) so this layer and the
    standing engine cannot disagree about what a name is.
    """

    entity_id: str
    org_id: str
    entity_type: str
    display_name: str
    canonical_name: str
    source_system: str
    source_record_id: Optional[str] = None
    resolution_status: str = STATUS_RESOLVED
    cross_references: Tuple[ExternalRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def identity_key(self) -> Optional[Tuple[str, str]]:
        """This entity's own ``(source_system, source_record_id)``, when it has a
        stable source id. ``None`` for a name-derived entity — which is precisely
        why such an entity can never be a tier-1 target."""
        if not self.source_system or not self.source_record_id:
            return None
        return (self.source_system, str(self.source_record_id))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "org_id": self.org_id,
            "entity_type": self.entity_type,
            "display_name": self.display_name,
            "canonical_name": self.canonical_name,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "resolution_status": self.resolution_status,
            "cross_references": [r.to_dict() for r in self.cross_references],
        }


@dataclass(frozen=True)
class ResolutionMatch:
    """One candidate the engine matched, with the tier that matched it.

    ``action`` is derived from the tier by :func:`action_for_tier` — it is never
    passed in, so a caller cannot construct a merge out of a name match.
    """

    target: ResolutionEntity
    tier: str
    action: str
    confidence: float
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_merge(self) -> bool:
        return self.action == ACTION_MERGE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "tier": self.tier,
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ResolutionDecision:
    """The engine's answer for ONE subject entity.

    ``merge_target`` is populated only for :data:`STATUS_RESOLVED` — i.e. only
    from an auto-merge tier with exactly one target. ``proposals`` are tier-3
    candidates for human confirmation and are NEVER applied by anything reading
    this object. ``considered`` records what was looked at (and what a gate
    dropped) so a decision — including a decision to do nothing — is auditable.
    """

    subject: ResolutionEntity
    status: str
    tier: Optional[str] = None
    confidence: float = CONFIDENCE_NONE
    merge_target: Optional[ResolutionEntity] = None
    matches: Tuple[ResolutionMatch, ...] = ()
    proposals: Tuple[ResolutionMatch, ...] = ()
    reason: str = ""
    considered: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_merge(self) -> bool:
        """True only when the decision authorises an automatic merge."""
        return self.status == STATUS_RESOLVED and self.merge_target is not None

    @property
    def has_proposals(self) -> bool:
        return bool(self.proposals)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject.to_dict(),
            "status": self.status,
            "tier": self.tier,
            "confidence": self.confidence,
            "merge_target": self.merge_target.to_dict() if self.merge_target else None,
            "matches": [m.to_dict() for m in self.matches],
            "proposals": [p.to_dict() for p in self.proposals],
            "reason": self.reason,
            "considered": dict(self.considered),
        }


@dataclass(frozen=True)
class ResolutionPolicy:
    """Knobs for how CONSERVATIVE the engine is — never for what may merge.

    Every field here can only make the engine propose LESS or consider fewer
    candidates. There is deliberately no field (and no env var) that can move a
    tier across the auto-merge boundary; see :data:`AUTO_MERGE_TIERS`.

    ``require_corroborating_relationship`` implements the story's tier-3 signal
    definition — "exact normalised name + corroborating relationship". Without it
    every reused word ("admin", "billing", "core") in two systems would become a
    proposal, and a review queue nobody can finish is a review queue nobody uses.
    ``require_cross_source_for_name_tier`` keeps tier 3 to its actual job: two
    same-named entities from the SAME source are already the standing engine's
    business (:mod:`app.entity_resolution` resolves or marks them ambiguous), so
    re-proposing them here would be noise.
    """

    require_corroborating_relationship: bool = True
    require_cross_source_for_name_tier: bool = True
    max_proposals: int = 10
    #: Tier 4 opt-in: how many LEADING words of the canonical name must match for
    #: a pair to be PROPOSED. ``0`` (the default) disables the tier entirely, so
    #: every existing deployment behaves exactly as before.
    #:
    #: This field can only make the engine propose MORE, which is why it is the
    #: one exception to this class's "only ever more conservative" rule — and why
    #: it is stated here rather than hidden. It still cannot move anything across
    #: the auto-merge boundary: :data:`TIER_NAME_PREFIX` is absent from
    #: :data:`AUTO_MERGE_TIERS`, so :func:`action_for_tier` can only ever return
    #: ``ACTION_PROPOSE`` for it, and ``entity_merge._RULE_FOR_TIER`` has no rule
    #: for it either. Two independent gates, neither reachable from config.
    #:
    #: 1 matches on the first word alone — the loosest setting, and the one that
    #: floods a queue fastest. Prefer 2 where the names allow it.
    name_prefix_words: int = 0
    #: Tier 4 only: require the shared observed neighbour. SEPARATE from
    #: ``require_corroborating_relationship`` so tier 3 keeps its own requirement
    #: untouched whatever tier 4 is set to.
    #:
    #: Turning this OFF removes the only evidence tier 4 has beyond the words
    #: themselves, so every pair sharing a leading word becomes a proposal. On one
    #: measured org that is ~3,600 proposals from the words "case" and "test"
    #: alone. ``max_proposals`` becomes the only thing standing between a reviewer
    #: and an unusable queue — set it deliberately if you disable this.
    name_prefix_require_corroboration: bool = True


DEFAULT_POLICY = ResolutionPolicy()


def action_for_tier(tier: str) -> str:
    """The ONLY place a tier's action is decided.

    An auto-merge tier merges; anything else — including any tier added later
    that is not deliberately added to :data:`AUTO_MERGE_TIERS` — can at most be
    proposed. Fail-closed by construction: the default is the weaker action.
    """
    return ACTION_MERGE if tier in AUTO_MERGE_TIERS else ACTION_PROPOSE


def confidence_for_tier(tier: str) -> float:
    return {
        TIER_EXPLICIT_REFERENCE: CONFIDENCE_EXPLICIT_REFERENCE,
        TIER_ALIAS_MAPPING: CONFIDENCE_ALIAS_MAPPING,
        TIER_NAME_SIMILARITY: CONFIDENCE_NAME_SIMILARITY,
        TIER_NAME_PREFIX: CONFIDENCE_NAME_PREFIX,
    }.get(tier, CONFIDENCE_NONE)


# ── Building the resolver's view of an entity ───────────────────────────────


def _text(value: Any) -> str:
    return str(value or "").strip()


def extract_cross_references(
    metadata: Optional[Mapping[str, Any]],
    *,
    own_system: str = "",
) -> Tuple[ExternalRef, ...]:
    """Read an entity's EXPLICIT cross-references out of its metadata.

    Reads the three documented forms only (see the convention block above):
    ``cross_references`` (preferred), ``external_ids``, and the enumerated
    single-field keys. An unrecognised key is ignored — a reference is never
    inferred from a field name that merely looks like an id, because a wrong
    tier-1 match auto-merges.

    A self-reference (a reference to the entity's own system) is dropped: it
    carries no cross-source information and would let an entity match every
    sibling from its own source on a shared field value.
    """
    if not isinstance(metadata, Mapping):
        return ()

    own = _text(own_system).lower()
    seen: set = set()
    refs: List[ExternalRef] = []

    def _add(system: Any, record_id: Any, field_name: str) -> None:
        sys_name = _text(system).lower()
        rec = _text(record_id)
        if not sys_name or not rec or sys_name == own:
            return
        key = (sys_name, rec)
        if key in seen:
            return
        seen.add(key)
        refs.append(ExternalRef(system=sys_name, record_id=rec, field_name=field_name))

    declared = metadata.get(METADATA_CROSS_REFERENCES)
    if isinstance(declared, Iterable) and not isinstance(declared, (str, bytes, Mapping)):
        for entry in declared:
            if isinstance(entry, Mapping):
                _add(
                    entry.get("system") or entry.get("source_system"),
                    entry.get("record_id") or entry.get("id") or entry.get("source_record_id"),
                    _text(entry.get("field")) or METADATA_CROSS_REFERENCES,
                )

    external_ids = metadata.get(METADATA_EXTERNAL_IDS)
    if isinstance(external_ids, Mapping):
        for system, record_id in external_ids.items():
            _add(system, record_id, METADATA_EXTERNAL_IDS)

    for key, system in KNOWN_CROSS_REFERENCE_KEYS.items():
        if key in metadata:
            _add(system, metadata.get(key), key)

    # Deterministic order so a decision's evidence is reproducible.
    return tuple(sorted(refs, key=lambda r: (r.system, r.record_id, r.field_name)))


def resolution_entity_from_entity(entity: Entity) -> ResolutionEntity:
    """Build the resolver's view of a persisted ``entities`` row."""
    metadata = entity.metadata if isinstance(entity.metadata, dict) else {}
    return ResolutionEntity(
        entity_id=str(entity.id),
        org_id=entity.org_id,
        entity_type=entity.entity_type,
        display_name=entity.display_name,
        canonical_name=canonical_name_for(entity.display_name or entity.canonical_name),
        source_system=entity.source_system,
        source_record_id=entity.source_record_id,
        resolution_status=entity.resolution_status,
        cross_references=extract_cross_references(
            metadata, own_system=entity.source_system
        ),
        metadata=metadata,
    )


def resolution_entities_from_entities(
    entities: Iterable[Entity],
) -> List[ResolutionEntity]:
    return [resolution_entity_from_entity(e) for e in entities]


# ── Relationship corroboration (tier 3's second requirement) ────────────────


@dataclass(frozen=True)
class RelationshipIndex:
    """OBSERVED graph neighbours per entity id, for corroboration only.

    A tier-3 proposal requires the two same-named entities to share a
    corroborating relationship: each has an observed edge to a COMMON third
    entity ("both are members of the same team", "both depend on the same CI").
    That is real, checkable evidence that the two names describe one thing in one
    estate, as opposed to a coincidence of vocabulary.

    INFERRED edges are deliberately excluded when the index is built from the
    graph (see :func:`load_relationship_index`): an inferred edge is a co-firing
    hypothesis, and corroborating a proposal with a hypothesis would stack a guess
    on a guess. The neighbour key includes the relationship type, so "member_of
    team X" does not corroborate against "owns team X".
    """

    neighbours: Mapping[str, FrozenSet[Tuple[str, str]]] = field(default_factory=dict)

    def for_entity(self, entity_id: str) -> FrozenSet[Tuple[str, str]]:
        return self.neighbours.get(str(entity_id), frozenset())

    def shared(self, left_id: str, right_id: str) -> Tuple[Tuple[str, str], ...]:
        """The observed neighbours both entities have in common (sorted)."""
        common = self.for_entity(left_id) & self.for_entity(right_id)
        return tuple(sorted(common))

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.neighbours)


EMPTY_RELATIONSHIP_INDEX = RelationshipIndex()


def build_relationship_index(
    edges: Iterable[Mapping[str, Any]],
    *,
    include_inferred: bool = False,
) -> RelationshipIndex:
    """Index ``{from_entity_id, to_entity_id, relationship_type, inferred}`` rows.

    Edges are treated as undirected for corroboration purposes (sharing a
    neighbour is symmetric), but the relationship TYPE is part of the neighbour
    key so two different kinds of link to the same third entity do not corroborate
    each other.
    """
    neighbours: Dict[str, set] = {}
    for edge in edges or ():
        if not isinstance(edge, Mapping):
            continue
        if not include_inferred and bool(edge.get("inferred")):
            continue
        left = _text(edge.get("from_entity_id"))
        right = _text(edge.get("to_entity_id"))
        rel_type = _text(edge.get("relationship_type"))
        if not left or not right or not rel_type or left == right:
            continue
        neighbours.setdefault(left, set()).add((rel_type, right))
        neighbours.setdefault(right, set()).add((rel_type, left))
    return RelationshipIndex(
        neighbours={k: frozenset(v) for k, v in neighbours.items()}
    )


# ── The ranked engine ───────────────────────────────────────────────────────


def _eligible_candidates(
    subject: ResolutionEntity, candidates: Iterable[ResolutionEntity]
) -> Tuple[List[ResolutionEntity], Dict[str, int]]:
    """Apply the four gates, returning survivors plus a count per rejection.

    The counts are reported on the decision so "nothing matched" can be told apart
    from "everything was gated out, and by which gate".
    """
    dropped = {"cross_org": 0, "type_mismatch": 0, "self": 0, "not_resolved": 0}
    survivors: List[ResolutionEntity] = []
    for candidate in candidates or ():
        if candidate.entity_id and candidate.entity_id == subject.entity_id:
            dropped["self"] += 1
            continue
        if candidate.org_id != subject.org_id:
            # Hard tenancy gate. Counted (never silently ignored) because a
            # cross-org candidate reaching this engine is itself a caller bug.
            dropped["cross_org"] += 1
            continue
        if candidate.entity_type != subject.entity_type:
            dropped["type_mismatch"] += 1
            continue
        if candidate.resolution_status != STATUS_RESOLVED:
            dropped["not_resolved"] += 1
            continue
        survivors.append(candidate)
    survivors.sort(key=lambda c: (c.canonical_name, c.source_system, c.entity_id))
    return survivors, dropped


def _explicit_reference_matches(
    subject: ResolutionEntity, candidates: Sequence[ResolutionEntity]
) -> List[ResolutionMatch]:
    """Tier 1 — the source data states the identity.

    Three shapes, all exact and all read from explicit fields:

      * ``subject_references_candidate`` — the subject cites the candidate's own
        ``(source_system, source_record_id)``;
      * ``candidate_references_subject`` — the reverse;
      * ``shared_external_reference`` — both cite the SAME third-party record,
        which is neither entity's own identity (e.g. both a Jira project entity
        and a ServiceNow group entity carry the same CMDB CI sys_id).
    """
    subject_refs = {r.key: r for r in subject.cross_references}
    subject_identity = subject.identity_key
    matches: List[ResolutionMatch] = []

    for candidate in candidates:
        evidence: Dict[str, Any] = {}
        kind = ""

        candidate_identity = candidate.identity_key
        if candidate_identity is not None and candidate_identity in subject_refs:
            ref = subject_refs[candidate_identity]
            kind = "subject_references_candidate"
            evidence = {
                "reference": ref.to_dict(),
                "matched_identity": {
                    "system": candidate.source_system,
                    "record_id": candidate.source_record_id,
                },
            }
        else:
            candidate_refs = {r.key: r for r in candidate.cross_references}
            if subject_identity is not None and subject_identity in candidate_refs:
                ref = candidate_refs[subject_identity]
                kind = "candidate_references_subject"
                evidence = {
                    "reference": ref.to_dict(),
                    "matched_identity": {
                        "system": subject.source_system,
                        "record_id": subject.source_record_id,
                    },
                }
            else:
                shared = sorted(set(subject_refs) & set(candidate_refs))
                # A shared reference to one of the two entities' OWN identities is
                # already covered above; anything left is a genuine third-party
                # record both sides cite.
                shared = [
                    key for key in shared
                    if key != subject_identity and key != candidate_identity
                ]
                if shared:
                    kind = "shared_external_reference"
                    evidence = {
                        "shared_references": [
                            {"system": s, "record_id": r} for s, r in shared
                        ]
                    }

        if not kind:
            continue

        evidence["match_kind"] = kind
        matches.append(
            ResolutionMatch(
                target=candidate,
                tier=TIER_EXPLICIT_REFERENCE,
                action=action_for_tier(TIER_EXPLICIT_REFERENCE),
                confidence=confidence_for_tier(TIER_EXPLICIT_REFERENCE),
                reason=f"explicit cross-reference ({kind})",
                evidence=evidence,
            )
        )
    return matches


def _alias_matches(
    subject: ResolutionEntity,
    candidates: Sequence[ResolutionEntity],
    alias_index: AliasIndex,
) -> List[ResolutionMatch]:
    """Tier 2 — the org's alias table states the identity.

    Both entities' canonical names must fall in the SAME alias group of the SAME
    entity type. The group is named on the match evidence so a merge can be traced
    to the human assertion (and to whoever recorded it) that authorised it.
    """
    if len(alias_index) == 0:
        return []
    group: Optional[AliasMapping] = alias_index.group_for(
        subject.entity_type, subject.canonical_name
    )
    if group is None:
        return []

    matches: List[ResolutionMatch] = []
    for candidate in candidates:
        candidate_group = alias_index.group_for(
            candidate.entity_type, candidate.canonical_name
        )
        if candidate_group is None or candidate_group.group_id != group.group_id:
            continue
        if candidate.canonical_name == subject.canonical_name:
            # Same name in the same group is not what the alias table asserts —
            # it asserts that DIFFERENT names mean one thing. An identical name is
            # the standing engine's business (or tier 3's, cross-source).
            continue
        matches.append(
            ResolutionMatch(
                target=candidate,
                tier=TIER_ALIAS_MAPPING,
                action=action_for_tier(TIER_ALIAS_MAPPING),
                confidence=confidence_for_tier(TIER_ALIAS_MAPPING),
                reason=f"org-configured alias mapping '{group.group_id}'",
                evidence={
                    "alias_group": group.group_id,
                    "canonical": group.canonical,
                    "subject_alias": subject.canonical_name,
                    "target_alias": candidate.canonical_name,
                    "note": group.note,
                    "created_by": group.created_by,
                },
            )
        )
    return matches


def _leading_words(canonical_name: str, count: int) -> str:
    """The first ``count`` words of a canonical name, or "" when there are fewer.

    A name SHORTER than the required prefix yields "" and therefore never matches:
    with ``count=2``, "payment" cannot be said to agree with "payment operations"
    on two words, and treating it as agreement would be inventing a match.

    A name of EXACTLY ``count`` words does match — so with ``count=1`` an entity
    called "Payment" is a candidate against "Payment Operations". That is
    deliberate: it is a real question a reviewer might answer yes to, and
    suppressing it would be an arbitrary carve-out. The volume it invites is held
    down by the corroborating-relationship requirement and ``max_proposals``, not
    by refusing to ask.
    """
    words = canonical_name.split()
    if count <= 0 or len(words) < count:
        return ""
    return " ".join(words[:count])


def _name_prefix_matches(
    subject: ResolutionEntity,
    candidates: Sequence[ResolutionEntity],
    relationship_index: RelationshipIndex,
    policy: ResolutionPolicy,
) -> Tuple[List[ResolutionMatch], List[Dict[str, Any]]]:
    """Tier 4 — LEADING-WORD name match, PROPOSED only, opt-in.

    Runs only when ``policy.name_prefix_words > 0``. Every guard tier 3 applies
    still applies here — cross-source, corroborating observed relationship,
    proposal-only — because the looser the name rule, the more the other
    constraints are doing to keep the queue answerable.

    Pairs whose names match EXACTLY are skipped: they are tier 3's business and
    would already have returned a decision before this tier is consulted.
    """
    words = policy.name_prefix_words
    if words <= 0:
        return [], []

    subject_prefix = _leading_words(subject.canonical_name, words)
    if not subject_prefix:
        return [], []

    proposals: List[ResolutionMatch] = []
    skipped: List[Dict[str, Any]] = []

    for candidate in candidates:
        if candidate.canonical_name == subject.canonical_name:
            continue  # tier 3's job, already decided above
        if _leading_words(candidate.canonical_name, words) != subject_prefix:
            continue
        # CROSS-SOURCE ONLY, and deliberately not configurable. 2.0-B2 is a
        # cross-source story, and a same-source leading-word match is not a
        # near-miss on that — it is a different question with a much worse
        # signal-to-noise ratio. Allowing it (briefly, behind a flag) produced
        # "Test Case 10" against "Test Case 40" and "STRS Disability Review 1"
        # against "…Review 3": numbered variants of one record type, where
        # confirming either would permanently merge two unrelated records. 696
        # such pairs in one org.
        #
        # Duplicate groups WITHIN one system is a real data-quality question, but
        # it wants its own surface and its own rules (a numeric-suffix guard, at
        # minimum) rather than borrowing a cross-source review queue.
        if candidate.source_system == subject.source_system:
            skipped.append({
                "entity_id": candidate.entity_id,
                "source_system": candidate.source_system,
                "reason": REASON_SAME_SOURCE,
            })
            continue

        shared = relationship_index.shared(subject.entity_id, candidate.entity_id)
        if policy.name_prefix_require_corroboration and not shared:
            skipped.append({
                "entity_id": candidate.entity_id,
                "source_system": candidate.source_system,
                "reason": REASON_NO_CORROBORATION,
            })
            continue

        proposals.append(
            ResolutionMatch(
                target=candidate,
                tier=TIER_NAME_PREFIX,
                # Structurally ACTION_PROPOSE: TIER_NAME_PREFIX is not in
                # AUTO_MERGE_TIERS, so this branch cannot return a merge.
                action=action_for_tier(TIER_NAME_PREFIX),
                confidence=confidence_for_tier(TIER_NAME_PREFIX),
                reason=(
                    f"first {words} word(s) of the name match across sources "
                    f"(\"{subject_prefix}\")"
                    + (
                        " with a corroborating observed relationship"
                        if shared
                        else " with NO corroborating relationship — the names are "
                        "the only evidence"
                    )
                    + " — the FULL names differ, so this is a weaker signal than "
                    "an exact match and needs human confirmation"
                ),
                evidence={
                    "matched_prefix": subject_prefix,
                    "prefix_words": words,
                    "subject_name": subject.canonical_name,
                    "target_name": candidate.canonical_name,
                    # Same shape tier 3 emits — the UI reads
                    # {relationship_type, entity_id}, and handing it the raw
                    # (type, id) tuples rendered a bare "Both" with both values
                    # blank on every card.
                    "corroborating_relationships": [
                        {"relationship_type": rel, "entity_id": other}
                        for rel, other in shared
                    ],
                },
            )
        )

    proposals.sort(key=lambda m: m.target.entity_id)
    return proposals[: max(0, policy.max_proposals)], skipped


def _name_similarity_matches(
    subject: ResolutionEntity,
    candidates: Sequence[ResolutionEntity],
    relationship_index: RelationshipIndex,
    policy: ResolutionPolicy,
) -> Tuple[List[ResolutionMatch], List[Dict[str, Any]]]:
    """Tier 3 — exact canonical-name equality, PROPOSED only.

    "Similarity" here is deliberately *exact normalised equality*, not a fuzzy
    distance: a distance threshold is a dial someone will turn up, and every turn
    silently widens what the platform is willing to call the same thing. Two
    entities that merely share a name are a QUESTION for a human, which is why the
    action is always :data:`ACTION_PROPOSE` — the returned matches carry no
    authority to merge.

    Returns ``(proposals, skipped)``; ``skipped`` records name matches that did
    NOT become proposals and why, so the engine's silence is explainable.
    """
    if not subject.canonical_name:
        return [], []

    proposals: List[ResolutionMatch] = []
    skipped: List[Dict[str, Any]] = []

    for candidate in candidates:
        if candidate.canonical_name != subject.canonical_name:
            continue
        if (
            policy.require_cross_source_for_name_tier
            and candidate.source_system == subject.source_system
        ):
            skipped.append({
                "entity_id": candidate.entity_id,
                "source_system": candidate.source_system,
                "reason": REASON_SAME_SOURCE,
            })
            continue

        shared = relationship_index.shared(subject.entity_id, candidate.entity_id)
        if policy.require_corroborating_relationship and not shared:
            skipped.append({
                "entity_id": candidate.entity_id,
                "source_system": candidate.source_system,
                "reason": REASON_NO_CORROBORATION,
            })
            continue

        proposals.append(
            ResolutionMatch(
                target=candidate,
                tier=TIER_NAME_SIMILARITY,
                # Never action_for_tier's merge branch — TIER_NAME_SIMILARITY is
                # not in AUTO_MERGE_TIERS, so this is structurally ACTION_PROPOSE.
                action=action_for_tier(TIER_NAME_SIMILARITY),
                confidence=confidence_for_tier(TIER_NAME_SIMILARITY),
                reason=(
                    "exact normalised name match across sources with a "
                    "corroborating observed relationship"
                    if shared
                    else "exact normalised name match across sources"
                ),
                evidence={
                    "canonical_name": subject.canonical_name,
                    "subject_source": subject.source_system,
                    "target_source": candidate.source_system,
                    "corroborating_relationships": [
                        {"relationship_type": rel, "entity_id": other}
                        for rel, other in shared
                    ],
                },
            )
        )

    proposals.sort(
        key=lambda m: (
            -len(m.evidence.get("corroborating_relationships") or []),
            m.target.source_system,
            m.target.entity_id,
        )
    )
    if policy.max_proposals >= 0 and len(proposals) > policy.max_proposals:
        for extra in proposals[policy.max_proposals:]:
            skipped.append({
                "entity_id": extra.target.entity_id,
                "source_system": extra.target.source_system,
                "reason": f"beyond max_proposals={policy.max_proposals}",
            })
        proposals = proposals[: policy.max_proposals]
    return proposals, skipped


def _distinct_targets(matches: Sequence[ResolutionMatch]) -> List[ResolutionMatch]:
    """One match per target entity, deterministically ordered."""
    seen: set = set()
    out: List[ResolutionMatch] = []
    for match in sorted(
        matches, key=lambda m: (m.target.source_system, m.target.entity_id)
    ):
        if match.target.entity_id in seen:
            continue
        seen.add(match.target.entity_id)
        out.append(match)
    return out


def resolve_entity(
    subject: ResolutionEntity,
    candidates: Iterable[ResolutionEntity],
    *,
    alias_index: Optional[AliasIndex] = None,
    relationship_index: Optional[RelationshipIndex] = None,
    policy: ResolutionPolicy = DEFAULT_POLICY,
) -> ResolutionDecision:
    """Resolve ONE entity against a candidate pool, tier by tier.

    Order of operations, and why:

      1. Gate the pool (org / type / self / status).
      2. Tier 1 (explicit cross-reference). Exactly one distinct target →
         ``resolved`` + merge. 2+ → ``ambiguous``, no merge, STOP: disagreeing
         explicit references are a source-data problem, and dropping to a weaker
         tier to force a merge would be precisely the wrong response.
      3. Tier 2 (org alias mapping). Same rule.
      4. Tier 3 (name similarity). Never merges — any match is a PROPOSAL, so
         several matches are fine (a human picks) and the status is ``proposed``.
      5. Nothing → ``unresolved``, with the gate counts and the skipped name
         matches recorded so the null answer is explainable.
    """
    eligible, dropped = _eligible_candidates(subject, candidates)
    considered: Dict[str, Any] = {
        "candidates_supplied": sum(dropped.values()) + len(eligible),
        "eligible": len(eligible),
        "dropped": dropped,
    }

    for tier, matcher in (
        (
            TIER_EXPLICIT_REFERENCE,
            lambda: _explicit_reference_matches(subject, eligible),
        ),
        (
            TIER_ALIAS_MAPPING,
            lambda: _alias_matches(subject, eligible, alias_index or AliasIndex()),
        ),
    ):
        matches = _distinct_targets(matcher())
        if not matches:
            continue
        if len(matches) == 1:
            match = matches[0]
            return ResolutionDecision(
                subject=subject,
                status=STATUS_RESOLVED,
                tier=tier,
                confidence=match.confidence,
                merge_target=match.target,
                matches=(match,),
                proposals=(),
                reason=match.reason,
                considered=considered,
            )
        return ResolutionDecision(
            subject=subject,
            status=STATUS_AMBIGUOUS,
            tier=tier,
            confidence=CONFIDENCE_AMBIGUOUS,
            merge_target=None,
            matches=tuple(matches),
            proposals=(),
            reason=(
                f"{len(matches)} candidates matched on {tier} — left separate "
                "(conservative: an ambiguous identity is never merged)"
            ),
            considered=considered,
        )

    proposals, skipped = _name_similarity_matches(
        subject, eligible, relationship_index or EMPTY_RELATIONSHIP_INDEX, policy
    )
    if skipped:
        considered["name_matches_not_proposed"] = skipped

    if proposals:
        return ResolutionDecision(
            subject=subject,
            status=STATUS_PROPOSED,
            tier=TIER_NAME_SIMILARITY,
            confidence=CONFIDENCE_NAME_SIMILARITY,
            merge_target=None,   # never — see AUTO_MERGE_TIERS
            matches=tuple(proposals),
            proposals=tuple(proposals),
            reason=(
                f"{len(proposals)} name-similarity candidate(s) proposed for "
                "confirmation — never auto-merged"
            ),
            considered=considered,
        )

    # Tier 4 — leading-word match. Consulted LAST and only when a deployment has
    # opted in (policy.name_prefix_words > 0), so an exact match always wins and
    # the default engine is unchanged.
    prefix_proposals, prefix_skipped = _name_prefix_matches(
        subject, eligible, relationship_index or EMPTY_RELATIONSHIP_INDEX, policy
    )
    if prefix_skipped:
        considered["prefix_matches_not_proposed"] = prefix_skipped

    if prefix_proposals:
        return ResolutionDecision(
            subject=subject,
            status=STATUS_PROPOSED,
            tier=TIER_NAME_PREFIX,
            confidence=CONFIDENCE_NAME_PREFIX,
            merge_target=None,   # never — TIER_NAME_PREFIX is not an auto-merge tier
            matches=tuple(prefix_proposals),
            proposals=tuple(prefix_proposals),
            reason=(
                f"{len(prefix_proposals)} leading-word candidate(s) proposed for "
                "confirmation — the full names differ, so this is never auto-merged"
            ),
            considered=considered,
        )

    return ResolutionDecision(
        subject=subject,
        status=STATUS_UNRESOLVED,
        tier=None,
        confidence=CONFIDENCE_NONE,
        merge_target=None,
        matches=(),
        proposals=(),
        reason="no cross-source identity evidence — left separate",
        considered=considered,
    )


def resolve_entities(
    subjects: Iterable[ResolutionEntity],
    candidates: Iterable[ResolutionEntity],
    *,
    alias_index: Optional[AliasIndex] = None,
    relationship_index: Optional[RelationshipIndex] = None,
    policy: ResolutionPolicy = DEFAULT_POLICY,
) -> List[ResolutionDecision]:
    """Resolve a batch. Each subject is resolved independently against the SAME
    materialised pool and in input order, so the batch is deterministic and one
    subject's outcome can never depend on another's."""
    pool = list(candidates)
    return [
        resolve_entity(
            subject,
            pool,
            alias_index=alias_index,
            relationship_index=relationship_index,
            policy=policy,
        )
        for subject in subjects
    ]


def merge_decisions(decisions: Iterable[ResolutionDecision]) -> List[ResolutionDecision]:
    """Only the decisions that authorise an automatic merge (tiers 1–2)."""
    return [d for d in decisions if d.is_merge]


def proposal_decisions(decisions: Iterable[ResolutionDecision]) -> List[ResolutionDecision]:
    """Only the decisions carrying tier-3 proposals for human confirmation."""
    return [d for d in decisions if d.has_proposals]


# ── DB-backed loaders ───────────────────────────────────────────────────────
#
# Everything above is pure. These read the entities/relationships tables to feed
# it, and are the only functions here that touch a database. They still never
# WRITE: applying a merge (with its provenance) is a later 2.0-B2 task.


def load_resolution_entities(
    org_id: str, entity_type: str, *, resolved_only: bool = True
) -> List[ResolutionEntity]:
    """Load one org's entities of ``entity_type`` as resolver views.

    Org-scoped in SQL, so a candidate pool can never contain another tenant's
    entity — the gate in :func:`_eligible_candidates` is defence in depth, not the
    only barrier.
    """
    from . import db

    conn = db.connect()
    try:
        cur = conn.cursor()
        sql = """
            SELECT * FROM entities
            WHERE org_id = %s AND entity_type = %s
        """
        params: List[Any] = [org_id, entity_type]
        if resolved_only:
            sql += " AND resolution_status = %s"
            params.append(STATUS_RESOLVED)
        sql += " ORDER BY canonical_name, source_system, id"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    finally:
        conn.close()
    return [resolution_entity_from_entity(Entity.from_db_row(dict(r))) for r in rows]


def load_relationship_index(
    org_id: str, *, include_inferred: bool = False
) -> RelationshipIndex:
    """Build the corroboration index from one org's OBSERVED graph edges.

    ``include_inferred`` defaults to False for the reason
    :func:`app.graph_query.get_entity_relationships` gives: a caller that does not
    explicitly ask for hypotheses gets observed facts only. Corroborating a
    proposal with an inferred edge would stack a guess on a guess.
    """
    from . import db

    conn = db.connect()
    try:
        cur = conn.cursor()
        sql = """
            SELECT from_entity_id, to_entity_id, relationship_type, inferred
            FROM entity_relationships
            WHERE org_id = %s
        """
        if not include_inferred:
            sql += " AND inferred = FALSE"
        cur.execute(sql, (org_id,))
        edges = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return build_relationship_index(edges, include_inferred=include_inferred)


def resolve_org_entity_type(
    org_id: str,
    entity_type: str,
    *,
    policy: ResolutionPolicy = DEFAULT_POLICY,
) -> List[ResolutionDecision]:
    """Run the ranked engine over every entity of one type in one org.

    The convenience entry point: loads the candidate pool, the org's alias table
    (:mod:`app.entity_alias_mappings`), and the observed-relationship index, then
    resolves each entity against all the others. Read-only — it returns decisions
    and writes nothing.
    """
    from .entity_alias_mappings import get_alias_index

    pool = load_resolution_entities(org_id, entity_type)
    if not pool:
        return []
    return resolve_entities(
        pool,
        pool,
        alias_index=get_alias_index(org_id),
        relationship_index=load_relationship_index(org_id),
        policy=policy,
    )


__all__ = [
    "TIER_EXPLICIT_REFERENCE",
    "TIER_ALIAS_MAPPING",
    "TIER_NAME_SIMILARITY",
    "TIER_RANK",
    "TIERS_BY_RANK",
    "AUTO_MERGE_TIERS",
    "ACTION_MERGE",
    "ACTION_PROPOSE",
    "ACTION_NONE",
    "STATUS_RESOLVED",
    "STATUS_PROPOSED",
    "STATUS_AMBIGUOUS",
    "STATUS_UNRESOLVED",
    "CONFIDENCE_EXPLICIT_REFERENCE",
    "CONFIDENCE_ALIAS_MAPPING",
    "CONFIDENCE_NAME_PREFIX",
    "TIER_NAME_PREFIX",
    "CONFIDENCE_NAME_SIMILARITY",
    "CONFIDENCE_AMBIGUOUS",
    "METADATA_CROSS_REFERENCES",
    "METADATA_EXTERNAL_IDS",
    "KNOWN_CROSS_REFERENCE_KEYS",
    "ExternalRef",
    "ResolutionEntity",
    "ResolutionMatch",
    "ResolutionDecision",
    "ResolutionPolicy",
    "DEFAULT_POLICY",
    "RelationshipIndex",
    "EMPTY_RELATIONSHIP_INDEX",
    "action_for_tier",
    "confidence_for_tier",
    "extract_cross_references",
    "resolution_entity_from_entity",
    "resolution_entities_from_entities",
    "build_relationship_index",
    "build_alias_index",
    "resolve_entity",
    "resolve_entities",
    "merge_decisions",
    "proposal_decisions",
    "load_resolution_entities",
    "load_relationship_index",
    "resolve_org_entity_type",
]
