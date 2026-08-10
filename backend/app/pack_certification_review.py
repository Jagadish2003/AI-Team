"""Pack certification review workflow — 2.0-C2 T2 (AT-832).

The rule this module owns (parent story AC5):

    Every certification decision is recorded with reviewer, criteria, and date,
    and is auditable.

What a review is — and what it deliberately is NOT
--------------------------------------------------
A review is the **record of a human decision**: who reviewed which pack version,
against which criteria, how each criterion came out, on what date, against which
platform version, and whether the outcome was approve or reject.

A review does **not** grant a badge. AT-831 is unambiguous that the only thing
which makes a pack Certified is a valid CloudFulcrum signature over its metadata,
and if recording a review were enough to change a level then the reviewer's
database row would be the trust root instead of the signing key — which is exactly
the self-application hole the signature exists to close.

So the two halves compose like this:

    review (this module, in-app)  →  decision + criteria verdicts, recorded
             ↓  approved_declaration()
    canonical payload             →  signed OFFLINE with the release key
             ↓
    certification metadata        →  verified at runtime by AT-831

:meth:`PackCertificationReview.approved_declaration` emits the exact declaration
block and canonical payload bytes the key holder signs, so what gets signed is
provably what was approved rather than something retyped afterwards.

Approval is gated on the checklist
----------------------------------
A review cannot be recorded as ``approved`` unless every REQUIRED criterion
(``discovery.packs.certification_criteria``) carries a passing verdict. This is
the whole point of a checklist-driven workflow: an approval that skipped an item
is not a lighter-weight approval, it is an unreviewed pack wearing a badge. A
``not_applicable`` verdict is allowed but must carry a note, because "this did not
apply" is a judgement that a later auditor has to be able to read.

Append-only
-----------
There is **no update and no delete path in this module**. Re-reviewing a pack
writes a NEW record at the next revision; a superseded review stays on the trail.
That is what makes it an audit trail rather than a current-state mirror, and it is
why ``pack_certification_reviews`` is in ``history_retention.PROTECTED_TABLES``.

Org scoping
-----------
Certification review is an internal CloudFulcrum activity, performed in
CloudFulcrum's own workspace. It is nevertheless org-scoped like every other write
in this codebase — one tenant can never read or write another's review trail, and
a partner or federal deployment reviewing packs locally is isolated by
construction.

Read posture
------------
Reads are fail-soft where a missing review must not break a page
(:func:`latest_reviews_safe`) and strict where the caller asked a direct question.
Writes are never fail-soft: a review that did not persist must not look recorded.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from app import db
from app.pack_state import PackNotFound
from discovery.packs.certification_criteria import (
    CERTIFICATION_CRITERIA,
    REQUIRED_CRITERION_IDS,
    describe_criterion,
    is_known_criterion,
)
from discovery.packs.pack_certification import (
    CLOUDFULCRUM,
    LEVEL_LABELS,
    SIGNATURE_REQUIRED_LEVELS,
    canonical_payload_bytes,
    certification_payload,
)
from discovery.packs.platform_capabilities import get_platform_version

logger = logging.getLogger(__name__)

# ── Vocabulary ────────────────────────────────────────────────────────────────

#: The criterion met.
OUTCOME_PASS = "pass"
#: The criterion was checked and NOT met — blocks approval.
OUTCOME_FAIL = "fail"
#: The criterion does not apply to this pack. Requires a note.
OUTCOME_NOT_APPLICABLE = "not_applicable"

CRITERION_OUTCOMES = frozenset({OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_NOT_APPLICABLE})

#: The reviewer approved the pack at the proposed level.
DECISION_APPROVED = "approved"
#: The reviewer did not approve. The pack keeps whatever it had before.
DECISION_REJECTED = "rejected"

REVIEW_DECISIONS = frozenset({DECISION_APPROVED, DECISION_REJECTED})


class ReviewValidationError(ValueError):
    """The review as submitted is not a well-formed checklist review."""


class ReviewDecisionError(ReviewValidationError):
    """The decision contradicts the checklist (e.g. approved with a failed item).

    ``str(exc)`` names every criterion responsible, so the refusal is actionable.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, name: str, *, max_length: int = 2000) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReviewValidationError(f"{name} is required")
    return text[:max_length]


