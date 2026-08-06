"""2.0-B2 T5 — reverse a resolution, and flag what depended on it.

AC4: "Unmerge restores constituents and flags dependent findings for re-evaluation."

Restoring costs almost nothing, by T2's design: a merge never deleted anything. The
absorbed row, its identity, its edges and its own ``resolution_status`` all survive
a merge; what a merge writes is a ``merge_provenance`` block on the survivor and a
``merged_into`` pointer on the constituent. So the restore here is removing those
two marks — no resurrection, no reconstruction, nothing to get wrong.

Three things are genuinely hard, and this module exists for them.

**1. A reversal that the next run undoes is not a reversal.** The merge appliers are
idempotent and re-run continuously: ``apply_org_merges`` walks the auto-merge tiers
and every human-confirmed pair on every pass. The source data still carries the
cross-reference; the confirmed proposal is still confirmed (T4 made that answer
durable deliberately). So an unmerge that only removes the marks lasts until the
next pass, and the operator sees the merge "come back" with no explanation. Every
unmerge therefore records a BLOCK, and :func:`entity_merge.apply_merge` consults it
and refuses with a named reason. Releasing the block is a separate, deliberate act.

**2. A chain of merges must come apart at the right joint.** A→B→C is stored flat
on C's constituent list, but the *tree* survives in the pointers (A still points at
B; only the entity being absorbed ever has its pointer written). So detaching B from
C hands back B **with A still merged into it** — the sub-merge nobody asked to
reverse is left intact, and C's list loses exactly B and B's subtree. Deriving the
tree from the pointers rather than the flat list is what makes that exact.

**3. "Flags dependent findings" has to mean something checkable.** A finding depends
on this merge if it referenced the survivor (whose meaning just narrowed — it no
longer speaks for the detached source) or the detached entity (a separate thing
again). Findings are matched through their OWN entity references; a finding carrying
no entity linkage cannot be shown to depend on the merge, so it is **not** flagged
and is **counted** as unassessed. Flagging everything would be as useless as
flagging nothing, and quietly flagging nothing is worse than both.

The flag itself lives in :mod:`app.finding_reevaluation`, keyed on the stable
``opportunity_identity`` so it survives to the next run.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import db
from .entity_merge import (
    METADATA_MERGE_PROVENANCE,
    METADATA_MERGED_INTO,
    MergeProvenance,
    resolve_survivor_id,
)
from .finding_reevaluation import (
    REASON_ENTITY_UNMERGED,
    TRIGGER_ENTITY_UNMERGE,
    ensure_reevaluation_tables,
    flag_findings,
)
from database.models.entity_unmerges import ENTITY_UNMERGE_COLUMNS

logger = logging.getLogger(__name__)

#: Recorded on a restored entity so its row shows the merge it was released from.
#: Read by nobody in the merge path — ``merged_into`` is what resolution follows —
#: so this is history, not state.
METADATA_UNMERGED_FROM = "unmerged_from"

STATUS_BLOCKED = "blocked"
STATUS_RELEASED = "released"

#: The two ways a pair is named, and what each is for — see the model docstring in
#: ``database/models/entity_unmerges.py``. Both are recorded per unmerge and either
#: one matching blocks the merge.
PAIR_KEY_ROWS = "entity_rows"
PAIR_KEY_IDENTITY = "source_identity"

OUTCOME_UNMERGED = "unmerged"
OUTCOME_NOT_MERGED = "not_merged"

ACTOR_SYSTEM = "system"

#: How many of the org's recent runs the dependency sweep reads. A finding's entity
#: references live in run-scoped storage, so the sweep is bounded — and whatever the
#: bound leaves unread is REPORTED (``runs_truncated``), never silently dropped.
DEFAULT_MAX_RUNS_SCANNED = 25

#: An unmerge splits one constituent at a time; a full split loops. The cap stops a
#: corrupt provenance list turning one request into an unbounded write.
MAX_CONSTITUENTS_PER_SPLIT = 100


class EntityUnmergeError(ValueError):
    """An unmerge could not be performed as asked."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


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


def _canonical(value: Any) -> str:
    """The same normalisation the entity layer uses, so a pair key built here and a
    name stored there cannot disagree about what a name is."""
    from .entity_resolution import canonical_name_for

    return canonical_name_for(_text(value))


# ── the pair keys ───────────────────────────────────────────────────────────


def row_pair_key(left_entity_id: str, right_entity_id: str) -> str:
    """The exact key: the two entity ROW ids, order-independent.

    The only key that is correct when two genuinely different entities share a
    name. Breaks when the rows churn, which is what the identity key covers.
    """
    left, right = _text(left_entity_id), _text(right_entity_id)
    if not left or not right:
        raise EntityUnmergeError("a pair key needs two entity ids")
    if left == right:
        raise EntityUnmergeError("an entity cannot form a pair with itself")
    a, b = sorted((left, right))
    return f"rows:{a}|{b}"


