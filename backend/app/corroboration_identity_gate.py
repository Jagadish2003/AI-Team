"""
corroboration_identity_gate.py — Release 2.0-B2 T6: the corroboration identity gate.

Cross-source corroboration is the platform's strongest confidence signal: two
independent systems agreeing takes a finding to HIGH. That agreement only means
something if both systems are talking about **the same thing**.

Today COR-01 (ServiceNow) and COR-02 (Jira) fire on a DETECTOR link, and nothing
checks the identity of the entities each side references. So a ServiceNow team
called "Payments" and a Jira project called "Payments" — two unrelated things that
happen to share a word — produce "Corroborated across ServiceNow and Jira" and a
HIGH confidence. That is the subtle honesty gap AC5 closes: **a same name is not
a shared identity.**

## What this gate does

When both corroborating sides reference an entity and those references LOOK like
the same thing (equal normalised names across different sources), the gate
requires that identity to be **genuinely resolved** before the pairing may raise
confidence. Genuinely resolved means one of, and only one of (strongest first):

  * the two references are literally the same graph entity;
  * they resolve to the same APPLIED merge survivor (:mod:`app.entity_merge`),
    reported with the rule that merge's provenance recorded — the authoritative
    answer, because it is what the graph actually did;
  * a human CONFIRMED the pair in the review surface
    (:mod:`app.entity_match_proposals`);
  * the ranked engine (:mod:`app.cross_source_resolution`) AUTO-MERGES them — an
    explicit cross-reference in the source data, or the org's alias table — which
    is the answer for a pair no merge has been applied to yet.

A name match is never enough — and it cannot become enough by accident, because
the re-derivation asks T1's engine for a MERGE decision and T1's name-similarity
tier is structurally incapable of producing one (``AUTO_MERGE_TIERS`` excludes
it), while the merge path reads only rules T2 was permitted to merge on. The gate
inherits those guarantees rather than re-implementing a weaker copy of them.

## What this gate deliberately does NOT do

It does not re-decide corroboration that rests on no identity claim. Where the two
sides reference plainly different things (a ServiceNow *team* and a Jira
*process*, say), the corroboration is detector-linked — the pre-existing, weaker
basis — and T6 is not the ticket that changes that. Silently downgrading every
such finding would be a large, unannounced behaviour change dressed up as a
correctness fix.

What the gate does instead is make the distinction VISIBLE: every verdict is
recorded on the result (``identity_verified``, the basis, and the reason), so a
reviewer can see whether a HIGH rests on a verified shared identity or merely on
a shared detector.

## Degradation — one rule, and it fails CLOSED

**An identity claim that is not positively resolved never elevates, whatever the
reason** — not resolved, resolver error, no resolver. There is deliberately no
"probably fine" branch.

The alternative (elevate when the graph cannot be read) would reopen this exact
hole precisely when the system is unhealthy, and a wrong HIGH is the harmful
direction: it is quoted in a board paper, while a conservative MEDIUM is merely
cautious and recovers on the next run. Every refusal is recorded with its reason
and logged, so a run that lost elevation to an unreadable graph says so rather
than looking like a genuine downgrade.

The one fail-OPEN path is this module failing to IMPORT: the engine then cannot
extract references at all, and it logs a warning naming the consequence. That is
a packaging fault CI catches, not a runtime data condition — unlike a graph read,
which will fail in production eventually and therefore fails closed above.

Pure by construction: the decision logic takes an injected resolver, so the whole
gate is testable without a database. The DB-backed resolver sits at the bottom.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# How an identity claim was (or was not) settled.
BASIS_SAME_ENTITY = "same_entity"
BASIS_EXPLICIT_REFERENCE = "explicit_reference"
BASIS_ALIAS_MAPPING = "alias_mapping"
BASIS_CONFIRMED_PROPOSAL = "confirmed_proposal"

#: The bases that genuinely establish one identity. A name match is absent by
#: design and must stay absent — that absence IS the acceptance criterion.
RESOLVED_BASES: frozenset = frozenset({
    BASIS_SAME_ENTITY,
    BASIS_EXPLICIT_REFERENCE,
    BASIS_ALIAS_MAPPING,
    BASIS_CONFIRMED_PROPOSAL,
})

# Why a claim was not settled — recorded so the gate's answer is explainable.
REASON_NOT_RESOLVED = "same name across sources but no resolved identity"
REASON_NO_CLAIM = "the sources reference different things — no identity claim"
REASON_NO_REFERENCE = "a corroborating record carries no entity reference"
REASON_UNVERIFIABLE = "identity could not be verified (graph unavailable)"

#: Record fields that carry an entity's STABLE id per source system, strongest
#: first. An explicit enumeration, never a heuristic: a field is read as an
#: identity only because it is named here.
_RECORD_ID_FIELDS: Dict[str, Tuple[str, ...]] = {
    "servicenow": ("ci_sys_id", "cmdb_ci_sys_id", "assignment_group_id", "sys_id"),
    "jira": ("project_id", "component_id", "issue_key", "key"),
    "java_app": ("app_id", "service_id"),
    "dotnet_app": ("app_id", "service_id"),
}

#: Record fields that carry an entity's NAME per source system. A name locates a
#: candidate; it never proves an identity.
_RECORD_NAME_FIELDS: Dict[str, Tuple[str, ...]] = {
    "servicenow": ("entity_name", "ci", "ci_name", "service", "assignment_group", "team"),
    "jira": ("entity_name", "component", "project", "service", "process"),
    "java_app": ("entity_name", "service", "app_id"),
    "dotnet_app": ("entity_name", "service", "app_id"),
}


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        # ServiceNow display-value envelopes: prefer the raw value.
        value = value.get("value") or value.get("display_value")
    return str(value or "").strip()


def _canonical(value: Any) -> str:
    """The shared canonicalisation, so this layer and the entity layer cannot
    disagree about what a name is."""
    try:
        from .entity_resolution import canonical_name_for

        return canonical_name_for(_text(value))
    except Exception:  # noqa: BLE001 — the gate must not need the app package.
        return " ".join(_text(value).split()).lower()


# ── what a corroborating record points at ───────────────────────────────────


@dataclass(frozen=True)
class EntityRef:
    """The entity one corroborating record references.

    ``record_id`` is the stable source id when the record carries one (the same
    value an ``entities`` row holds as ``source_record_id``); ``name`` is the
    display name. ``field`` records WHERE it was read from, so a verdict can name
    its evidence.
    """

    source_system: str
    record_id: Optional[str] = None
    name: str = ""
    field_name: str = ""

    @property
    def canonical_name(self) -> str:
        return _canonical(self.name)

    @property
    def is_present(self) -> bool:
        return bool(self.record_id or self.canonical_name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_system": self.source_system,
            "record_id": self.record_id,
            "name": self.name,
            "canonical_name": self.canonical_name,
            "field": self.field_name,
        }


def entity_ref_from_record(
    source_system: str, record: Optional[Mapping[str, Any]]
) -> Optional[EntityRef]:
    """Extract the entity a corroborating record references, or ``None``.

    Reads the enumerated id fields first (a stable identity), then the enumerated
    name fields. An unrecognised field is never read as an entity reference —
    guessing here would invent identity claims the source never made.
    """
    if not isinstance(record, Mapping):
        return None
    system = _text(source_system).lower()
    for key in _RECORD_ID_FIELDS.get(system, ()):
        value = _text(record.get(key))
        if value:
            name = ""
            for name_key in _RECORD_NAME_FIELDS.get(system, ()):
                name = _text(record.get(name_key))
                if name:
                    break
            return EntityRef(system, record_id=value, name=name, field_name=key)
    for key in _RECORD_NAME_FIELDS.get(system, ()):
        value = _text(record.get(key))
        if value:
            return EntityRef(system, record_id=None, name=value, field_name=key)
    return None


def first_entity_ref(
    source_system: str, records: Sequence[Mapping[str, Any]]
) -> Optional[EntityRef]:
    """The first usable entity reference among a source's corroborating records.

    Deterministic (input order is the engine's own deterministic record order),
    and ``None`` when no record names an entity — which the gate treats as "no
    identity claim", not as a failed one.
    """
    for record in records or ():
        ref = entity_ref_from_record(source_system, record)
        if ref is not None and ref.is_present:
            return ref
    return None


# ── the verdict ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdentityVerdict:
    """Whether two cross-source references are the same real thing.

    ``claim`` is True when the two references LOOK like the same thing (equal
    normalised names across different sources) — i.e. when an identity is being
    asserted and therefore has to be proven. ``resolved`` is whether it was.
    """

    left: Optional[EntityRef]
    right: Optional[EntityRef]
    claim: bool
    resolved: bool
    basis: Optional[str] = None
    reason: str = ""

    @property
    def blocks_elevation(self) -> bool:
        """The AC5 condition: an identity IS claimed and is NOT resolved."""
        return self.claim and not self.resolved

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left": self.left.to_dict() if self.left else None,
            "right": self.right.to_dict() if self.right else None,
            "claim": self.claim,
            "resolved": self.resolved,
            "basis": self.basis,
            "reason": self.reason,
            "blocks_elevation": self.blocks_elevation,
        }


#: A resolver answers "are these two references the same entity?" and returns the
#: BASIS when they are, else ``None``. Injected so the gate is testable DB-free.
IdentityResolverFn = Callable[[str, EntityRef, EntityRef], Optional[str]]


def _claims_same_identity(left: EntityRef, right: EntityRef) -> bool:
    """Do the two references purport to be the same thing?

    Deliberately narrow: equal normalised names from DIFFERENT source systems.
    That is precisely the shape AC5 calls out — and it is the only shape where a
    reader would read "corroborated across two systems" as "about one thing".
    Two plainly different names make no identity claim, so there is nothing for
    this gate to prove or refuse.
    """
    if not left.is_present or not right.is_present:
        return False
    if left.source_system == right.source_system:
        return False
    return bool(left.canonical_name) and left.canonical_name == right.canonical_name


def check_identity(
    org_id: str,
    left: Optional[EntityRef],
    right: Optional[EntityRef],
    *,
    resolver: Optional[IdentityResolverFn] = None,
) -> IdentityVerdict:
    """Decide whether a cross-source identity claim is genuinely resolved.

    Pure apart from the injected ``resolver``. Never raises, and never guesses in
    the permissive direction: a resolver that fails or is absent yields "claimed
    but unverifiable", which is NOT a resolved identity and therefore blocks the
    elevation, recorded with its reason (see the module docstring).
    """
    if left is None or right is None or not left.is_present or not right.is_present:
        return IdentityVerdict(
            left=left, right=right, claim=False, resolved=False,
            reason=REASON_NO_REFERENCE,
        )

    if (
        left.record_id
        and right.record_id
        and left.source_system == right.source_system
        and left.record_id == right.record_id
    ):
        return IdentityVerdict(
            left=left, right=right, claim=True, resolved=True,
            basis=BASIS_SAME_ENTITY, reason="the same source record",
        )

    if not _claims_same_identity(left, right):
        return IdentityVerdict(
            left=left, right=right, claim=False, resolved=False,
            reason=REASON_NO_CLAIM,
        )

    if resolver is None:
        return IdentityVerdict(
            left=left, right=right, claim=True, resolved=False,
            reason=REASON_UNVERIFIABLE,
        )

    try:
        basis = resolver(org_id, left, right)
    except Exception as exc:  # noqa: BLE001 — never break scoring on a graph read.
        logger.warning(
            "corroboration identity gate: resolver failed for org=%s (%s vs %s): %s",
            org_id, left.source_system, right.source_system, exc,
        )
        return IdentityVerdict(
            left=left, right=right, claim=True, resolved=False,
            reason=REASON_UNVERIFIABLE,
        )

    if basis in RESOLVED_BASES:
        return IdentityVerdict(
            left=left, right=right, claim=True, resolved=True, basis=basis,
            reason=f"identity resolved by {basis}",
        )
    return IdentityVerdict(
        left=left, right=right, claim=True, resolved=False,
        reason=REASON_NOT_RESOLVED,
    )


# ── gating the fired rules ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GateOutcome:
    """What the gate did to one corroboration evaluation — always reported.

    ``blocked_rules`` are the cross-source rules whose elevation is refused
    because the identity they rest on is claimed but unresolved.
    ``identity_verified`` says whether a genuine shared identity was PROVEN, so a
    reviewer can tell a verified HIGH from a detector-linked one.
    """

    verdict: IdentityVerdict
    blocked_rules: Tuple[str, ...] = ()
    applied: bool = False

    @property
    def identity_verified(self) -> bool:
        return self.verdict.resolved

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applied": self.applied,
            "identity_verified": self.identity_verified,
            "identity_claim": self.verdict.claim,
            "blocked_rules": list(self.blocked_rules),
            "basis": self.verdict.basis,
            "reason": self.verdict.reason,
            "left": self.verdict.left.to_dict() if self.verdict.left else None,
            "right": self.verdict.right.to_dict() if self.verdict.right else None,
        }


#: The cross-source pairing this gate governs: the two independent primary
#: corroborators whose agreement is what "corroborated across systems" means.
#: COR-03 (triple) is derived from both, so it falls with them.
CROSS_SOURCE_RULE_PAIR: Tuple[str, str] = ("COR-01", "COR-02")
DERIVED_CROSS_SOURCE_RULES: Tuple[str, ...] = ("COR-03",)


def gate_cross_source_corroboration(
    org_id: str,
    fired_rules: Sequence[str],
    *,
    left: Optional[EntityRef],
    right: Optional[EntityRef],
    resolver: Optional[IdentityResolverFn] = None,
) -> GateOutcome:
    """Refuse cross-source elevation that rests on an unresolved identity (AC5).

    The gate only engages when BOTH cross-source rules fired — one source alone
    is not a cross-source claim, and COR-08 already handles the single-source
    case. When it engages and the identity is claimed but unresolved, both
    primary rules and the derived triple are blocked from elevating.

    Blocking removes the ELEVATION, not the evidence: the rules stay on the
    result (a reviewer should still see that ServiceNow and Jira each had
    something to say) and the reason is recorded.
    """
    fired = set(fired_rules or ())
    engaged = all(rule in fired for rule in CROSS_SOURCE_RULE_PAIR)
    verdict = check_identity(org_id, left, right, resolver=resolver)

    if not engaged:
        return GateOutcome(verdict=verdict, blocked_rules=(), applied=False)
    if not verdict.blocks_elevation:
        return GateOutcome(verdict=verdict, blocked_rules=(), applied=True)

    blocked = tuple(
        rule for rule in CROSS_SOURCE_RULE_PAIR + DERIVED_CROSS_SOURCE_RULES
        if rule in fired
    )
    logger.info(
        "2.0-B2 T6: cross-source corroboration not elevated for org=%s — %s "
        "(%s '%s' vs %s '%s')",
        org_id, verdict.reason,
        (left.source_system if left else "?"), (left.name if left else ""),
        (right.source_system if right else "?"), (right.name if right else ""),
    )
    return GateOutcome(verdict=verdict, blocked_rules=blocked, applied=True)


# ── the graph-backed resolver ───────────────────────────────────────────────


def _load_entity(cur: Any, org_id: str, ref: EntityRef) -> Optional[Dict[str, Any]]:
    """Find the graph entity a reference points at, org-scoped.

    A stable ``(source_system, source_record_id)`` is tried first. Falling back to
    the canonical NAME is a LOOKUP, not a decision: it locates a candidate row so
    the resolution engine can judge it. Exactly one confidently-resolved match
    counts — several same-named rows are ambiguous, and ambiguity never resolves.
    """
    if ref.record_id:
        cur.execute(
            "SELECT * FROM entities WHERE org_id = %s AND source_system = %s "
            "AND source_record_id = %s AND resolution_status = 'resolved' "
            "ORDER BY created_at ASC, id ASC LIMIT 2",
            (org_id, ref.source_system, ref.record_id),
        )
        rows = cur.fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return None
    canonical = ref.canonical_name
    if not canonical:
        return None
    cur.execute(
        "SELECT * FROM entities WHERE org_id = %s AND source_system = %s "
        "AND canonical_name = %s AND resolution_status = 'resolved' "
        "ORDER BY created_at ASC, id ASC LIMIT 2",
        (org_id, ref.source_system, canonical),
    )
    rows = cur.fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def _merged_identity_basis(
    org_id: str, left_entity_id: str, right_entity_id: str
) -> Optional[str]:
    """The basis recorded by an APPLIED merge (2.0-B2 T2), or ``None``.

    Two entities that were actually merged resolve to the same survivor. The
    survivor's ``merge_provenance`` records, per constituent, the rule that merged
    it — so the gate reports the rule the graph acted on rather than one it
    re-derived. Only a rule T2 was permitted to merge on can be returned, which is
    why a name match cannot leak in here either.

    Returns ``None`` (never raises) when the merge layer is unavailable — the
    resolver then falls through to re-derivation.
    """
    try:
        from . import db
        from .entity_merge import (
            MERGE_RULES,
            MergeProvenance,
            resolve_survivor_id,
        )
    except Exception as exc:  # noqa: BLE001 — merge layer optional to this gate.
        logger.debug("corroboration identity gate: merge layer unavailable: %s", exc)
        return None

    con = db.connect()
    try:
        cur = con.cursor()
        left_head = resolve_survivor_id(cur, org_id, left_entity_id)
        right_head = resolve_survivor_id(cur, org_id, right_entity_id)
        if left_head != right_head:
            return None
        cur.execute(
            "SELECT id, metadata FROM entities WHERE org_id = %s AND id = %s",
            (org_id, left_head),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None
    metadata = row["metadata"] if isinstance(row, Mapping) else row[1]
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except Exception:  # noqa: BLE001 — corrupt metadata proves nothing.
            return None
    provenance = MergeProvenance.from_metadata(str(left_head), metadata or {})

    # The rule that merged whichever of the two was absorbed. Both may be
    # constituents of a third survivor, so take the strongest recorded rule among
    # the ones that apply, in the enumeration's own order.
    involved = {left_entity_id, right_entity_id}
    rules = {
        c.rule for c in provenance.constituents
        if c.entity_id in involved and c.rule in MERGE_RULES
    }
    for rule in MERGE_RULES:
        if rule in rules:
            return rule
    # Merged (same survivor) but no rule recorded for either side — e.g. one side
    # IS the survivor and the other was merged before provenance existed. The
    # merge is still a recorded identity, so report the weakest honest basis.
    return BASIS_SAME_ENTITY if provenance.is_merged else None


def graph_identity_resolver(
    org_id: str, left: EntityRef, right: EntityRef
) -> Optional[str]:
    """The production resolver: does the GRAPH say these are one identity?

    Consulted strongest-first, and every source of truth is one a human or a
    machine explicitly stated:

      1. the two references locate the SAME entity row;
      2. they resolve to the same T2 MERGE SURVIVOR — the merge that actually
         happened, with the rule it recorded. Authoritative over any
         re-derivation: an alias table edited after the merge could make step 4
         disagree with what the graph already did;
      3. a human CONFIRMED the pair in the T3 review surface;
      4. the T1 ranked engine AUTO-MERGES them — an explicit cross-reference or
         the org's alias table (the answer for a pair not yet applied by T2).

    Returns the basis, or ``None`` when nothing establishes the identity. A name
    match cannot return a basis: step 4 asks T1 for a MERGE, and T1's
    name-similarity tier is structurally incapable of producing one; step 2 reads
    only rules T2 was permitted to merge on.
    """
    from . import db
    from .cross_source_resolution import (
        resolution_entity_from_entity,
        resolve_entity,
    )
    from database.models.entities import Entity

    con = db.connect()
    try:
        cur = con.cursor()
        left_row = _load_entity(cur, org_id, left)
        right_row = _load_entity(cur, org_id, right)
    finally:
        con.close()

    if left_row is None or right_row is None:
        return None

    left_id, right_id = str(left_row["id"]), str(right_row["id"])
    if left_id == right_id:
        return BASIS_SAME_ENTITY

    # The merge that ACTUALLY happened (2.0-B2 T2) outranks everything below: it
    # is the recorded fact, and its provenance names the rule that authorised it.
    merged_basis = _merged_identity_basis(org_id, left_id, right_id)
    if merged_basis is not None:
        return merged_basis

    # A human confirmation outranks a re-derivation: it is a recorded decision,
    # and it is the only way a name-similarity pair may ever count.
    try:
        from .entity_match_proposals import confirmed_pairs

        confirmed = {
            tuple(sorted((pair[1], pair[2]))) for pair in confirmed_pairs(org_id)
        }
        if tuple(sorted((left_id, right_id))) in confirmed:
            return BASIS_CONFIRMED_PROPOSAL
    except Exception as exc:  # noqa: BLE001 — a missing review table is not fatal.
        logger.debug("corroboration identity gate: confirmed-pair read failed: %s", exc)

    subject = resolution_entity_from_entity(Entity.from_db_row(dict(left_row)))
    candidate = resolution_entity_from_entity(Entity.from_db_row(dict(right_row)))
    # The org's alias table must be supplied or tier 2 could never fire here —
    # an Owner-asserted identity would be invisible to the gate that most needs
    # it. No relationship index is passed: tier 3 proposes and never merges, so
    # it cannot contribute a basis either way.
    try:
        from .entity_alias_mappings import get_alias_index

        alias_index = get_alias_index(org_id)
    except Exception as exc:  # noqa: BLE001 — a bad alias table disables tier 2 only.
        logger.debug("corroboration identity gate: alias table unreadable: %s", exc)
        alias_index = None
    decision = resolve_entity(subject, [candidate], alias_index=alias_index)
    if decision.is_merge and decision.merge_target is not None:
        if str(decision.merge_target.entity_id) == right_id:
            return decision.tier
    return None


def default_resolver() -> IdentityResolverFn:
    """The resolver the engine uses in production."""
    return graph_identity_resolver


__all__ = [
    "BASIS_SAME_ENTITY",
    "BASIS_EXPLICIT_REFERENCE",
    "BASIS_ALIAS_MAPPING",
    "BASIS_CONFIRMED_PROPOSAL",
    "RESOLVED_BASES",
    "REASON_NOT_RESOLVED",
    "REASON_NO_CLAIM",
    "REASON_NO_REFERENCE",
    "REASON_UNVERIFIABLE",
    "CROSS_SOURCE_RULE_PAIR",
    "DERIVED_CROSS_SOURCE_RULES",
    "EntityRef",
    "IdentityVerdict",
    "GateOutcome",
    "IdentityResolverFn",
    "entity_ref_from_record",
    "first_entity_ref",
    "check_identity",
    "gate_cross_source_corroboration",
    "graph_identity_resolver",
    "default_resolver",
]
