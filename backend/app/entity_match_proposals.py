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


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
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

    Only tier-3 proposals reach this table: a decision that authorises an
    auto-merge has nothing to review, and an ``unresolved``/``ambiguous`` one has
    nothing to show. Each proposed pair is written once (order-independent id),
    and an already-answered pair is left untouched and counted.

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
            prepared.append({
                "proposal_id": pid,
                "entity_type": entity_type,
                "left": left,
                "right": right,
                "tier": str(getattr(match, "tier", "") or ""),
                "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
                "evidence": json.dumps(build_proposal_evidence(subject, match)),
            })

    if not prepared:
        return RecordOutcome()

    created = 0
    refreshed = 0
    skipped = 0
    con = db.connect()
    try:
        cur = con.cursor()
        for item in prepared:
            # The WHERE clause is rule 2 in SQL: a decided row is not touched, so
            # a later pass can never revert an answer to "pending" or overwrite
            # the evidence the answer was given against.
            cur.execute(
                """
                INSERT INTO entity_match_proposals (
                    org_id, proposal_id, entity_type, left_entity_id, right_entity_id,
                    tier, confidence, status, evidence_payload, revision,
                    decided_by, decided_at, note,
                    first_proposed_at, last_proposed_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, 0,
                    NULL, NULL, NULL, %s, %s, %s, %s
                )
                ON CONFLICT (org_id, proposal_id) DO UPDATE SET
                    tier             = EXCLUDED.tier,
                    confidence       = EXCLUDED.confidence,
                    evidence_payload = EXCLUDED.evidence_payload,
                    last_proposed_at = EXCLUDED.last_proposed_at,
                    updated_at       = EXCLUDED.updated_at
                WHERE entity_match_proposals.status = %s
                RETURNING (xmax = 0) AS inserted
                """,
                (
                    org, item["proposal_id"], item["entity_type"], item["left"],
                    item["right"], item["tier"], item["confidence"], STATUS_PENDING,
                    item["evidence"], stamp, stamp, stamp, stamp, STATUS_PENDING,
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

    created = refreshed = skipped = 0
    ids: List[str] = []
    for entity_type in requested:
        decisions = resolve_org_entity_type(org, entity_type)
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
