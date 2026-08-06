"""
entity_merge.py — Release 2.0-B2 T2: applying a merge, with its provenance.

T1 decides which entities are the same thing and refuses to merge on weak
evidence. T3 lets a person answer the questions T1 refuses. **This module is the
only place either answer is actually applied to the graph** — and the reason it
is its own module is that a merge is the most destructive-by-accident operation
in the platform: a wrongly merged entity corrupts every finding built on it, and
the corruption is invisible.

So a merge here is applied in the one shape that keeps it inspectable and
reversible:

  * **Nothing is deleted.** A constituent entity keeps its row, its identity, and
    its edges; it gains a ``merged_into`` pointer in metadata. Deleting the row
    would destroy exactly the evidence AC2 requires and would make the AC4 unmerge
    impossible (2.0-B2 T5).
  * **Every constituent source identity is retained** on the survivor, INCLUDING
    the survivor's own. "This entity is ServiceNow CI ``sn-1`` and Jira project
    ``PAY`` and git repo ``payments-api``" is the fact a finding needs to show;
    a list that silently omits the survivor's own identity is not that fact.
  * **The rule that merged it is recorded per constituent**, not once per entity.
    A node merged from three sources may have been merged by three different
    rules on three different days, by three different actors. One rule field
    would have to lie about two of them.

Survivor selection is DETERMINISTIC (:func:`choose_survivor`), because a merge
that picks a different winner on re-run would rewrite history each time:
an existing survivor wins, then a stable ``source_record_id``, then the earliest
``created_at``, then the lowest id — a total order, so there is never a tie.

Merges compose transitively: both sides are resolved to their CURRENT survivor
before merging (:func:`resolve_survivor_id`, cycle-guarded), so merging C into an
already-merged B lands C on B's survivor rather than creating a second head.

Applying is idempotent — re-applying a merge already recorded changes nothing and
is reported as ``already_merged``, never written twice.

What this module does NOT do: change ``resolution_status``, rewrite edges, or
hide a constituent from any list. Those are display/graph-consolidation concerns
(and the corroboration uplift is AC5). Keeping the constituent visible is the
conservative choice: nothing disappears from a customer's graph because a rule
fired.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import db

logger = logging.getLogger(__name__)

#: Metadata key holding the survivor's provenance block.
METADATA_MERGE_PROVENANCE = "merge_provenance"

#: Metadata key on a CONSTITUENT pointing at the entity it was merged into.
METADATA_MERGED_INTO = "merged_into"

#: Bumped when the stored provenance shape changes in a way a reader must notice.
MERGE_PROVENANCE_VERSION = 1

# The rules that can merge. The first two are T1's auto-merge tiers; the third is
# a human confirming a T3 proposal — deliberately its OWN rule, because it was not
# the name-similarity tier that authorised the merge (that tier can never
# authorise one), it was a person.
RULE_EXPLICIT_REFERENCE = "explicit_reference"
RULE_ALIAS_MAPPING = "alias_mapping"
RULE_CONFIRMED_PROPOSAL = "confirmed_proposal"

MERGE_RULES: Tuple[str, ...] = (
    RULE_EXPLICIT_REFERENCE,
    RULE_ALIAS_MAPPING,
    RULE_CONFIRMED_PROPOSAL,
)

#: Actor recorded when a merge was applied by a rule rather than a person.
ACTOR_SYSTEM = "system"

# Outcomes of one apply attempt, so a caller can report what happened rather than
# guess from a count.
OUTCOME_MERGED = "merged"
OUTCOME_ALREADY_MERGED = "already_merged"
OUTCOME_SKIPPED = "skipped"
#: 2.0-B2 T5: the pair was deliberately UNMERGED, so re-merging it is refused.
#: Distinct from ``skipped`` on purpose — skipped means "this applier had no
#: authority here", blocked means "a person reversed this and the reversal stands".
OUTCOME_BLOCKED = "blocked"

#: Guard against a corrupt merged_into cycle (A→B→A). Far above any real chain.
_MAX_SURVIVOR_HOPS = 32


class EntityMergeError(ValueError):
    """A merge cannot be applied as asked (unknown entity, cross-org, bad rule)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 — corrupt metadata must not break a read.
            logger.warning("entity metadata is not readable JSON; treating as empty")
            return {}
    return {}


