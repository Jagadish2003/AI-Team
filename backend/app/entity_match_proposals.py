"""
entity_match_proposals.py — Release 2.0-B2 T3: the proposal review store.

The ranked engine (:mod:`app.cross_source_resolution`, T1) auto-merges only where
identity is STATED — an explicit cross-reference, or the org's alias table. A
name-similarity match is never merged; it is a QUESTION for a person. This module
is where that question is parked, shown with its evidence, and answered.

Three rules shape everything here:

1. **One question per pair, not one per direction.** The engine resolves each
   entity independently, so a proposed pair arrives TWICE (A→B and B→A). Both are
   the same question. :func:`proposal_id_for` derives a deterministic id from the
   ORDER-INDEPENDENT pair, so the two collapse into one row an analyst answers
   once — and a later engine pass upserts that same row instead of growing a
   duplicate queue.

2. **An answered question is never asked again.** :func:`record_proposals`
   refreshes evidence for ``pending`` rows only. Once someone has confirmed or
   rejected a pair, a later pass leaves it alone: re-proposing it would ask
   forever and quietly discard the answer (the story's "confirm/reject is recorded
   and durable across runs").

3. **Recording a decision is not applying one.** A confirmation is a durable,
   attributable statement that two entities are the same thing —
   :func:`confirmed_pairs` is the read a merge applier consumes. This module never
   writes to ``entities`` or ``entity_relationships``; merging with provenance is
   a separate task, and doing it here silently would put an irreversible graph
   change behind a button labelled "confirm".

History is append-only. An analyst reversing their own decision appends a new
forward row rather than rewriting the old one, so the original answer and its
author survive — the same discipline as ``opportunity_lifecycle_history``.

Every key and every query includes ``org_id``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from . import db

logger = logging.getLogger(__name__)

# Statuses. A proposal starts pending and is answered exactly one of two ways.
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
PROPOSAL_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_REJECTED)

# Actions a reviewer can take.
ACTION_CONFIRM = "confirm"
ACTION_REJECT = "reject"
DECISION_ACTIONS = (ACTION_CONFIRM, ACTION_REJECT)

_STATUS_FOR_ACTION: Dict[str, str] = {
    ACTION_CONFIRM: STATUS_CONFIRMED,
    ACTION_REJECT: STATUS_REJECTED,
}

#: Default page size for the review queue — bounded so one org with a large
#: estate cannot make the review surface unloadable.
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 1000

_MAX_NOTE_CHARS = 2000


class ProposalNotFound(LookupError):
    """No such proposal in this org. Cross-org and missing are the same answer —
    a proposal id from another tenant must not be distinguishable from a typo."""


class ProposalDecisionError(ValueError):
    """The requested decision is not valid (unknown action, blank actor)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    """A trimmed string, unwrapping the ServiceNow ``{value, display_value}``
    envelope so an identity is never built from a dict's repr."""
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("display_value")
    return str(value or "").strip()


def _required(value: Any, name: str) -> str:
    text = _text(value)
    if not text:
        raise ProposalDecisionError(f"{name} is required")
    return text


def _clean_note(note: Any) -> Optional[str]:
    text = str(note or "").strip()
    if not text:
        return None
    return text[:_MAX_NOTE_CHARS]


def proposal_id_for(entity_type: str, left_entity_id: str, right_entity_id: str) -> str:
    """Deterministic, ORDER-INDEPENDENT id for one proposed pair.

    Sorting the two entity ids before hashing is what makes A→B and B→A the same
    question — see the module docstring. The entity type is part of the digest so
    a ``team`` pair and a ``system`` pair with (impossibly) the same ids could
    never collide.

    Deterministic rather than random so a re-scan upserts the existing row: the
    review queue is a set of open questions, not an append-only log of every time
    the engine noticed one.
    """
    left = str(left_entity_id or "").strip()
    right = str(right_entity_id or "").strip()
    if not left or not right:
        raise ProposalDecisionError("a proposal needs two entity ids")
    if left == right:
        raise ProposalDecisionError("a proposal cannot pair an entity with itself")
    a, b = sorted((left, right))
    digest = hashlib.sha256(
        f"{str(entity_type or '').strip()}|{a}|{b}".encode("utf-8")
    ).hexdigest()
    return f"emp_{digest[:32]}"