def _optional_text(value: Any, *, max_length: int = 4000) -> Optional[str]:
    text = str(value or "").strip()
    return text[:max_length] or None


def _validated_pack_id(pack_id: Any) -> str:
    """Strict, like ``pack_state``: an unknown pack has nothing to review.

    Deliberately NOT ``get_pack()``'s resolve-to-default behaviour — recording a
    review of ``service_cloud`` because someone typo'd a pack id would put a real
    reviewer's name against a pack they never looked at.
    """
    from discovery.packs.pack_config import PACK_REGISTRY

    pack = str(pack_id or "").strip()
    if not pack:
        raise ReviewValidationError("pack_id is required")
    if pack not in PACK_REGISTRY:
        raise PackNotFound(f"unknown pack '{pack}'")
    return pack


# ── Criterion results ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CriterionResult:
    """One checklist verdict."""

    criterion_id: str
    outcome: str
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterionId": self.criterion_id,
            "outcome": self.outcome,
            "note": self.note,
        }

    @property
    def blocks_approval(self) -> bool:
        return self.outcome == OUTCOME_FAIL


def normalize_criteria(
    results: Optional[Sequence[Mapping[str, Any]]],
) -> List[CriterionResult]:
    """Validate and normalise submitted checklist verdicts.

    Rejects an unknown criterion id, an unknown outcome, a duplicate verdict, and a
    ``not_applicable`` with no note. Order is preserved — the reviewer's own order
    through the checklist is part of the record.
    """
    if not results:
        raise ReviewValidationError("at least one criterion result is required")

    normalized: List[CriterionResult] = []
    seen: set = set()
    for entry in results:
        if not isinstance(entry, Mapping):
            raise ReviewValidationError(
                "each criterion result must be an object with criterionId and outcome"
            )
        criterion_id = str(entry.get("criterionId") or entry.get("criterion_id") or "").strip()
        if not criterion_id:
            raise ReviewValidationError("criterionId is required on every result")
        if not is_known_criterion(criterion_id):
            raise ReviewValidationError(
                f"unknown review criterion {criterion_id!r}; the checklist is: "
                f"{', '.join(CERTIFICATION_CRITERIA)}"
            )
        if criterion_id in seen:
            raise ReviewValidationError(
                f"criterion {criterion_id!r} has more than one verdict"
            )
        seen.add(criterion_id)

        outcome = str(entry.get("outcome") or "").strip().lower()
        if outcome not in CRITERION_OUTCOMES:
            raise ReviewValidationError(
                f"outcome for {criterion_id!r} must be one of "
                f"{', '.join(sorted(CRITERION_OUTCOMES))}, got {outcome!r}"
            )

        note = _optional_text(entry.get("note"))
        if outcome == OUTCOME_NOT_APPLICABLE and not note:
            raise ReviewValidationError(
                f"criterion {describe_criterion(criterion_id)} was marked "
                f"not_applicable without a note — a later auditor has to be able to "
                f"read why it did not apply"
            )
        normalized.append(CriterionResult(criterion_id, outcome, note))
    return normalized