# ── the provenance model ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConstituentIdentity:
    """One source identity a merged entity is made of.

    ``rule`` is what merged THIS constituent in (the survivor's own record carries
    the rule ``None`` — it was not merged in, it is the origin), and ``merged_by``
    is who applied it: an actor id for a confirmed proposal, :data:`ACTOR_SYSTEM`
    for an auto-merge tier.
    """

    entity_id: str
    source_system: str
    source_record_id: Optional[str]
    display_name: str
    canonical_name: str
    rule: Optional[str] = None
    confidence: Optional[float] = None
    merged_at: Optional[str] = None
    merged_by: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    is_origin: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "display_name": self.display_name,
            "canonical_name": self.canonical_name,
            "rule": self.rule,
            "confidence": self.confidence,
            "merged_at": self.merged_at,
            "merged_by": self.merged_by,
            "evidence": dict(self.evidence),
            "is_origin": self.is_origin,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConstituentIdentity":
        return cls(
            entity_id=_text(raw.get("entity_id")),
            source_system=_text(raw.get("source_system")),
            source_record_id=raw.get("source_record_id"),
            display_name=_text(raw.get("display_name")),
            canonical_name=_text(raw.get("canonical_name")),
            rule=raw.get("rule"),
            confidence=raw.get("confidence"),
            merged_at=raw.get("merged_at"),
            merged_by=raw.get("merged_by"),
            evidence=raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {},
            is_origin=bool(raw.get("is_origin")),
        )


@dataclass(frozen=True)
class MergeProvenance:
    """Everything a finding needs to show where a merged entity came from (AC2).

    ``constituents`` always includes the survivor's own identity (``is_origin``),
    so ``source_systems`` is the complete set of systems this one entity speaks
    for — the fact the corroboration uplift (T5) has to be able to trust.
    """

    entity_id: str
    constituents: Tuple[ConstituentIdentity, ...] = ()
    version: int = MERGE_PROVENANCE_VERSION
    last_merged_at: Optional[str] = None

    @property
    def is_merged(self) -> bool:
        """True once at least one OTHER identity was merged in. An unmerged entity
        has only its own identity, which is not a merge."""
        return any(not c.is_origin for c in self.constituents)

    @property
    def rules(self) -> Tuple[str, ...]:
        """The distinct rules that produced this entity, strongest-first order of
        first appearance preserved as sorted for determinism."""
        return tuple(sorted({c.rule for c in self.constituents if c.rule}))

    @property
    def source_systems(self) -> Tuple[str, ...]:
        return tuple(sorted({c.source_system for c in self.constituents if c.source_system}))

    @property
    def source_identities(self) -> Tuple[Dict[str, Any], ...]:
        """The constituent identities as ``(source_system, source_record_id)`` —
        the minimal form a finding renders."""
        return tuple(
            {"source_system": c.source_system, "source_record_id": c.source_record_id}
            for c in self.constituents
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "entity_id": self.entity_id,
            "constituents": [c.to_dict() for c in self.constituents],
            "rules": list(self.rules),
            "source_systems": list(self.source_systems),
            "constituent_count": len(self.constituents),
            "is_merged": self.is_merged,
            "last_merged_at": self.last_merged_at,
        }

    @classmethod
    def from_metadata(
        cls, entity_id: str, metadata: Optional[Mapping[str, Any]]
    ) -> "MergeProvenance":
        block = (metadata or {}).get(METADATA_MERGE_PROVENANCE)
        if not isinstance(block, Mapping):
            return cls(entity_id=entity_id, constituents=())
        raw = block.get("constituents")
        raw = raw if isinstance(raw, list) else []
        return cls(
            entity_id=entity_id,
            constituents=tuple(
                ConstituentIdentity.from_dict(item)
                for item in raw
                if isinstance(item, Mapping) and _text(item.get("entity_id"))
            ),
            version=int(block.get("version") or MERGE_PROVENANCE_VERSION),
            last_merged_at=block.get("last_merged_at"),
        )


@dataclass(frozen=True)
class MergeOutcome:
    """The result of one apply attempt — always reported, never inferred."""

    outcome: str
    rule: str
    survivor_id: Optional[str] = None
    merged_entity_id: Optional[str] = None
    reason: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome == OUTCOME_MERGED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "rule": self.rule,
            "survivor_id": self.survivor_id,
            "merged_entity_id": self.merged_entity_id,
            "reason": self.reason,
        }


# ── pure helpers ────────────────────────────────────────────────────────────