def sorted_pair(left_entity_id: str, right_entity_id: str) -> Tuple[str, str]:
    """The pair in the canonical stored order."""
    return tuple(sorted((str(left_entity_id), str(right_entity_id))))  # type: ignore[return-value]


# ── the stable identity key (2.0-B2 T4) ─────────────────────────────────────
#
# ``proposal_id`` hashes entity ROW ids, which is right for addressing a row but
# wrong for remembering a DECISION: row ids churn. The clearest case is a source
# that begins supplying record ids — ``upsert_source_entity`` keys on
# ``(source_system, source_record_id)``, does not match the name-only row already
# there, and INSERTS a second resolved row for the same real thing. A decision
# keyed on row ids alone then misses its own pair and asks the question again,
# which is exactly what AC3 forbids.
#
# The identity key is what the pair IS in its source systems, so it survives that.


def entity_identity(source_system: Any, canonical_name: Any) -> str:
    """One side's STABLE identity: its source system and canonical name.

    The record id is deliberately NOT part of this, which is counter-intuitive
    enough to be worth stating plainly: the record id is the part that CHURNS. A
    connector that starts supplying record ids gives the same real entity a new
    ``(system, record_id)`` pair — and a key built on that would change exactly
    when we most need it to hold, defeating its own purpose.

    The canonical name is invariant for the pairs this table can hold. Only a
    propose-only tier reaches here, and the only one is ``name_similarity``, which
    requires EXACT canonical-name equality across two different sources — so the
    name is the pair's joint identity by construction, and it is what the reviewer
    actually answered about ("ServiceNow's Payments and Jira's Payments").

    A rename does produce a new key, and that is correct rather than a gap: once
    the names diverge the tier's premise is gone and the pair is no longer
    proposed at all; if both sides are renamed alike, it is a genuinely new
    question about names nobody has yet been asked about.
    """
    return f"{_text(source_system).lower()}|name:{_canonical_name(canonical_name)}"


def identity_key_for(entity_type: str, left_identity: str, right_identity: str) -> str:
    """Deterministic, ORDER-INDEPENDENT key for a pair's stable identities.

    Same construction as :func:`proposal_id_for` — sorted then hashed, with the
    entity type in the digest — so the two keys agree about what "the same pair"
    means and differ only in WHICH identity they are built from.
    """
    left, right = _text(left_identity), _text(right_identity)
    if not left or not right:
        raise ProposalDecisionError("an identity key needs two source identities")
    if left == right:
        raise ProposalDecisionError(
            "a pair cannot share one source identity — that is one entity"
        )
    a, b = sorted((left, right))
    digest = hashlib.sha256(
        f"{_text(entity_type)}|{a}|{b}".encode("utf-8")
    ).hexdigest()
    return f"emk_{digest[:32]}"


def _canonical_name(value: Any) -> str:
    """The shared canonicalisation, so this layer and the entity layer cannot
    disagree about what a name is."""
    try:
        from .entity_resolution import canonical_name_for

        return canonical_name_for(_text(value))
    except Exception:  # noqa: BLE001 — never let a name lookup break a scan.
        return " ".join(_text(value).split()).lower()


def _identity_from_view(view: Any) -> str:
    """One side's stable identity from an evidence-snapshot view or a resolution
    entity — whichever the caller has.

    Prefers the stored ``canonical_name`` and falls back to the display name,
    which :func:`_canonical_name` then normalises the same way the entity layer
    does — so a snapshot that recorded only a display name still yields the same
    identity as the live entity.
    """
    if isinstance(view, Mapping):
        return entity_identity(
            view.get("source_system"),
            view.get("canonical_name") or view.get("display_name"),
        )
    return entity_identity(
        getattr(view, "source_system", None),
        getattr(view, "canonical_name", None) or getattr(view, "display_name", None),
    )