def _assert_decision_is_supported(
    decision: str, criteria: Sequence[CriterionResult], notes: Optional[str]
) -> None:
    """Refuse a decision the checklist does not support.

    Approval requires a PASS on every required criterion — missing and failed are
    reported separately because they are different mistakes (one is an incomplete
    review, the other is a review that found a problem).

    A rejection requires a note. "Rejected, no reason recorded" is not auditable,
    and the pack author has nothing to act on.
    """
    if decision == DECISION_REJECTED:
        if not notes:
            raise ReviewDecisionError(
                "a rejected review must record why — notes is required"
            )
        return

    by_id = {result.criterion_id: result for result in criteria}
    missing = [cid for cid in REQUIRED_CRITERION_IDS if cid not in by_id]
    failed = [
        cid
        for cid in REQUIRED_CRITERION_IDS
        if cid in by_id and by_id[cid].outcome != OUTCOME_PASS
    ]
    if not missing and not failed:
        return

    clauses: List[str] = []
    if missing:
        clauses.append(
            "no verdict was recorded for "
            + ", ".join(describe_criterion(cid) for cid in missing)
        )
    if failed:
        clauses.append(
            "these required criteria did not pass: "
            + ", ".join(
                f"{describe_criterion(cid)} = {by_id[cid].outcome}" for cid in failed
            )
        )
    raise ReviewDecisionError(
        "This review cannot be approved: " + "; ".join(clauses) + "."
    )


# ── The review record ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackCertificationReview:
    """One recorded certification decision. Immutable once written."""

    review_id: str
    org_id: str
    pack_id: str
    pack_version: str
    revision: int
    reviewer_id: str
    reviewer_name: Optional[str]
    reviewed_at: str
    platform_version: str
    proposed_level: str
    decision: str
    criteria: List[CriterionResult] = field(default_factory=list)
    scope_summary: str = ""
    notes: Optional[str] = None

    @property
    def approved(self) -> bool:
        return self.decision == DECISION_APPROVED

    @property
    def criteria_ids(self) -> List[str]:
        """Criterion ids that PASSED — the list a certification scope declares.

        A failed or not-applicable criterion is deliberately excluded: the signed
        scope states what the certification actually covers, and listing an item
        the review did not pass would overstate it.
        """
        return [
            result.criterion_id
            for result in self.criteria
            if result.outcome == OUTCOME_PASS
        ]

    @property
    def summary(self) -> str:
        """One human sentence for a surface or an audit export."""
        level = LEVEL_LABELS.get(self.proposed_level, self.proposed_level)
        verb = "approved for" if self.approved else "did not approve"
        return (
            f"{self.reviewer_name or self.reviewer_id} {verb} {level} certification "
            f"of pack '{self.pack_id}' v{self.pack_version} on "
            f"{self.reviewed_at[:10]}, against platform version "
            f"{self.platform_version}."
        )

    def approved_declaration(
        self, *, certifying_entity: Optional[str] = None
    ) -> Dict[str, Any]:
        """The AT-831 certification block this review authorises, minus the signature.

        The bridge between the review and the badge: hand this to
        ``pack_certification.sign_certification`` (or sign
        :meth:`canonical_payload` offline) so what is signed is provably what was
        approved. The ``signature`` slot is present but EMPTY — this module cannot
        and must not mint one.

        Raises :class:`ReviewDecisionError` for a rejected review: there is nothing
        to sign, and returning a block anyway would invite exactly the mistake the
        gate exists to prevent.
        """
        if not self.approved:
            raise ReviewDecisionError(
                f"review {self.review_id} did not approve pack '{self.pack_id}' — "
                f"there is no certification to issue"
            )
        return {
            "level": self.proposed_level,
            "certifyingEntity": certifying_entity or CLOUDFULCRUM,
            "reviewDate": self.reviewed_at[:10],
            "reviewedAgainstPlatformVersion": self.platform_version,
            "scope": {
                "summary": self.scope_summary,
                "criteria": self.criteria_ids,
            },
            "signature": {"keyId": "", "algorithm": "", "value": ""},
        }

    def canonical_payload(
        self, *, certifying_entity: Optional[str] = None
    ) -> str:
        """The exact bytes (as UTF-8 text) the release key signs for this review."""
        declaration = self.approved_declaration(certifying_entity=certifying_entity)
        payload = certification_payload(self.pack_id, declaration)
        return canonical_payload_bytes(payload).decode("utf-8")

    def as_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "reviewId": self.review_id,
            "orgId": self.org_id,
            "packId": self.pack_id,
            "packVersion": self.pack_version,
            "revision": self.revision,
            "reviewerId": self.reviewer_id,
            "reviewerName": self.reviewer_name,
            "reviewedAt": self.reviewed_at,
            "reviewedAgainstPlatformVersion": self.platform_version,
            "proposedLevel": self.proposed_level,
            "decision": self.decision,
            "approved": self.approved,
            "criteria": [result.to_dict() for result in self.criteria],
            "passedCriteria": self.criteria_ids,
            "scopeSummary": self.scope_summary,
            "notes": self.notes,
            "summary": self.summary,
        }
        if self.approved:
            # Present only on an approval, so a consumer cannot accidentally build a
            # signing payload out of a rejection.
            record["certificationDeclaration"] = self.approved_declaration()
            record["canonicalPayload"] = self.canonical_payload()
        return record