def identity_pair_key(left: Mapping[str, Any], right: Mapping[str, Any]) -> Optional[str]:
    """The churn-resistant key: each side's ``source_system`` + canonical name.

    ``None`` when either side cannot supply both parts — a partial key would match
    the wrong pair, and blocking the wrong pair is worse than not blocking this one
    (the row key still covers the common case).
    """
    def _side(row: Mapping[str, Any]) -> Optional[str]:
        system = _text(row.get("source_system")).lower()
        name = _canonical(row.get("canonical_name") or row.get("display_name"))
        return f"{system}|{name}" if system and name else None

    a, b = _side(left), _side(right)
    if not a or not b or a == b:
        # Equal sides mean one identity, which is not a pair — and would block every
        # merge of that identity with anything.
        return None
    lo, hi = sorted((a, b))
    return f"ident:{lo}|{hi}"


def pair_keys_for(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> List[Tuple[str, str]]:
    """Every ``(kind, key)`` naming this pair — recorded on unmerge, checked on merge."""
    keys: List[Tuple[str, str]] = [(
        PAIR_KEY_ROWS,
        row_pair_key(_text(left.get("id")), _text(right.get("id"))),
    )]
    identity = identity_pair_key(left, right)
    if identity:
        keys.append((PAIR_KEY_IDENTITY, identity))
    return keys


# ── reading the merge tree ──────────────────────────────────────────────────


def _load_entity(cur: Any, org_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT * FROM entities WHERE org_id = %s AND id = %s", (org_id, entity_id)
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _load_entities(cur: Any, org_id: str, entity_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    ids = [i for i in {_text(e) for e in entity_ids} if i]
    if not ids:
        return {}
    cur.execute(
        "SELECT * FROM entities WHERE org_id = %s AND id = ANY(%s)", (org_id, ids)
    )
    return {_text(r["id"]): dict(r) for r in (cur.fetchall() or [])}


def _merged_into_id(row: Mapping[str, Any]) -> str:
    pointer = _loads(row.get("metadata")).get(METADATA_MERGED_INTO)
    return _text(pointer.get("entity_id")) if isinstance(pointer, Mapping) else ""


def _merge_rule(row: Mapping[str, Any]) -> Optional[str]:
    pointer = _loads(row.get("metadata")).get(METADATA_MERGED_INTO)
    return _text(pointer.get("rule")) or None if isinstance(pointer, Mapping) else None


def detached_subtree(
    detached_id: str, rows_by_id: Mapping[str, Mapping[str, Any]]
) -> List[str]:
    """``detached_id`` plus every constituent that reaches the survivor THROUGH it.

    The flat constituent list cannot answer this; the ``merged_into`` pointers can,
    because only the entity being absorbed ever has its pointer written, so a
    sub-merge's pointer still names its own local survivor. Getting this right is
    what leaves an untouched sub-merge intact when a chain is split.
    """
    target = _text(detached_id)
    if not target:
        return []
    subtree = {target}
    # Iterate to a fixed point: a child may be listed before its parent.
    for _ in range(len(rows_by_id) + 1):
        grew = False
        for eid, row in rows_by_id.items():
            if eid in subtree:
                continue
            if _merged_into_id(row) in subtree:
                subtree.add(eid)
                grew = True
        if not grew:
            break
    return sorted(subtree)


# ── the merge block ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MergeBlock:
    """A recorded refusal to re-merge a pair."""

    org_id: str
    pair_key: str
    pair_key_kind: str
    unmerge_id: str
    status: str
    survivor_entity_id: str
    detached_entity_id: str
    entity_type: str
    previous_rule: Optional[str]
    restored_entity_ids: Tuple[str, ...]
    flagged_finding_count: int
    unlinked_finding_count: int
    reason: Optional[str]
    actor_id: str
    created_at: Optional[str]
    released_by: Optional[str] = None
    released_at: Optional[str] = None
    release_reason: Optional[str] = None

    @property
    def is_blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unmergeId": self.unmerge_id,
            "pairKey": self.pair_key,
            "pairKeyKind": self.pair_key_kind,
            "status": self.status,
            "survivorEntityId": self.survivor_entity_id,
            "detachedEntityId": self.detached_entity_id,
            "entityType": self.entity_type,
            "previousRule": self.previous_rule,
            "restoredEntityIds": list(self.restored_entity_ids),
            "flaggedFindingCount": self.flagged_finding_count,
            "unlinkedFindingCount": self.unlinked_finding_count,
            "reason": self.reason,
            "actorId": self.actor_id,
            "createdAt": self.created_at,
            "releasedBy": self.released_by,
            "releasedAt": self.released_at,
            "releaseReason": self.release_reason,
        }


def _row_to_block(row: Sequence[Any]) -> MergeBlock:
    restored = row[9]
    if isinstance(restored, str):
        try:
            parsed = json.loads(restored)
        except Exception:  # noqa: BLE001
            parsed = []
    else:
        parsed = restored or []
    return MergeBlock(
        org_id=_text(row[0]),
        pair_key=_text(row[1]),
        unmerge_id=_text(row[2]),
        pair_key_kind=_text(row[3]),
        status=_text(row[4]),
        survivor_entity_id=_text(row[5]),
        detached_entity_id=_text(row[6]),
        entity_type=_text(row[7]),
        previous_rule=row[8],
        restored_entity_ids=tuple(_text(v) for v in parsed if _text(v)),
        flagged_finding_count=int(row[10] or 0),
        unlinked_finding_count=int(row[11] or 0),
        reason=row[12],
        actor_id=_text(row[13]),
        created_at=_iso(row[14]),
        released_by=row[15],
        released_at=_iso(row[16]),
        release_reason=row[17],
    )


_BLOCK_SELECT = f"SELECT {', '.join(ENTITY_UNMERGE_COLUMNS)} FROM entity_unmerges"


def _blocked_for_keys(cur: Any, org_id: str, keys: Sequence[str]) -> Optional[MergeBlock]:
    wanted = [k for k in {_text(k) for k in keys} if k]
    if not wanted:
        return None
    cur.execute(
        f"{_BLOCK_SELECT} WHERE org_id = %s AND status = %s AND pair_key = ANY(%s) "
        "ORDER BY created_at DESC LIMIT 1",
        (org_id, STATUS_BLOCKED, wanted),
    )
    row = cur.fetchone()
    return _row_to_block(row) if row is not None else None


def merge_block_on_cursor(
    cur: Any, org_id: str, left: Mapping[str, Any], right: Mapping[str, Any]
) -> Optional[MergeBlock]:
    """The active block covering this pair, read on a cursor the CALLER owns.

    This is the form ``entity_merge.apply_merge`` uses, and it takes the cursor for
    a specific reason: ``apply_merge`` already holds a pooled connection for the
    whole merge, so opening a second one here would mean every concurrent merge
    holding one connection while waiting for another — a pool-exhaustion deadlock
    that only appears under load.

    A read failure is NOT caught here: on PostgreSQL a failed statement aborts the
    surrounding transaction, so pretending to have an answer would be worse than
    useless. ``apply_merge`` rolls back and raises, which is still fail-closed — no
    merge happens — and the operator sees the real error.
    """
    org = _text(org_id)
    if not org:
        return None
    try:
        keys = [key for _kind, key in pair_keys_for(left, right)]
    except EntityUnmergeError:
        return None
    return _blocked_for_keys(cur, org, keys)


def merge_block_for(
    org_id: str, left: Mapping[str, Any], right: Mapping[str, Any]
) -> Optional[MergeBlock]:
    """The active block covering this pair, on its own connection.

    The standalone read (a caller asking "would this merge be refused?") rather than
    the one on the merge path — see :func:`merge_block_on_cursor` for that. Takes the
    two entity ROWS rather than ids because the identity key is built from their
    source system and name.
    """
    org = _text(org_id)
    if not org:
        return None
    try:
        keys = [key for _kind, key in pair_keys_for(left, right)]
    except EntityUnmergeError:
        return None
    try:
        ensure_reevaluation_tables()
        with closing(db.connect()) as con:
            cur = con.cursor()
            return _blocked_for_keys(cur, org, keys)
    except Exception as exc:  # noqa: BLE001
        # A block that cannot be read must not silently permit the merge it exists
        # to prevent: fail closed by reporting a synthetic block, and say why.
        logger.error(
            "entity_unmerge: merge-block lookup failed for org %s (%s) — refusing the "
            "merge rather than risking re-merging an unmerged pair",
            org, exc,
        )
        return MergeBlock(
            org_id=org,
            pair_key=keys[0] if keys else "",
            pair_key_kind=PAIR_KEY_ROWS,
            unmerge_id="",
            status=STATUS_BLOCKED,
            survivor_entity_id=_text(left.get("id")),
            detached_entity_id=_text(right.get("id")),
            entity_type=_text(left.get("entity_type")),
            previous_rule=None,
            restored_entity_ids=(),
            flagged_finding_count=0,
            unlinked_finding_count=0,
            reason=f"unmerge block state unreadable: {exc}",
            actor_id=ACTOR_SYSTEM,
            created_at=None,
        )


# ── dependent findings ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class DependencySweep:
    """Which findings referenced the affected entities — and what could not be told.

    ``unlinked`` is the honest part: a finding carrying no entity references cannot
    be shown to depend on the merge. It is neither flagged nor forgotten.
    """

    identities: Tuple[str, ...] = ()
    run_ids: Dict[str, str] = field(default_factory=dict)
    findings_examined: int = 0
    unlinked: int = 0
    runs_scanned: int = 0
    runs_truncated: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identities": list(self.identities),
            "findingsExamined": self.findings_examined,
            "dependentFindings": len(self.identities),
            "unlinkedFindings": self.unlinked,
            "runsScanned": self.runs_scanned,
            "runsTruncated": self.runs_truncated,
        }


def _entity_ids_of_opp(opp: Mapping[str, Any]) -> Set[str]:
    """The entity ids a finding itself references.

    Reads the same keys ``llm_enrichment._explicit_opp_entity_ids`` reads, plus the
    per-entity ``entity_id``/``id`` shapes an entity SUMMARY carries, so the link is
    the one the enrichment layer already understands rather than a second guess.
    """
    found: Set[str] = set()
    for key in ("entity_ids", "entityIds", "entities"):
        value = opp.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                if _text(item):
                    found.add(_text(item))
            elif isinstance(item, Mapping):
                candidate = _text(item.get("entity_id") or item.get("id"))
                if candidate:
                    found.add(candidate)
    return found


def _enrichment_entity_ids(run_id: str) -> Dict[str, Set[str]]:
    """Per-opportunity entity ids from the run's enrichment artifact, if present."""
    try:
        enrichment = db.run_kv_get("llm_enrichment", run_id, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("unmerge sweep: enrichment read failed for %s: %s", run_id, exc)
        return {}
    per_opp = enrichment.get("perOpportunity") if isinstance(enrichment, Mapping) else None
    if not isinstance(per_opp, Mapping):
        return {}
    result: Dict[str, Set[str]] = {}
    for opp_id, payload in per_opp.items():
        if isinstance(payload, Mapping):
            ids = _entity_ids_of_opp(payload)
            if ids:
                result[_text(opp_id)] = ids
    return result


def dependent_findings(
    org_id: str,
    entity_ids: Iterable[str],
    *,
    max_runs: int = DEFAULT_MAX_RUNS_SCANNED,
) -> DependencySweep:
    """Findings that referenced any of ``entity_ids``, as stable identities.

    Bounded to the org's most recent runs, because a finding's entity references
    live in run-scoped storage and an unbounded sweep would read every run an org
    has ever had. The bound is reported, not silent.
    """
    org = _text(org_id)
    affected = {_text(e) for e in (entity_ids or ()) if _text(e)}
    if not org or not affected:
        return DependencySweep()

    try:
        from .opportunity_instances import get_instances_for_run
    except Exception as exc:  # noqa: BLE001
        logger.warning("unmerge sweep: opportunity instances unavailable: %s", exc)
        return DependencySweep()

    try:
        runs = db.tenancy_get_runs(org) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("unmerge sweep: run listing failed for org %s: %s", org, exc)
        return DependencySweep()

    ordered = sorted(
        (r for r in runs if _text(r.get("id"))),
        key=lambda r: (r.get("seq") or 0, _text(r.get("id"))),
        reverse=True,
    )
    cap = max(1, int(max_runs or DEFAULT_MAX_RUNS_SCANNED))
    scanned, truncated = ordered[:cap], max(0, len(ordered) - cap)

    identities: List[str] = []
    seen: Set[str] = set()
    run_for_identity: Dict[str, str] = {}
    examined = 0
    unlinked = 0

    for run in scanned:
        run_id = _text(run.get("id"))
        try:
            opps = db.run_kv_get("opps", run_id, []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("unmerge sweep: opps read failed for %s: %s", run_id, exc)
            continue
        if not isinstance(opps, list) or not opps:
            continue

        enrichment_ids = _enrichment_entity_ids(run_id)
        # The instance rows are the only place the STABLE identity is stored for a
        # run's opportunities when the served opp was written before identities were
        # stamped; the opp's own stamp wins when present.
        identity_by_ref: Dict[str, str] = {}
        try:
            for instance in get_instances_for_run(run_id, org_id=org):
                ref = _text(getattr(instance, "opportunity_ref", ""))
                if ref:
                    identity_by_ref[ref] = _text(instance.opportunity_identity)
        except Exception as exc:  # noqa: BLE001
            logger.debug("unmerge sweep: instances read failed for %s: %s", run_id, exc)

        for opp in opps:
            if not isinstance(opp, Mapping):
                continue
            examined += 1
            opp_id = _text(opp.get("id"))
            linked = _entity_ids_of_opp(opp) | enrichment_ids.get(opp_id, set())
            if not linked:
                unlinked += 1
                continue
            if not (linked & affected):
                continue
            identity = _text(opp.get("opportunity_identity")) or identity_by_ref.get(opp_id, "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            identities.append(identity)
            run_for_identity[identity] = run_id

    if truncated:
        logger.warning(
            "unmerge sweep for org %s read the %d most recent runs and left %d "
            "unread; findings only present in older runs are not flagged",
            org, len(scanned), truncated,
        )
    return DependencySweep(
        identities=tuple(identities),
        run_ids=run_for_identity,
        findings_examined=examined,
        unlinked=unlinked,
        runs_scanned=len(scanned),
        runs_truncated=truncated,
    )


# ── performing the unmerge ──────────────────────────────────────────────────


@dataclass(frozen=True)
class UnmergeOutcome:
    """The result of one unmerge attempt — always reported, never inferred."""

    outcome: str
    survivor_entity_id: Optional[str] = None
    detached_entity_id: Optional[str] = None
    unmerge_id: Optional[str] = None
    previous_rule: Optional[str] = None
    restored_entity_ids: Tuple[str, ...] = ()
    remaining_constituents: int = 0
    sweep: Optional[DependencySweep] = None
    flagged_findings: int = 0
    reason: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome == OUTCOME_UNMERGED

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "outcome": self.outcome,
            "survivorEntityId": self.survivor_entity_id,
            "detachedEntityId": self.detached_entity_id,
            "unmergeId": self.unmerge_id,
            "previousRule": self.previous_rule,
            "restoredEntityIds": list(self.restored_entity_ids),
            "remainingConstituents": self.remaining_constituents,
            "flaggedFindings": self.flagged_findings,
            "reason": self.reason,
        }
        if self.sweep is not None:
            payload["dependencySweep"] = self.sweep.to_dict()
        return payload


def _write_metadata(cur: Any, org_id: str, entity_id: str, metadata: Mapping[str, Any], now: datetime) -> None:
    cur.execute(
        "UPDATE entities SET metadata = %s, updated_at = %s WHERE org_id = %s AND id = %s",
        (json.dumps(dict(metadata)), now, org_id, entity_id),
    )


def _restore_constituent(
    cur: Any,
    org_id: str,
    row: Mapping[str, Any],
    *,
    survivor_id: str,
    unmerge_id: str,
    actor: str,
    now: datetime,
) -> None:
    """Give one row its independence back.

    Removing ``merged_into`` is the whole restoration — resolution follows that
    pointer and nothing else, so the row is immediately addressable again with its
    own identity, edges, status and (if it was itself a survivor) its own
    constituents intact. ``unmerged_from`` is left behind as history: it records
    what the row was released from without taking part in resolution.
    """
    metadata = _loads(row.get("metadata"))
    previous = metadata.pop(METADATA_MERGED_INTO, None)
    metadata[METADATA_UNMERGED_FROM] = {
        "entity_id": survivor_id,
        "rule": (previous or {}).get("rule") if isinstance(previous, Mapping) else None,
        "merged_at": (previous or {}).get("merged_at") if isinstance(previous, Mapping) else None,
        "unmerge_id": unmerge_id,
        "unmerged_at": _iso(now),
        "unmerged_by": actor,
    }
    _write_metadata(cur, org_id, _text(row.get("id")), metadata, now)


def _reduce_survivor_provenance(
    cur: Any,
    org_id: str,
    survivor: Mapping[str, Any],
    removed_ids: Set[str],
    now: datetime,
) -> int:
    """Drop the detached subtree from the survivor's constituent list.

    When nothing merged-in remains, the ``merge_provenance`` block is removed
    outright rather than left as an empty shell: the entity is genuinely made of one
    thing again, and :func:`entity_merge.get_entity_provenance` already reports that
    honestly for an entity that was never merged. The record that a merge once
    existed lives in the unmerge log and the audit trail, which is where history
    belongs — keeping a stale ``last_merged_at`` on a no-longer-merged entity would
    be a small, quiet lie.
    """
    survivor_id = _text(survivor.get("id"))
    metadata = _loads(survivor.get("metadata"))
    provenance = MergeProvenance.from_metadata(survivor_id, metadata)
    kept = [c for c in provenance.constituents if c.entity_id not in removed_ids]
    remaining = [c for c in kept if not c.is_origin]

    if remaining:
        block = MergeProvenance(
            entity_id=survivor_id,
            constituents=tuple(kept),
            version=provenance.version,
            last_merged_at=provenance.last_merged_at,
        ).to_dict()
        metadata[METADATA_MERGE_PROVENANCE] = block
    else:
        metadata.pop(METADATA_MERGE_PROVENANCE, None)
    _write_metadata(cur, org_id, survivor_id, metadata, now)
    return len(remaining)


def _record_blocks(
    cur: Any,
    org_id: str,
    *,
    unmerge_id: str,
    survivor: Mapping[str, Any],
    detached: Mapping[str, Any],
    previous_rule: Optional[str],
    restored_ids: Sequence[str],
    flagged: int,
    unlinked: int,
    reason: Optional[str],
    actor: str,
    now: datetime,
) -> List[str]:
    """Record the suppression, under every key that names this pair.

    ``ON CONFLICT`` re-blocks: a pair unmerged, released, and unmerged again is one
    row per key whose newest unmerge wins — the intermediate release survives in the
    audit trail rather than in a second row nothing would ever read.
    """
    written: List[str] = []
    payload = json.dumps(list(restored_ids))
    for kind, key in pair_keys_for(survivor, detached):
        cur.execute(
            """
            INSERT INTO entity_unmerges (
                org_id, pair_key, unmerge_id, pair_key_kind, status,
                survivor_entity_id, detached_entity_id, entity_type, previous_rule,
                restored_entity_ids, flagged_finding_count, unlinked_finding_count,
                reason, actor_id, created_at, released_by, released_at, release_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      NULL, NULL, NULL)
            ON CONFLICT (org_id, pair_key) DO UPDATE SET
                unmerge_id = EXCLUDED.unmerge_id,
                pair_key_kind = EXCLUDED.pair_key_kind,
                status = EXCLUDED.status,
                survivor_entity_id = EXCLUDED.survivor_entity_id,
                detached_entity_id = EXCLUDED.detached_entity_id,
                entity_type = EXCLUDED.entity_type,
                previous_rule = EXCLUDED.previous_rule,
                restored_entity_ids = EXCLUDED.restored_entity_ids,
                flagged_finding_count = EXCLUDED.flagged_finding_count,
                unlinked_finding_count = EXCLUDED.unlinked_finding_count,
                reason = EXCLUDED.reason,
                actor_id = EXCLUDED.actor_id,
                created_at = EXCLUDED.created_at,
                released_by = NULL,
                released_at = NULL,
                release_reason = NULL
            """,
            (
                org_id, key, unmerge_id, kind, STATUS_BLOCKED,
                _text(survivor.get("id")), _text(detached.get("id")),
                _text(survivor.get("entity_type")), previous_rule,
                payload, flagged, unlinked, reason, actor, now,
            ),
        )
        written.append(key)
    return written


def _update_block_counts(
    org_id: str, unmerge_id: str, *, flagged: int, unlinked: int
) -> None:
    """Record what the sweep found on the already-written block rows.

    Best-effort by design: the block itself is committed with the restore, so a
    failure here loses only the reported counts — which the returned outcome and the
    audit event also carry. Logged rather than swallowed.
    """
    if not flagged and not unlinked:
        return
    try:
        with closing(db.connect()) as con:
            cur = con.cursor()
            cur.execute(
                "UPDATE entity_unmerges SET flagged_finding_count = %s, "
                "unlinked_finding_count = %s WHERE org_id = %s AND unmerge_id = %s",
                (flagged, unlinked, org_id, unmerge_id),
            )
            con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "unmerge %s: recording dependent-finding counts failed: %s",
            unmerge_id, exc,
        )


def unmerge_entity(
    org_id: str,
    entity_id: str,
    *,
    actor: str = ACTOR_SYSTEM,
    reason: Optional[str] = None,
    flag_dependents: bool = True,
    max_runs: int = DEFAULT_MAX_RUNS_SCANNED,
) -> UnmergeOutcome:
    """Detach ``entity_id`` from the entity it was merged into (AC4).

    Restores the constituent (and leaves any sub-merge it contains intact), removes
    it from the survivor's constituent list, records the block that stops the next
    run re-merging it, and flags every dependent finding for re-evaluation.

    Refuses rather than guesses when the entity does not exist in this org. An
    entity that is not merged is reported as ``not_merged`` — that is a truthful
    answer to "unmerge this", not an error.
    """
    org = _text(org_id)
    target = _text(entity_id)
    if not org:
        raise EntityUnmergeError("an unmerge must be scoped to an org")
    if not target:
        raise EntityUnmergeError("an unmerge needs an entity id")

    ensure_reevaluation_tables()
    now = _now()
    unmerge_id = f"unm_{uuid.uuid4().hex[:20]}"
    actor_id = _text(actor) or ACTOR_SYSTEM

    with closing(db.connect()) as con:
        try:
            cur = con.cursor()
            row = _load_entity(cur, org, target)
            if row is None:
                raise EntityUnmergeError(
                    f"entity {target!r} does not exist in org {org!r}"
                )

            survivor_id = _merged_into_id(row)
            if not survivor_id:
                return UnmergeOutcome(
                    outcome=OUTCOME_NOT_MERGED,
                    detached_entity_id=target,
                    reason="entity is not merged into another entity",
                )
            # The pointer may name an intermediate that has itself been merged on.
            head_id = resolve_survivor_id(cur, org, survivor_id)
            survivor = _load_entity(cur, org, head_id)
            if survivor is None:
                # A pointer to a row that is not there. No code path deletes an
                # entity (a T2 guard pins that), so this needs manual surgery to
                # reach — but refusing would leave the constituent permanently
                # merged into a ghost, with the one action that helps denied. So it
                # is restored, loudly. No block is recorded: there is no pair to
                # block, and ``apply_merge`` already refuses a missing entity.
                logger.warning(
                    "unmerge %s: entity %s points at missing survivor %s (org %s) — "
                    "restoring it and recording no re-merge block",
                    unmerge_id, target, head_id, org,
                )
                _restore_constituent(
                    cur, org, row, survivor_id=head_id, unmerge_id=unmerge_id,
                    actor=actor_id, now=now,
                )
                con.commit()
                return UnmergeOutcome(
                    outcome=OUTCOME_UNMERGED,
                    survivor_entity_id=head_id,
                    detached_entity_id=target,
                    unmerge_id=unmerge_id,
                    previous_rule=_merge_rule(row),
                    restored_entity_ids=(target,),
                    reason=f"restored from missing survivor {head_id}",
                )

            previous_rule = _merge_rule(row)
            provenance = MergeProvenance.from_metadata(
                head_id, _loads(survivor.get("metadata"))
            )
            constituent_ids = [c.entity_id for c in provenance.constituents]
            rows_by_id = _load_entities(cur, org, constituent_ids + [target])
            subtree = detached_subtree(target, rows_by_id)

            # Only the detached entity's own pointer is cleared. Every other member
            # of the subtree keeps its local ``merged_into`` — its merge is not the
            # one being reversed — which is precisely how the untouched sub-merge
            # survives and follows the detached entity out.
            _restore_constituent(
                cur, org, row, survivor_id=head_id, unmerge_id=unmerge_id,
                actor=actor_id, now=now,
            )
            remaining = _reduce_survivor_provenance(cur, org, survivor, set(subtree), now)
            # The block is written in the SAME transaction as the restore, because the
            # two are one act: a restore that commits without its block leaves a pair
            # the next merge pass will silently rejoin. The dependent-finding counts
            # are not known yet (the sweep reads run-scoped storage, which needs its
            # own connections and must not run inside this transaction), so they are
            # filled in afterwards — losing a COUNT is cosmetic, losing the BLOCK is
            # not.
            _record_blocks(
                cur, org,
                unmerge_id=unmerge_id,
                survivor={"id": head_id, "entity_type": survivor.get("entity_type"),
                          "source_system": survivor.get("source_system"),
                          "canonical_name": survivor.get("canonical_name"),
                          "display_name": survivor.get("display_name")},
                detached={"id": target, "source_system": row.get("source_system"),
                          "canonical_name": row.get("canonical_name"),
                          "display_name": row.get("display_name")},
                previous_rule=previous_rule,
                restored_ids=subtree,
                flagged=0,
                unlinked=0,
                reason=_text(reason) or None,
                actor=actor_id,
                now=now,
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    # The graph is restored and the block is in place. The sweep runs afterwards: it
    # reads run-scoped storage, and a sweep failure must not undo a completed reversal.
    sweep = DependencySweep()
    flagged = 0
    if flag_dependents:
        try:
            sweep = dependent_findings(org, [head_id, *subtree], max_runs=max_runs)
            if sweep.identities:
                report = flag_findings(
                    org,
                    sweep.identities,
                    reason=REASON_ENTITY_UNMERGED,
                    trigger_kind=TRIGGER_ENTITY_UNMERGE,
                    trigger_ref=unmerge_id,
                    entity_ids=[head_id, *subtree],
                    run_ids=sweep.run_ids,
                    actor=actor_id,
                )
                flagged = report.total
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "unmerge %s restored entities but could not flag dependents: %s",
                unmerge_id, exc,
            )

    _update_block_counts(org, unmerge_id, flagged=flagged, unlinked=sweep.unlinked)

    outcome = UnmergeOutcome(
        outcome=OUTCOME_UNMERGED,
        survivor_entity_id=head_id,
        detached_entity_id=target,
        unmerge_id=unmerge_id,
        previous_rule=previous_rule,
        restored_entity_ids=tuple(subtree),
        remaining_constituents=remaining,
        sweep=sweep,
        flagged_findings=flagged,
        reason=f"detached from {head_id}",
    )
    _audit_unmerge(org, outcome, actor=actor_id, reason=_text(reason) or None)
    return outcome


def unmerge_all(
    org_id: str,
    survivor_entity_id: str,
    *,
    actor: str = ACTOR_SYSTEM,
    reason: Optional[str] = None,
    max_runs: int = DEFAULT_MAX_RUNS_SCANNED,
) -> List[UnmergeOutcome]:
    """Undo the merges INTO this entity, one at a time.

    Deliberately a loop over :func:`unmerge_entity` rather than a bulk path: each
    constituent gets its own block, its own audit event, and its own restored
    subtree, so a full split is inspectable as the set of reversals it actually is.

    "Completely" means the merges **this entity** performed — its DIRECT
    constituents, the ones whose pointer names it. A nested sub-merge is left intact,
    for consistency with :func:`unmerge_entity`: the flat constituent list would also
    offer the members of a chain (``git → jira → sn`` lists both under ``sn``), and
    splitting those too would reverse a merge nobody asked about and block a pair
    nobody separated. Undoing a sub-merge is a second, explicit call.
    """
    org = _text(org_id)
    survivor_id = _text(survivor_entity_id)
    if not org or not survivor_id:
        raise EntityUnmergeError("a full unmerge needs an org and a survivor id")

    from .entity_merge import get_entity_provenance

    provenance = get_entity_provenance(org, survivor_id)
    if provenance is None:
        raise EntityUnmergeError(
            f"entity {survivor_id!r} does not exist in org {org!r}"
        )
    listed = [c.entity_id for c in provenance.constituents if not c.is_origin]
    with closing(db.connect()) as con:
        rows = _load_entities(con.cursor(), org, listed)
    targets = [eid for eid in listed if _merged_into_id(rows.get(eid, {})) == survivor_id]
    if len(targets) > MAX_CONSTITUENTS_PER_SPLIT:
        raise EntityUnmergeError(
            f"entity {survivor_id!r} lists {len(targets)} constituents, above the "
            f"{MAX_CONSTITUENTS_PER_SPLIT} per-split cap"
        )

    outcomes: List[UnmergeOutcome] = []
    for target in targets:
        outcomes.append(
            unmerge_entity(
                org, target, actor=actor, reason=reason, max_runs=max_runs
            )
        )
    return outcomes


def release_merge_block(
    org_id: str,
    unmerge_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
) -> int:
    """Allow a previously-unmerged pair to merge again.

    Separate and deliberate, because it re-permits AUTOMATIC merging of a pair a
    person had corrected — and it is somebody undoing somebody else's correction.
    Nothing is deleted: the row keeps its unmerge record and gains who released it.

    Returns the number of block keys released (0 when the id is unknown to this org
    — which a caller should treat as "not found", never as success).
    """
    org, uid = _text(org_id), _text(unmerge_id)
    actor_id = _text(actor)
    if not org or not uid:
        raise EntityUnmergeError("releasing a block needs an org and an unmerge id")
    if not actor_id:
        raise EntityUnmergeError("releasing a block must record who did it")

    ensure_reevaluation_tables()
    now = _now()
    with closing(db.connect()) as con:
        try:
            cur = con.cursor()
            cur.execute(
                """
                UPDATE entity_unmerges
                   SET status = %s, released_by = %s, released_at = %s, release_reason = %s
                 WHERE org_id = %s AND unmerge_id = %s AND status = %s
             RETURNING pair_key
                """,
                (STATUS_RELEASED, actor_id, now, _text(reason) or None, org, uid,
                 STATUS_BLOCKED),
            )
            released = [_text(r[0]) for r in (cur.fetchall() or [])]
            con.commit()
        except Exception:
            con.rollback()
            raise

    if released:
        _audit_release(org, uid, released, actor=actor_id, reason=_text(reason) or None)
    return len(released)


def list_unmerges(
    org_id: str, *, status: Optional[str] = None, limit: int = 100
) -> List[MergeBlock]:
    """One org's unmerges, newest first, de-duplicated to one row per action.

    Each unmerge writes one row per pair key; the log reads as ACTIONS, so the
    row-id key is the representative and the identity key is an implementation
    detail of the block rather than a second event.
    """
    org = _text(org_id)
    if not org:
        return []
    ensure_reevaluation_tables()
    sql = f"{_BLOCK_SELECT} WHERE org_id = %s"
    params: List[Any] = [org]
    if status is not None:
        sql += " AND status = %s"
        params.append(_text(status))
    sql += " ORDER BY created_at DESC, pair_key ASC LIMIT %s"
    params.append(max(1, min(int(limit or 100), 1000)) * 2)

    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() or []

    blocks: List[MergeBlock] = []
    seen: Set[str] = set()
    for row in rows:
        block = _row_to_block(row)
        if block.unmerge_id and block.unmerge_id in seen:
            continue
        if block.unmerge_id:
            seen.add(block.unmerge_id)
        blocks.append(block)
    return blocks[: max(1, min(int(limit or 100), 1000))]


# ── audit ───────────────────────────────────────────────────────────────────


def _audit_unmerge(
    org_id: str, outcome: UnmergeOutcome, *, actor: str, reason: Optional[str]
) -> None:
    """Record the unmerge in the org-wide audit trail.

    Best-effort: the restore is already committed, so a failed audit write must not
    fail the operation — but it is logged, never swallowed.
    """
    try:
        from .middleware.audit import ENTITY_UNMERGED, log_event

        log_event(
            ENTITY_UNMERGED,
            org_id=org_id,
            user_id=actor,
            unmerge_id=outcome.unmerge_id,
            survivor_entity_id=outcome.survivor_entity_id,
            detached_entity_id=outcome.detached_entity_id,
            previous_rule=outcome.previous_rule,
            restored_entity_count=len(outcome.restored_entity_ids),
            flagged_finding_count=outcome.flagged_findings,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — log_event is itself non-raising.
        logger.warning("entity unmerge audit write failed: %s", exc)


def _audit_release(
    org_id: str,
    unmerge_id: str,
    pair_keys: Sequence[str],
    *,
    actor: str,
    reason: Optional[str],
) -> None:
    try:
        from .middleware.audit import ENTITY_MERGE_BLOCK_RELEASED, log_event

        log_event(
            ENTITY_MERGE_BLOCK_RELEASED,
            org_id=org_id,
            user_id=actor,
            unmerge_id=unmerge_id,
            released_keys=len(pair_keys),
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("merge-block release audit write failed: %s", exc)


__all__ = [
    "METADATA_UNMERGED_FROM",
    "STATUS_BLOCKED",
    "STATUS_RELEASED",
    "PAIR_KEY_ROWS",
    "PAIR_KEY_IDENTITY",
    "OUTCOME_UNMERGED",
    "OUTCOME_NOT_MERGED",
    "DEFAULT_MAX_RUNS_SCANNED",
    "EntityUnmergeError",
    "MergeBlock",
    "DependencySweep",
    "UnmergeOutcome",
    "row_pair_key",
    "identity_pair_key",
    "pair_keys_for",
    "detached_subtree",
    "merge_block_for",
    "merge_block_on_cursor",
    "dependent_findings",
    "unmerge_entity",
    "unmerge_all",
    "release_merge_block",
    "list_unmerges",
]