def identity_key_from_evidence(evidence: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Recompute a stored proposal's identity key from its evidence snapshot.

    This is what lets rows written before T4 be backfilled rather than abandoned:
    the snapshot already carries both sides' ``source_system`` and
    ``source_record_id`` (T3 stored them for the reviewer), which is precisely the
    identity the key is built from. Returns ``None`` when the snapshot cannot
    supply one, so an unbackfillable row is left alone rather than given a wrong key.
    """
    if not isinstance(evidence, Mapping):
        return None
    subject, target = evidence.get("subject"), evidence.get("target")
    if not isinstance(subject, Mapping) or not isinstance(target, Mapping):
        return None
    entity_type = _text(subject.get("entity_type")) or _text(target.get("entity_type"))
    try:
        return identity_key_for(
            entity_type, _identity_from_view(subject), _identity_from_view(target)
        )
    except ProposalDecisionError:
        return None


# ── data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityMatchProposal:
    """One proposed pair as the review surface sees it."""

    org_id: str
    proposal_id: str
    entity_type: str
    left_entity_id: str
    right_entity_id: str
    tier: str
    confidence: float
    status: str
    #: 2.0-B2 T4 — the pair's stable source identity (see identity_key_for).
    identity_key: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 0
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: Optional[str] = None
    first_proposed_at: Optional[str] = None
    last_proposed_at: Optional[str] = None

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "proposal_id": self.proposal_id,
            "entity_type": self.entity_type,
            "left_entity_id": self.left_entity_id,
            "right_entity_id": self.right_entity_id,
            "tier": self.tier,
            "confidence": self.confidence,
            "status": self.status,
            "identity_key": self.identity_key,
            "evidence": dict(self.evidence),
            "revision": self.revision,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "note": self.note,
            "first_proposed_at": self.first_proposed_at,
            "last_proposed_at": self.last_proposed_at,
        }


@dataclass(frozen=True)
class DecisionOutcome:
    """The result of confirming or rejecting one proposal."""

    proposal: EntityMatchProposal
    action: str
    previous_status: str
    resulting_status: str
    revision: int
    changed: bool
    actor_id: str
    decided_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal": self.proposal.to_dict(),
            "action": self.action,
            "previous_status": self.previous_status,
            "resulting_status": self.resulting_status,
            "revision": self.revision,
            "changed": self.changed,
            "actor_id": self.actor_id,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class RecordOutcome:
    """What one :func:`record_proposals` pass did — reported, never silent.

    ``skipped_already_decided`` is the number of pairs the engine proposed again
    that a human has already answered. It is the honest counterpart to rule 2:
    the queue did not grow, and here is exactly how many times that rule fired.
    """

    created: int = 0
    refreshed: int = 0
    skipped_already_decided: int = 0
    proposal_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": self.created,
            "refreshed": self.refreshed,
            "skipped_already_decided": self.skipped_already_decided,
            "proposal_ids": list(self.proposal_ids),
        }


# ── row mapping ─────────────────────────────────────────────────────────────


def _loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 — a corrupt snapshot must not break the queue.
            logger.warning("entity match proposal has unreadable evidence payload")
            return {}
    return {}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_proposal(row: Mapping[str, Any]) -> EntityMatchProposal:
    return EntityMatchProposal(
        org_id=row["org_id"],
        proposal_id=row["proposal_id"],
        entity_type=row["entity_type"],
        left_entity_id=row["left_entity_id"],
        right_entity_id=row["right_entity_id"],
        tier=row["tier"],
        confidence=float(row["confidence"]),
        status=row["status"],
        identity_key=row.get("identity_key"),
        evidence=_loads(row["evidence_payload"]),
        revision=int(row["revision"] or 0),
        decided_by=row.get("decided_by"),
        decided_at=_iso(row.get("decided_at")),
        note=row.get("note"),
        first_proposed_at=_iso(row.get("first_proposed_at")),
        last_proposed_at=_iso(row.get("last_proposed_at")),
    )


# ── building a proposal's evidence snapshot ─────────────────────────────────


def _entity_view(entity: Any) -> Dict[str, Any]:
    """The reviewer-facing view of one side of the pair.

    Everything a person needs to answer "are these the same thing?" without
    leaving the screen: what it is called in each system, which record it is, and
    which system it came from.
    """
    return {
        "entity_id": getattr(entity, "entity_id", None),
        "display_name": getattr(entity, "display_name", None),
        "canonical_name": getattr(entity, "canonical_name", None),
        "entity_type": getattr(entity, "entity_type", None),
        "source_system": getattr(entity, "source_system", None),
        "source_record_id": getattr(entity, "source_record_id", None),
    }


def build_proposal_evidence(subject: Any, match: Any) -> Dict[str, Any]:
    """Snapshot the evidence behind one proposed match.

    Stored with the proposal so the review surface can explain it without
    re-running the engine, and so a decision stays explainable against the
    evidence that existed when it was made.
    """
    evidence = dict(getattr(match, "evidence", {}) or {})
    return {
        "subject": _entity_view(subject),
        "target": _entity_view(getattr(match, "target", None)),
        "tier": getattr(match, "tier", None),
        "confidence": getattr(match, "confidence", None),
        "reason": getattr(match, "reason", ""),
        "corroborating_relationships": list(
            evidence.get("corroborating_relationships") or []
        ),
        "canonical_name": evidence.get("canonical_name"),
        "subject_source": evidence.get("subject_source"),
        "target_source": evidence.get("target_source"),
    }


# ── writing proposals ───────────────────────────────────────────────────────


def record_proposals(
    org_id: str, decisions: Iterable[Any], *, now: Optional[str] = None
) -> RecordOutcome:
    """Record the PROPOSALS carried by a batch of T1 resolution decisions.

    Only PROPOSAL-tier decisions reach this table: one that authorises an
    auto-merge has nothing to review, and an ``unresolved``/``ambiguous`` one has
    nothing to show. That is tier 3 (exact name) and, where a deployment has opted
    in, tier 4 (leading-word name) — the tier travels with each row so a reviewer
    can see which signal produced the question. Each proposed pair is written once
    (order-independent id), and an already-answered pair is left untouched and
    counted.

    Idempotent: running it twice over the same decisions creates nothing new the
    second time. Never raises for an individual malformed decision — that entry is
    skipped with a warning rather than failing the whole pass.
    """
    org = _required(org_id, "org_id")
    stamp = now or _now()

    #: pairs already handled in THIS pass — the symmetric A→B / B→A collapse.
    seen: set = set()
    prepared: List[Dict[str, Any]] = []

    for decision in decisions or ():
        subject = getattr(decision, "subject", None)
        for match in getattr(decision, "proposals", ()) or ():
            target = getattr(match, "target", None)
            if subject is None or target is None:
                continue
            try:
                entity_type = str(getattr(subject, "entity_type", "") or "")
                pid = proposal_id_for(
                    entity_type,
                    getattr(subject, "entity_id", ""),
                    getattr(target, "entity_id", ""),
                )
            except ProposalDecisionError as exc:
                logger.warning("skipping unusable entity match proposal: %s", exc)
                continue
            if pid in seen:
                continue
            seen.add(pid)
            left, right = sorted_pair(
                getattr(subject, "entity_id", ""), getattr(target, "entity_id", "")
            )
            # T4: the pair's stable source identity, so a decision survives the
            # entity row ids changing underneath it.
            try:
                identity_key = identity_key_for(
                    entity_type,
                    _identity_from_view(subject),
                    _identity_from_view(target),
                )
            except ProposalDecisionError as exc:
                # A pair whose two sides share one source identity is one entity,
                # not a match — and a pair with no usable identity cannot be
                # remembered durably. Neither is silently proposed.
                logger.warning(
                    "skipping entity match proposal with no usable identity key: %s", exc
                )
                continue
            prepared.append({
                "proposal_id": pid,
                "entity_type": entity_type,
                "left": left,
                "right": right,
                "identity_key": identity_key,
                "tier": str(getattr(match, "tier", "") or ""),
                "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
                "evidence": json.dumps(build_proposal_evidence(subject, match)),
            })

    if not prepared:
        return RecordOutcome()

    # T4: heal any pre-T4 rows first, so a decision recorded before the identity
    # key existed still protects its pair from here on.
    backfill_identity_keys(org)
    # Every pair this org has already ANSWERED, by stable identity. A pair in here
    # is never written again, whatever its current entity row ids are.
    decided = decided_identity_keys(org)

    created = 0
    refreshed = 0
    skipped = 0
    con = db.connect()
    try:
        cur = con.cursor()
        for item in prepared:
            if item["identity_key"] in decided:
                # The row ids may be new, but the QUESTION is not: a human has
                # answered this pair. Counted, never re-asked (AC3).
                skipped += 1
                continue
            # The WHERE clause is rule 2 in SQL: a decided row is not touched, so
            # a later pass can never revert an answer to "pending" or overwrite
            # the evidence the answer was given against.
            cur.execute(
                """
                INSERT INTO entity_match_proposals (
                    org_id, proposal_id, entity_type, left_entity_id, right_entity_id,
                    tier, confidence, status, identity_key, evidence_payload, revision,
                    decided_by, decided_at, note,
                    first_proposed_at, last_proposed_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0,
                    NULL, NULL, NULL, %s, %s, %s, %s
                )
                ON CONFLICT (org_id, proposal_id) DO UPDATE SET
                    tier             = EXCLUDED.tier,
                    confidence       = EXCLUDED.confidence,
                    identity_key     = EXCLUDED.identity_key,
                    evidence_payload = EXCLUDED.evidence_payload,
                    last_proposed_at = EXCLUDED.last_proposed_at,
                    updated_at       = EXCLUDED.updated_at
                WHERE entity_match_proposals.status = %s
                RETURNING (xmax = 0) AS inserted
                """,
                (
                    org, item["proposal_id"], item["entity_type"], item["left"],
                    item["right"], item["tier"], item["confidence"], STATUS_PENDING,
                    item["identity_key"], item["evidence"],
                    stamp, stamp, stamp, stamp, STATUS_PENDING,
                ),
            )
            row = cur.fetchone()
            if row is None:
                # The conflict target existed but the WHERE excluded it: an
                # already-decided pair. Counted, never silently ignored.
                skipped += 1
            elif bool(row[0]):
                created += 1
            else:
                refreshed += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return RecordOutcome(
        created=created,
        refreshed=refreshed,
        skipped_already_decided=skipped,
        proposal_ids=tuple(item["proposal_id"] for item in prepared),
    )


# ── reading ─────────────────────────────────────────────────────────────────


def decided_identity_keys(org_id: str) -> set:
    """Every pair this org has ANSWERED, by stable identity (2.0-B2 T4 / AC3).

    The durability read: ``record_proposals`` consults it before writing, so a
    confirmed or rejected pair is never re-proposed even when its entity ROW ids
    have changed since the decision. Confirmed and rejected both count — "these
    are not the same thing" is as durable an answer as "they are".
    """
    org = _required(org_id, "org_id")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT identity_key FROM entity_match_proposals "
            "WHERE org_id = %s AND identity_key IS NOT NULL AND status <> %s",
            (org, STATUS_PENDING),
        )
        return {row[0] for row in cur.fetchall() if row[0]}
    finally:
        con.close()


def backfill_identity_keys(org_id: str) -> int:
    """Give pre-T4 rows their stable identity key, from their own evidence snapshot.

    Rows written before T4 have ``identity_key IS NULL``, so the durability check
    above cannot see them — a decision recorded then would be unprotected against
    row churn forever. The snapshot T3 already stored carries both sides' source
    system and record id, which is exactly what the key is built from, so the fix
    needs no data migration and no re-run of the engine.

    Idempotent and cheap: it touches only NULL rows, so it is a no-op once healed —
    which is why ``record_proposals`` can call it every pass. Returns the number of
    rows healed. A row whose snapshot cannot supply an identity is left NULL rather
    than given a wrong key.
    """
    org = _required(org_id, "org_id")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT proposal_id, evidence_payload FROM entity_match_proposals "
            "WHERE org_id = %s AND identity_key IS NULL",
            (org,),
        )
        rows = [(r[0], r[1]) for r in cur.fetchall()]
        healed = 0
        for proposal_id, payload in rows:
            key = identity_key_from_evidence(_loads(payload))
            if not key:
                logger.debug(
                    "entity match proposal %s cannot be backfilled — its evidence "
                    "snapshot carries no usable source identity", proposal_id,
                )
                continue
            cur.execute(
                "UPDATE entity_match_proposals SET identity_key = %s "
                "WHERE org_id = %s AND proposal_id = %s AND identity_key IS NULL",
                (key, org, proposal_id),
            )
            healed += 1
        if healed:
            con.commit()
            logger.info(
                "2.0-B2 T4: backfilled %d entity match proposal identity key(s) for "
                "org %s", healed, org,
            )
        return healed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def list_proposals(
    org_id: str,
    *,
    status: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> List[EntityMatchProposal]:
    """One org's proposals, newest first, optionally filtered by status.

    Org-scoped in SQL — the review surface can never render another tenant's
    queue. An unknown status filter returns nothing rather than everything: a
    typo must not silently widen what is shown.
    """
    org = _required(org_id, "org_id")
    bounded = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    if status is not None and status not in PROPOSAL_STATUSES:
        return []

    sql = "SELECT * FROM entity_match_proposals WHERE org_id = %s"
    params: List[Any] = [org]
    if status is not None:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY last_proposed_at DESC, proposal_id ASC LIMIT %s"
    params.append(bounded)

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(sql, tuple(params))
        return [_row_to_proposal(dict(row)) for row in cur.fetchall()]
    finally:
        con.close()


def status_counts(org_id: str) -> Dict[str, int]:
    """Per-status counts for the review surface's filter tabs.

    Every status is present (zero-filled), so the UI never has to distinguish
    "none" from "not reported".
    """
    org = _required(org_id, "org_id")
    counts = {status: 0 for status in PROPOSAL_STATUSES}
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT status, COUNT(*) FROM entity_match_proposals "
            "WHERE org_id = %s GROUP BY status",
            (org,),
        )
        for row in cur.fetchall():
            key = str(row[0])
            if key in counts:
                counts[key] = int(row[1])
    finally:
        con.close()
    return counts


def get_proposal(org_id: str, proposal_id: str) -> EntityMatchProposal:
    """One proposal. Raises :class:`ProposalNotFound` for unknown AND cross-org."""
    org = _required(org_id, "org_id")
    pid = _required(proposal_id, "proposal_id")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM entity_match_proposals WHERE org_id = %s AND proposal_id = %s",
            (org, pid),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        raise ProposalNotFound("entity match proposal not found")
    return _row_to_proposal(dict(row))


def history(org_id: str, proposal_id: str) -> List[Dict[str, Any]]:
    """The append-only decision history for one proposal, newest first."""
    org = _required(org_id, "org_id")
    pid = _required(proposal_id, "proposal_id")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, org_id, proposal_id, revision, action, previous_status,
                   resulting_status, actor_id, note, decided_at
            FROM entity_match_proposal_history
            WHERE org_id = %s AND proposal_id = %s
            ORDER BY revision DESC
            """,
            (org, pid),
        )
        rows = [dict(row) for row in cur.fetchall()]
    finally:
        con.close()
    for row in rows:
        row["decided_at"] = _iso(row.get("decided_at"))
    return rows