# ── Store contract ────────────────────────────────────────────────────────────


class PackCertificationReviewStore:
    """Read/append contract. There is no update and no delete operation."""

    def record(self, review: PackCertificationReview) -> PackCertificationReview:
        raise NotImplementedError

    def next_revision(self, org_id: str, pack_id: str) -> int:
        raise NotImplementedError

    def list_for_pack(self, org_id: str, pack_id: str) -> List[PackCertificationReview]:
        """Newest-first review trail for one pack (repo audit convention)."""
        raise NotImplementedError

    def latest_per_pack(self, org_id: str) -> Dict[str, PackCertificationReview]:
        raise NotImplementedError


class InMemoryPackCertificationReviewStore(PackCertificationReviewStore):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._rows: List[PackCertificationReview] = []
        self._lock = threading.RLock()

    def record(self, review: PackCertificationReview) -> PackCertificationReview:
        with self._lock:
            self._rows.append(review)
        return review

    def next_revision(self, org_id: str, pack_id: str) -> int:
        with self._lock:
            revisions = [
                row.revision
                for row in self._rows
                if row.org_id == org_id and row.pack_id == pack_id
            ]
        return (max(revisions) + 1) if revisions else 1

    def list_for_pack(self, org_id: str, pack_id: str) -> List[PackCertificationReview]:
        with self._lock:
            rows = [
                row
                for row in self._rows
                if row.org_id == org_id and row.pack_id == pack_id
            ]
        return sorted(rows, key=lambda row: row.revision, reverse=True)

    def latest_per_pack(self, org_id: str) -> Dict[str, PackCertificationReview]:
        with self._lock:
            rows = [row for row in self._rows if row.org_id == org_id]
        latest: Dict[str, PackCertificationReview] = {}
        for row in sorted(rows, key=lambda row: row.revision):
            latest[row.pack_id] = row
        return latest