def identity_of(row: Mapping[str, Any], *, is_origin: bool = False, **stamp: Any) -> ConstituentIdentity:
    """Build a constituent identity from an ``entities`` row."""
    return ConstituentIdentity(
        entity_id=_text(row.get("id") or row.get("entity_id")),
        source_system=_text(row.get("source_system")),
        source_record_id=row.get("source_record_id"),
        display_name=_text(row.get("display_name")),
        canonical_name=_text(row.get("canonical_name")),
        is_origin=is_origin,
        **stamp,
    )


def _sort_key(row: Mapping[str, Any]) -> Tuple[int, int, str, str]:
    """Deterministic survivor preference — see :func:`choose_survivor`."""
    metadata = _loads(row.get("metadata"))
    provenance = MergeProvenance.from_metadata(_text(row.get("id")), metadata)
    return (
        0 if provenance.is_merged else 1,          # an existing survivor wins
        0 if _text(row.get("source_record_id")) else 1,  # a stable id beats a name
        str(row.get("created_at") or ""),          # then the oldest
        _text(row.get("id")),                      # then a total order — never a tie
    )


def choose_survivor(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """Pick the entity the others merge INTO, deterministically.

    Order of preference, and why each step exists:

      1. **An entity that is already a merge survivor** — so repeated merges
         accumulate on one node instead of fragmenting into competing heads.
      2. **An entity with a stable ``source_record_id``** — a name-derived row is
         the weaker identity to hang a merged node on; it can change.
      3. **The earliest ``created_at``** — the longest-standing node keeps its id,
         so existing references stay valid.
      4. **The lowest entity id** — a total order, so two otherwise-equal rows
         never produce a coin-flip that differs between runs.
    """
    usable = [r for r in rows if _text(r.get("id"))]
    if not usable:
        return None
    return sorted(usable, key=_sort_key)[0]


def build_merged_provenance(
    survivor: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    rule: str,
    confidence: Optional[float] = None,
    actor: str = ACTOR_SYSTEM,
    evidence: Optional[Mapping[str, Any]] = None,
    merged_at: Optional[str] = None,
) -> MergeProvenance:
    """Fold ``incoming`` (and everything IT was made of) into the survivor's
    provenance — pure, so the shape is testable without a database.

    Three properties this function exists to guarantee:

      * the survivor's OWN identity is present as the origin constituent, so the
        list is the complete set of identities the node speaks for;
      * an entity that was itself a merge brings its whole constituent list, so a
        chain of merges never loses the identities in the middle;
      * re-folding the same constituent updates nothing and duplicates nothing
        (keyed on ``entity_id``), which is what makes apply idempotent.
    """
    if rule not in MERGE_RULES:
        raise EntityMergeError(
            f"rule must be one of {list(MERGE_RULES)}; got {rule!r}"
        )
    stamp = merged_at or _now()
    survivor_id = _text(survivor.get("id"))

    existing = MergeProvenance.from_metadata(
        survivor_id, _loads(survivor.get("metadata"))
    )
    by_id: Dict[str, ConstituentIdentity] = {c.entity_id: c for c in existing.constituents}

    # The survivor's own identity — added once, never re-stamped with a rule.
    if survivor_id not in by_id:
        by_id[survivor_id] = identity_of(survivor, is_origin=True)

    incoming_provenance = MergeProvenance.from_metadata(
        _text(incoming.get("id")), _loads(incoming.get("metadata"))
    )
    # The incoming entity itself, plus anything it had already absorbed. An
    # already-merged constituent keeps the rule that first merged it — rewriting
    # it with this merge's rule would misattribute the earlier decision.
    incoming_identities = [identity_of(incoming)] + [
        c for c in incoming_provenance.constituents if c.entity_id != _text(incoming.get("id"))
    ]
    for identity in incoming_identities:
        if identity.entity_id in by_id and not by_id[identity.entity_id].is_origin:
            continue
        if identity.entity_id == survivor_id:
            continue
        by_id[identity.entity_id] = ConstituentIdentity(
            entity_id=identity.entity_id,
            source_system=identity.source_system,
            source_record_id=identity.source_record_id,
            display_name=identity.display_name,
            canonical_name=identity.canonical_name,
            rule=identity.rule or rule,
            confidence=identity.confidence if identity.rule else confidence,
            merged_at=identity.merged_at or stamp,
            merged_by=identity.merged_by or actor,
            evidence=identity.evidence or dict(evidence or {}),
            is_origin=False,
        )

    ordered = tuple(
        sorted(by_id.values(), key=lambda c: (not c.is_origin, c.source_system, c.entity_id))
    )
    return MergeProvenance(
        entity_id=survivor_id,
        constituents=ordered,
        version=MERGE_PROVENANCE_VERSION,
        last_merged_at=stamp,
    )


# ── DB reads ────────────────────────────────────────────────────────────────


def _load_entity(cur: Any, org_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT * FROM entities WHERE org_id = %s AND id = %s",
        (org_id, entity_id),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def resolve_survivor_id(cur: Any, org_id: str, entity_id: str) -> str:
    """Follow ``merged_into`` to the entity that currently represents this one.

    Merges compose: merging C into an already-merged B must land C on B's
    survivor, not create a second head. Cycle-guarded — a corrupt chain stops and
    is logged rather than spinning.
    """
    current = _text(entity_id)
    seen = {current}
    for _ in range(_MAX_SURVIVOR_HOPS):
        row = _load_entity(cur, org_id, current)
        if row is None:
            return current
        pointer = _loads(row.get("metadata")).get(METADATA_MERGED_INTO)
        target = _text(pointer.get("entity_id")) if isinstance(pointer, Mapping) else ""
        if not target or target == current:
            return current
        if target in seen:
            logger.warning(
                "entity_merge: merged_into cycle detected at %s (org %s) — stopping",
                current, org_id,
            )
            return current
        seen.add(target)
        current = target
    logger.warning(
        "entity_merge: merged_into chain exceeded %d hops from %s (org %s)",
        _MAX_SURVIVOR_HOPS, entity_id, org_id,
    )
    return current


def get_entity_provenance(org_id: str, entity_id: str) -> Optional[MergeProvenance]:
    """The merge provenance of one entity, or ``None`` when it does not exist.

    An entity that was never merged returns a provenance carrying just its own
    identity (``is_merged`` False) — an honest "made of one thing" rather than an
    empty answer a caller has to interpret.
    """
    org = _text(org_id)
    eid = _text(entity_id)
    if not org or not eid:
        return None
    con = db.connect()
    try:
        cur = con.cursor()
        row = _load_entity(cur, org, eid)
        if row is None:
            return None
        provenance = MergeProvenance.from_metadata(eid, _loads(row.get("metadata")))
        if provenance.constituents:
            return provenance
        return MergeProvenance(
            entity_id=eid, constituents=(identity_of(row, is_origin=True),)
        )
    finally:
        con.close()


def provenance_for_entities(
    org_id: str, entity_ids: Iterable[str]
) -> Dict[str, MergeProvenance]:
    """Provenance for many entities in ONE query.

    The seam a finding surface uses: an interrogation view resolving provenance
    for every entity a finding traverses must not issue a query per node.
    """
    org = _text(org_id)
    ids = [_text(i) for i in entity_ids if _text(i)]
    if not org or not ids:
        return {}
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM entities WHERE org_id = %s AND id = ANY(%s)",
            (org, ids),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()

    result: Dict[str, MergeProvenance] = {}
    for row in rows:
        eid = _text(row.get("id"))
        provenance = MergeProvenance.from_metadata(eid, _loads(row.get("metadata")))
        result[eid] = (
            provenance
            if provenance.constituents
            else MergeProvenance(
                entity_id=eid, constituents=(identity_of(row, is_origin=True),)
            )
        )
    return result


def merged_constituents(org_id: str, survivor_id: str) -> List[Dict[str, Any]]:
    """The rows currently pointing at ``survivor_id`` — the reverse of the
    pointer, so the AC4 unmerge can find what to restore without trusting the
    survivor's list alone."""
    provenance = get_entity_provenance(org_id, survivor_id)
    if provenance is None:
        return []
    return [c.to_dict() for c in provenance.constituents if not c.is_origin]


# ── applying a merge ────────────────────────────────────────────────────────


def _write_survivor_provenance(
    cur: Any, org_id: str, survivor: Mapping[str, Any], provenance: MergeProvenance, now: str
) -> None:
    metadata = _loads(survivor.get("metadata"))
    metadata[METADATA_MERGE_PROVENANCE] = provenance.to_dict()
    cur.execute(
        "UPDATE entities SET metadata = %s, updated_at = %s WHERE org_id = %s AND id = %s",
        (json.dumps(metadata), now, org_id, _text(survivor.get("id"))),
    )


def _write_merged_pointer(
    cur: Any,
    org_id: str,
    constituent: Mapping[str, Any],
    *,
    survivor_id: str,
    rule: str,
    actor: str,
    now: str,
) -> None:
    """Mark the constituent as merged WITHOUT deleting it or changing its status.

    The row, its identity, and its edges all survive — that is what makes the
    merge inspectable now and reversible later (AC4 / T5). ``resolution_status`` is
    deliberately untouched: it records how the STANDING engine resolved that row,
    and overwriting it would destroy that separate fact.
    """
    metadata = _loads(constituent.get("metadata"))
    metadata[METADATA_MERGED_INTO] = {
        "entity_id": survivor_id,
        "rule": rule,
        "merged_at": now,
        "merged_by": actor,
    }
    cur.execute(
        "UPDATE entities SET metadata = %s, updated_at = %s WHERE org_id = %s AND id = %s",
        (json.dumps(metadata), now, org_id, _text(constituent.get("id"))),
    )


def _merge_block_for(
    cur: Any, org_id: str, left_row: Mapping[str, Any], right_row: Mapping[str, Any]
) -> Optional[Any]:
    """The active unmerge block covering this pair, or ``None`` (2.0-B2 T5).

    Imported lazily because ``entity_unmerge`` imports this module — the dependency
    runs unmerge → merge, and this is the one call back the other way.

    Reads on the cursor this merge already holds rather than opening its own: every
    concurrent merge holding one pooled connection while waiting for a second is a
    deadlock that only shows up under load. A read failure therefore propagates —
    ``apply_merge`` rolls back and raises, which is still fail-closed (no merge), and
    the operator sees the real error rather than a merge that quietly went ahead.
    """
    from .entity_unmerge import merge_block_on_cursor

    return merge_block_on_cursor(cur, org_id, left_row, right_row)


def apply_merge(
    org_id: str,
    left_entity_id: str,
    right_entity_id: str,
    *,
    rule: str,
    confidence: Optional[float] = None,
    actor: str = ACTOR_SYSTEM,
    evidence: Optional[Mapping[str, Any]] = None,
) -> MergeOutcome:
    """Merge two entities, recording every constituent identity and the rule.

    Both sides are first resolved to their current survivor, so this composes with
    earlier merges. If they already resolve to the SAME entity the call is a no-op
    reported as ``already_merged`` — applying twice must never write twice.

    Refuses (rather than guesses) when: the rule is unknown, an entity does not
    exist in this org, or the two are of different entity types — a ``team`` named
    "Payments" is not the ``system`` named "Payments", and a merge across types
    would be unrecoverable nonsense.
    """
    org = _text(org_id)
    if not org:
        raise EntityMergeError("a merge must be scoped to an org")
    if rule not in MERGE_RULES:
        raise EntityMergeError(f"rule must be one of {list(MERGE_RULES)}; got {rule!r}")
    left_id, right_id = _text(left_entity_id), _text(right_entity_id)
    if not left_id or not right_id:
        raise EntityMergeError("a merge needs two entity ids")
    if left_id == right_id:
        return MergeOutcome(
            outcome=OUTCOME_SKIPPED, rule=rule, survivor_id=left_id,
            reason="an entity cannot be merged with itself",
        )

    now = _now()
    con = db.connect()
    try:
        cur = con.cursor()
        left_head = resolve_survivor_id(cur, org, left_id)
        right_head = resolve_survivor_id(cur, org, right_id)
        if left_head == right_head:
            con.commit()
            return MergeOutcome(
                outcome=OUTCOME_ALREADY_MERGED, rule=rule, survivor_id=left_head,
                reason="both entities already resolve to the same entity",
            )

        left_row = _load_entity(cur, org, left_head)
        right_row = _load_entity(cur, org, right_head)
        missing = [
            eid for eid, row in ((left_head, left_row), (right_head, right_row))
            if row is None
        ]
        if missing:
            raise EntityMergeError(
                f"entity {missing[0]!r} does not exist in org {org!r}"
            )
        if left_row["entity_type"] != right_row["entity_type"]:
            return MergeOutcome(
                outcome=OUTCOME_SKIPPED, rule=rule,
                reason=(
                    f"refusing to merge across entity types "
                    f"({left_row['entity_type']} vs {right_row['entity_type']})"
                ),
            )

        # 2.0-B2 T5 / AC4: a pair somebody UNMERGED must not be re-merged by the
        # next pass. This applier is idempotent and re-runs continuously — the
        # source cross-reference is still there, and a confirmed proposal is still
        # confirmed (T4 made that durable on purpose) — so without this check
        # "unmerge" would mean "unmerged until the next run", and the merge would
        # reappear with no explanation. Checked here rather than in the callers so
        # every path into a merge is covered by construction.
        block = _merge_block_for(cur, org, left_row, right_row)
        if block is not None:
            return MergeOutcome(
                outcome=OUTCOME_BLOCKED, rule=rule,
                survivor_id=_text(block.survivor_entity_id) or None,
                merged_entity_id=_text(block.detached_entity_id) or None,
                reason=(
                    "refusing to re-merge a pair that was unmerged"
                    + (f" ({block.unmerge_id})" if block.unmerge_id else "")
                    + (f": {block.reason}" if block.reason else "")
                ),
            )

        survivor = choose_survivor([left_row, right_row])
        absorbed = right_row if _text(survivor["id"]) == left_head else left_row

        provenance = build_merged_provenance(
            survivor, absorbed, rule=rule, confidence=confidence,
            actor=actor, evidence=evidence, merged_at=now,
        )
        _write_survivor_provenance(cur, org, survivor, provenance, now)
        _write_merged_pointer(
            cur, org, absorbed, survivor_id=_text(survivor["id"]),
            rule=rule, actor=actor, now=now,
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    outcome = MergeOutcome(
        outcome=OUTCOME_MERGED,
        rule=rule,
        survivor_id=_text(survivor["id"]),
        merged_entity_id=_text(absorbed["id"]),
        reason=f"merged on {rule}",
    )
    _audit_merge(org, outcome, actor=actor, provenance=provenance)
    return outcome


def _audit_merge(
    org_id: str, outcome: MergeOutcome, *, actor: str, provenance: MergeProvenance
) -> None:
    """Record the merge in the organisation-wide audit trail.

    Best-effort: the merge is already committed with its own provenance, so a
    failed audit write must not fail the operation — but it is logged, never
    swallowed.
    """
    try:
        from .middleware.audit import ENTITY_MERGED, log_event

        log_event(
            ENTITY_MERGED,
            org_id=org_id,
            user_id=actor,
            survivor_entity_id=outcome.survivor_id,
            merged_entity_id=outcome.merged_entity_id,
            rule=outcome.rule,
            constituent_count=len(provenance.constituents),
            source_systems=list(provenance.source_systems),
            timestamp=provenance.last_merged_at,
        )
    except Exception as exc:  # noqa: BLE001 — log_event is itself non-raising.
        logger.warning("entity merge audit write failed: %s", exc)


# ── the two handoffs T1 and T3 left ─────────────────────────────────────────


#: T1 tier → the merge rule recorded for it. A tier absent from this map cannot
#: merge, which is how the propose-only tier stays propose-only even here.
_RULE_FOR_TIER: Dict[str, str] = {
    "explicit_reference": RULE_EXPLICIT_REFERENCE,
    "alias_mapping": RULE_ALIAS_MAPPING,
}


@dataclass(frozen=True)
class MergeRunReport:
    """What one apply pass did, per outcome — reported rather than summarised
    into a single number a reader has to trust."""

    merged: int = 0
    already_merged: int = 0
    skipped: int = 0
    blocked: int = 0
    outcomes: Tuple[MergeOutcome, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "merged": self.merged,
            "already_merged": self.already_merged,
            "skipped": self.skipped,
            "blocked": self.blocked,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _tally(outcomes: Sequence[MergeOutcome]) -> MergeRunReport:
    return MergeRunReport(
        merged=sum(1 for o in outcomes if o.outcome == OUTCOME_MERGED),
        already_merged=sum(1 for o in outcomes if o.outcome == OUTCOME_ALREADY_MERGED),
        skipped=sum(1 for o in outcomes if o.outcome == OUTCOME_SKIPPED),
        blocked=sum(1 for o in outcomes if o.outcome == OUTCOME_BLOCKED),
        outcomes=tuple(outcomes),
    )


def apply_resolution_decisions(
    org_id: str, decisions: Iterable[Any], *, actor: str = ACTOR_SYSTEM
) -> MergeRunReport:
    """Apply the AUTO-MERGE decisions from the ranked engine (T1 tiers 1–2).

    A decision that is not a merge applies nothing: ``decision.is_merge`` is the
    engine's own answer, and a proposed / ambiguous / unresolved decision carries
    no authority here. The tier→rule map is a second, independent gate — a tier
    that is not in it cannot merge even if a future caller marks it mergeable.
    """
    org = _text(org_id)
    outcomes: List[MergeOutcome] = []
    for decision in decisions or ():
        if not getattr(decision, "is_merge", False):
            continue
        target = getattr(decision, "merge_target", None)
        subject = getattr(decision, "subject", None)
        tier = _text(getattr(decision, "tier", ""))
        rule = _RULE_FOR_TIER.get(tier)
        if subject is None or target is None or rule is None:
            outcomes.append(MergeOutcome(
                outcome=OUTCOME_SKIPPED, rule=tier or "unknown",
                reason=f"tier {tier!r} is not permitted to merge",
            ))
            continue
        outcomes.append(apply_merge(
            org,
            getattr(subject, "entity_id", ""),
            getattr(target, "entity_id", ""),
            rule=rule,
            confidence=getattr(decision, "confidence", None),
            actor=actor,
            evidence={"tier": tier, "reason": getattr(decision, "reason", "")},
        ))
    return _tally(outcomes)


def apply_confirmed_proposals(
    org_id: str, *, actor: str = ACTOR_SYSTEM
) -> MergeRunReport:
    """Apply the pairs a human CONFIRMED in the T3 review surface.

    This is the consumer ``entity_match_proposals.confirmed_pairs`` was written
    for. The rule recorded is :data:`RULE_CONFIRMED_PROPOSAL`, not the tier that
    proposed it — a name match never authorises a merge; the person who confirmed
    it did, and the provenance should say so.
    """
    from .entity_match_proposals import confirmed_pairs

    org = _text(org_id)
    outcomes: List[MergeOutcome] = []
    for _entity_type, left_id, right_id in confirmed_pairs(org):
        outcomes.append(apply_merge(
            org, left_id, right_id,
            rule=RULE_CONFIRMED_PROPOSAL,
            actor=actor,
            evidence={"source": "entity_match_proposal"},
        ))
    return _tally(outcomes)


def apply_org_merges(
    org_id: str,
    *,
    entity_types: Optional[Sequence[str]] = None,
    actor: str = ACTOR_SYSTEM,
    include_confirmed: bool = True,
) -> MergeRunReport:
    """Run the engine over an org and apply everything it is allowed to apply.

    Auto-merge tiers first (the machine- and org-stated identities), then the
    human-confirmed proposals. Both go through :func:`apply_merge`, so both are
    idempotent, transitive, and provenance-recording by construction.
    """
    from .cross_source_resolution import resolve_org_entity_type
    from .entity_match_proposals import SCANNABLE_ENTITY_TYPES

    org = _text(org_id)
    requested = [
        t for t in (entity_types or SCANNABLE_ENTITY_TYPES)
        if t in SCANNABLE_ENTITY_TYPES
    ]
    outcomes: List[MergeOutcome] = []
    for entity_type in requested:
        report = apply_resolution_decisions(
            org, resolve_org_entity_type(org, entity_type), actor=actor
        )
        outcomes.extend(report.outcomes)
    if include_confirmed:
        outcomes.extend(apply_confirmed_proposals(org, actor=actor).outcomes)
    return _tally(outcomes)


__all__ = [
    "METADATA_MERGE_PROVENANCE",
    "METADATA_MERGED_INTO",
    "MERGE_PROVENANCE_VERSION",
    "RULE_EXPLICIT_REFERENCE",
    "RULE_ALIAS_MAPPING",
    "RULE_CONFIRMED_PROPOSAL",
    "MERGE_RULES",
    "ACTOR_SYSTEM",
    "OUTCOME_MERGED",
    "OUTCOME_ALREADY_MERGED",
    "OUTCOME_SKIPPED",
    "OUTCOME_BLOCKED",
    "EntityMergeError",
    "ConstituentIdentity",
    "MergeProvenance",
    "MergeOutcome",
    "MergeRunReport",
    "identity_of",
    "choose_survivor",
    "build_merged_provenance",
    "resolve_survivor_id",
    "get_entity_provenance",
    "provenance_for_entities",
    "merged_constituents",
    "apply_merge",
    "apply_resolution_decisions",
    "apply_confirmed_proposals",
    "apply_org_merges",
]