def confirmed_pairs(org_id: str) -> List[Tuple[str, str, str]]:
    """Every human-confirmed identity pair for one org, as
    ``(entity_type, left_entity_id, right_entity_id)``.

    This is the read a MERGE APPLIER consumes. Confirming records an identity
    assertion; applying it to the graph (with the provenance the story requires)
    is a separate task, so this module deliberately stops here.
    """
    org = _required(org_id, "org_id")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT entity_type, left_entity_id, right_entity_id "
            "FROM entity_match_proposals WHERE org_id = %s AND status = %s "
            "ORDER BY entity_type, left_entity_id, right_entity_id",
            (org, STATUS_CONFIRMED),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        con.close()


# ── deciding ────────────────────────────────────────────────────────────────


def decide(
    org_id: str,
    proposal_id: str,
    action: str,
    actor_id: str,
    *,
    note: Any = None,
) -> DecisionOutcome:
    """Confirm or reject one proposal, appending to its history.

    Idempotent: repeating the decision already in force changes nothing and
    returns ``changed=False`` (no duplicate history row for a double-click).
    Reversing a decision IS allowed and appends a new forward row — an analyst who
    mis-clicked must be able to correct it, and the original decision stays in the
    record rather than being edited away.

    Raises :class:`ProposalNotFound` (unknown or cross-org) and
    :class:`ProposalDecisionError` (unknown action, blank actor).
    """
    org = _required(org_id, "org_id")
    pid = _required(proposal_id, "proposal_id")
    actor = _required(actor_id, "actor_id")
    normalised = str(action or "").strip().lower()
    if normalised not in DECISION_ACTIONS:
        raise ProposalDecisionError(
            f"action must be one of {list(DECISION_ACTIONS)}; got {action!r}"
        )
    cleaned_note = _clean_note(note)
    resulting = _STATUS_FOR_ACTION[normalised]

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM entity_match_proposals "
            "WHERE org_id = %s AND proposal_id = %s FOR UPDATE",
            (org, pid),
        )
        row = cur.fetchone()
        if row is None:
            raise ProposalNotFound("entity match proposal not found")
        current = _row_to_proposal(dict(row))

        if current.status == resulting:
            con.commit()
            return DecisionOutcome(
                proposal=current,
                action=normalised,
                previous_status=current.status,
                resulting_status=current.status,
                revision=current.revision,
                changed=False,
                actor_id=actor,
                decided_at=current.decided_at or _now(),
            )

        revision = current.revision + 1
        decided_at = _now()
        cur.execute(
            """
            UPDATE entity_match_proposals
            SET status = %s, revision = %s, decided_by = %s, decided_at = %s,
                note = %s, updated_at = %s
            WHERE org_id = %s AND proposal_id = %s
            """,
            (resulting, revision, actor, decided_at, cleaned_note, decided_at, org, pid),
        )
        cur.execute(
            """
            INSERT INTO entity_match_proposal_history (
                id, org_id, proposal_id, revision, action, previous_status,
                resulting_status, actor_id, note, decided_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"emph_{uuid4().hex}", org, pid, revision, normalised,
                current.status, resulting, actor, cleaned_note, decided_at,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    decided = get_proposal(org, pid)
    return DecisionOutcome(
        proposal=decided,
        action=normalised,
        previous_status=current.status,
        resulting_status=resulting,
        revision=revision,
        changed=True,
        actor_id=actor,
        decided_at=decided_at,
    )


# ── scan: produce proposals from the ranked engine ──────────────────────────

#: Entity types worth scanning for cross-source identity. Deliberately explicit
#: rather than "every type": ``person`` is excluded because a same-named person
#: across two systems is the highest-risk merge in the platform and the weakest
#: evidence (two real people share a name far more often than two systems do), and
#: proposing those would fill the queue with exactly the decisions a reviewer is
#: least able to make safely from a screen.
SCANNABLE_ENTITY_TYPES: Tuple[str, ...] = ("system", "team", "project", "object")


#: Deployment opt-in for the tier-4 leading-word tier. ``0``/unset keeps the
#: engine's default behaviour (exact name matching only).
PREFIX_WORDS_ENV = "ENTITY_MATCH_PREFIX_WORDS"
#: Drop the shared-neighbour requirement for the leading-word tier, leaving the
#: words as the only evidence. The fastest way to fill a review queue.
PREFIX_NO_CORROBORATION_ENV = "ENTITY_MATCH_PREFIX_NO_CORROBORATION"
#: Cap on proposals per subject. Matters most when the guards above are off.
PREFIX_MAX_ENV = "ENTITY_MATCH_PREFIX_MAX_PROPOSALS"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", name, raw, default)
        return default
    return value if value > 0 else default


def _scan_policy():
    """The resolution policy this scan runs under.

    The switch lives HERE rather than in ``cross_source_resolution`` on purpose:
    that module is deliberately pure — no env, no clock, no I/O — which is what
    makes its merge-boundary guarantee auditable by reading it. Enabling a tier is
    an operational decision, so it belongs at the scan boundary where the other
    operational decisions already are.

    A malformed or negative value falls back to OFF and logs, rather than raising:
    a typo in a deployment variable should not stop a scan, and silently enabling
    a wider match would be the worse failure.
    """
    from .cross_source_resolution import DEFAULT_POLICY, ResolutionPolicy

    raw = os.getenv(PREFIX_WORDS_ENV, "").strip()
    if not raw:
        return DEFAULT_POLICY
    try:
        words = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — leading-word matching stays OFF",
            PREFIX_WORDS_ENV, raw,
        )
        return DEFAULT_POLICY
    if words <= 0:
        return DEFAULT_POLICY

    corroborate = not _env_flag(PREFIX_NO_CORROBORATION_ENV)
    cap = _env_int(PREFIX_MAX_ENV, DEFAULT_POLICY.max_proposals)

    logger.info(
        "Entity match scan: leading-word tier ENABLED at %d word(s) "
        "(require_corroboration=%s, cap=%d). Cross-source pairs whose full names "
        "differ will be PROPOSED for review — never merged.",
        words, corroborate, cap,
    )
    if not corroborate:
        # The shared-neighbour requirement is tier 4's only evidence beyond the
        # words. Said loudly because this is the setting that fills a queue
        # fastest, and an operator who did it by accident should be able to find
        # out from the log rather than from the review surface.
        logger.warning(
            "Entity match scan: leading-word tier running WITHOUT the "
            "corroborating-relationship requirement — any cross-source pair "
            "sharing its first %d word(s) will be proposed on the names alone. "
            "The cap of %d is the only bound on queue size.", words, cap,
        )
    return ResolutionPolicy(
        name_prefix_words=words,
        name_prefix_require_corroboration=corroborate,
        max_proposals=cap,
    )


def scan_for_proposals(
    org_id: str, *, entity_types: Optional[Sequence[str]] = None
) -> RecordOutcome:
    """Run the ranked engine over an org and record whatever it PROPOSES.

    Read-only with respect to the graph: the engine writes nothing (T1), and this
    persists only proposal rows. Types outside :data:`SCANNABLE_ENTITY_TYPES` are
    ignored rather than silently scanned.
    """
    from .cross_source_resolution import resolve_org_entity_type

    org = _required(org_id, "org_id")
    requested = [
        t for t in (entity_types or SCANNABLE_ENTITY_TYPES)
        if t in SCANNABLE_ENTITY_TYPES
    ]

    policy = _scan_policy()

    created = refreshed = skipped = 0
    ids: List[str] = []
    for entity_type in requested:
        decisions = resolve_org_entity_type(org, entity_type, policy=policy)
        outcome = record_proposals(org, decisions)
        created += outcome.created
        refreshed += outcome.refreshed
        skipped += outcome.skipped_already_decided
        ids.extend(outcome.proposal_ids)
    return RecordOutcome(
        created=created,
        refreshed=refreshed,
        skipped_already_decided=skipped,
        proposal_ids=tuple(ids),
    )


__all__ = [
    "STATUS_PENDING",
    "STATUS_CONFIRMED",
    "STATUS_REJECTED",
    "PROPOSAL_STATUSES",
    "ACTION_CONFIRM",
    "ACTION_REJECT",
    "DECISION_ACTIONS",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "SCANNABLE_ENTITY_TYPES",
    "ProposalNotFound",
    "ProposalDecisionError",
    "EntityMatchProposal",
    "DecisionOutcome",
    "RecordOutcome",
    "proposal_id_for",
    "sorted_pair",
    "entity_identity",
    "identity_key_for",
    "identity_key_from_evidence",
    "decided_identity_keys",
    "backfill_identity_keys",
    "build_proposal_evidence",
    "record_proposals",
    "list_proposals",
    "status_counts",
    "get_proposal",
    "history",
    "confirmed_pairs",
    "decide",
    "scan_for_proposals",
]