class PostgresPackCertificationReviewStore(PackCertificationReviewStore):
    """Production store. Migration 0034 / provision.sql provision its table."""

    def record(self, review: PackCertificationReview) -> PackCertificationReview:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO pack_certification_reviews
                    (id, org_id, pack_id, pack_version, revision, reviewer_id,
                     reviewer_name, reviewed_at, platform_version, proposed_level,
                     decision, criteria, scope_summary, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review.review_id,
                    review.org_id,
                    review.pack_id,
                    review.pack_version,
                    review.revision,
                    review.reviewer_id,
                    review.reviewer_name,
                    review.reviewed_at,
                    review.platform_version,
                    review.proposed_level,
                    review.decision,
                    json.dumps([result.to_dict() for result in review.criteria]),
                    review.scope_summary,
                    review.notes,
                ),
            )
            con.commit()
        finally:
            con.close()
        return review

    def next_revision(self, org_id: str, pack_id: str) -> int:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM pack_certification_reviews "
                "WHERE org_id = %s AND pack_id = %s",
                (org_id, pack_id),
            )
            row = cur.fetchone()
        finally:
            con.close()
        return int(row[0] or 0) + 1

    def list_for_pack(self, org_id: str, pack_id: str) -> List[PackCertificationReview]:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"SELECT {_COLUMNS} FROM pack_certification_reviews "
                "WHERE org_id = %s AND pack_id = %s ORDER BY revision DESC",
                (org_id, pack_id),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [_row_to_review(row) for row in rows]

    def latest_per_pack(self, org_id: str) -> Dict[str, PackCertificationReview]:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                f"SELECT DISTINCT ON (pack_id) {_COLUMNS} "
                "FROM pack_certification_reviews WHERE org_id = %s "
                "ORDER BY pack_id, revision DESC",
                (org_id,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return {row[2]: _row_to_review(row) for row in rows}


_COLUMNS = (
    "id, org_id, pack_id, pack_version, revision, reviewer_id, reviewer_name, "
    "reviewed_at, platform_version, proposed_level, decision, criteria, "
    "scope_summary, notes"
)


def _row_to_review(row: Sequence[Any]) -> PackCertificationReview:
    raw_criteria = row[11]
    if isinstance(raw_criteria, (str, bytes, bytearray)):
        raw_criteria = json.loads(raw_criteria)
    criteria = [
        CriterionResult(
            str(entry.get("criterionId")),
            str(entry.get("outcome")),
            entry.get("note"),
        )
        for entry in (raw_criteria or [])
    ]
    return PackCertificationReview(
        review_id=str(row[0]),
        org_id=str(row[1]),
        pack_id=str(row[2]),
        pack_version=str(row[3] or ""),
        revision=int(row[4]),
        reviewer_id=str(row[5]),
        reviewer_name=row[6],
        reviewed_at=_iso(row[7]),
        platform_version=str(row[8] or ""),
        proposed_level=str(row[9]),
        decision=str(row[10]),
        criteria=criteria,
        scope_summary=str(row[12] or ""),
        notes=row[13],
    )


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# ── Store selection ───────────────────────────────────────────────────────────

_STORE: Optional[PackCertificationReviewStore] = None


def get_review_store() -> PackCertificationReviewStore:
    global _STORE
    if _STORE is None:
        _STORE = PostgresPackCertificationReviewStore()
    return _STORE


def set_review_store(store: Optional[PackCertificationReviewStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


# ── Public API ────────────────────────────────────────────────────────────────


def record_certification_review(
    org_id: str,
    pack_id: str,
    *,
    reviewer_id: str,
    proposed_level: str,
    decision: str,
    criteria: Sequence[Mapping[str, Any]],
    scope_summary: str,
    reviewer_name: Optional[str] = None,
    notes: Optional[str] = None,
    platform_version: Optional[str] = None,
) -> PackCertificationReview:
    """Record one checklist-driven certification review.

    The reviewer, the pack version, the platform version, and the date are recorded
    from the SERVER's view — never taken from the caller's body — so the trail
    cannot be back-dated or attributed to somebody else.

    Raises :class:`~app.pack_state.PackNotFound` for an unregistered pack,
    :class:`ReviewValidationError` for a malformed checklist, and
    :class:`ReviewDecisionError` when the decision contradicts it. Not fail-soft:
    a review that did not persist must never look recorded.
    """
    from discovery.packs.pack_config import get_pack_version

    org = _required_text(org_id, "org_id", max_length=64)
    pack = _validated_pack_id(pack_id)
    reviewer = _required_text(reviewer_id, "reviewer_id", max_length=128)

    level = str(proposed_level or "").strip().lower()
    if level not in SIGNATURE_REQUIRED_LEVELS:
        # Community is self-declared — there is nothing for a reviewer to vouch for,
        # so a "review" recording it would be a record of no decision at all.
        raise ReviewValidationError(
            f"proposedLevel must be one of {', '.join(sorted(SIGNATURE_REQUIRED_LEVELS))} "
            f"— {', '.join(LEVEL_LABELS[lvl] for lvl in sorted(SIGNATURE_REQUIRED_LEVELS))} "
            f"are the levels a CloudFulcrum review can grant"
        )

    verdict = str(decision or "").strip().lower()
    if verdict not in REVIEW_DECISIONS:
        raise ReviewValidationError(
            f"decision must be one of {', '.join(sorted(REVIEW_DECISIONS))}, "
            f"got {decision!r}"
        )

    results = normalize_criteria(criteria)
    review_notes = _optional_text(notes)
    _assert_decision_is_supported(verdict, results, review_notes)

    summary = _required_text(scope_summary, "scopeSummary", max_length=1000)

    store = get_review_store()
    review = PackCertificationReview(
        review_id=f"pcr_{uuid4().hex}",
        org_id=org,
        pack_id=pack,
        pack_version=get_pack_version(pack),
        revision=store.next_revision(org, pack),
        reviewer_id=reviewer,
        reviewer_name=_optional_text(reviewer_name, max_length=256),
        reviewed_at=_now(),
        platform_version=platform_version or get_platform_version(),
        proposed_level=level,
        decision=verdict,
        criteria=results,
        scope_summary=summary,
        notes=review_notes,
    )
    return store.record(review)


def pack_review_history(org_id: str, pack_id: str) -> List[PackCertificationReview]:
    """Newest-first review trail for one pack. Raises if the store is unreadable."""
    return get_review_store().list_for_pack(
        _required_text(org_id, "org_id", max_length=64),
        str(pack_id or "").strip(),
    )


def latest_review(org_id: str, pack_id: str) -> Optional[PackCertificationReview]:
    """The most recent review of a pack, or ``None`` if it has never been reviewed."""
    history = pack_review_history(org_id, pack_id)
    return history[0] if history else None


def latest_reviews_safe(org_id: Optional[str]) -> Dict[str, PackCertificationReview]:
    """``{pack_id: latest review}``, degrading to ``{}`` on any failure.

    Fail-soft for the same reason as ``pack_state.disabled_pack_ids_safe``: a
    surface that shows "last reviewed on…" must not fail to render — or worse, fail
    a run — because the review store is unreachable or the migration has not been
    applied. The badge itself never depends on this: it comes from the signature.
    """
    if not org_id:
        return {}
    try:
        return get_review_store().latest_per_pack(org_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read certification reviews for org=%s; reporting none",
            org_id,
            exc_info=True,
        )
        return {}


def review_view(org_id: str, pack_id: str) -> Dict[str, Any]:
    """Review trail plus the pack's live certification status — the surfacing shape.

    Deliberately reports BOTH: the recorded human decision (this module) and the
    signature-verified badge (AT-831). Showing only the review would let an
    approved-but-unsigned pack read as Certified, which is precisely the confusion
    the two-step design exists to prevent.
    """
    from discovery.packs.pack_certification import get_pack_certification

    history = pack_review_history(org_id, pack_id)
    certification = get_pack_certification(pack_id)
    return {
        "orgId": org_id,
        "packId": pack_id,
        "certification": certification.to_dict(),
        "latestReview": history[0].as_dict() if history else None,
        "reviews": [review.as_dict() for review in history],
    }


__all__ = [
    "CRITERION_OUTCOMES",
    "CriterionResult",
    "DECISION_APPROVED",
    "DECISION_REJECTED",
    "InMemoryPackCertificationReviewStore",
    "OUTCOME_FAIL",
    "OUTCOME_NOT_APPLICABLE",
    "OUTCOME_PASS",
    "PackCertificationReview",
    "PackCertificationReviewStore",
    "PostgresPackCertificationReviewStore",
    "REVIEW_DECISIONS",
    "ReviewDecisionError",
    "ReviewValidationError",
    "get_review_store",
    "latest_review",
    "latest_reviews_safe",
    "normalize_criteria",
    "pack_review_history",
    "record_certification_review",
    "review_view",
    "set_review_store",
]
