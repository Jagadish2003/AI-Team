"""2.0-A3 T1 — the analyst-decision half of the learning signal set.

Accept / dismiss / defer-with-reason, keyed on ``opportunity_identity``, stored
append-only so a decision survives the run that produced it.

Read ``database/models/opportunity_feedback.py`` for why this is a new record
rather than a widened ``decision`` enum. The short version: the existing
``decision`` field lives in the run-scoped ``opps`` KV blob that materialization
rewrites wholesale and replay resets, it is addressed by a per-run id rather than
the stable identity, it carries no per-decision id for AC2 to link to, and its
validation is shared with EVIDENCE decisions where ``defer`` is meaningless.

**This module is deliberately dumb about weighting.** It records what happened;
``learning_signals.py`` decides what it is worth. Keeping the two apart is what
lets the weighting change (config, no deploy) without rewriting history — and it
means a stored decision is a fact, not a fact multiplied by whatever the weights
happened to be that week.

**Reason codes are a closed vocabulary.** Free text is refused, for two reasons
that both matter: a reason the learning layer cannot group on teaches it nothing,
and free text entering a learning input is an unbounded PII surface. An analyst
who needs to elaborate may — ``reason_detail`` is carried for the explainability
surface and for audit, and is never parsed, grouped on, or used as an input.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import db

logger = logging.getLogger(__name__)

FEEDBACK_SCHEMA_VERSION = "1.0.0"

ACTION_ACCEPT = "accept"
ACTION_DISMISS = "dismiss"
ACTION_DEFER = "defer"

#: The learning actions. Deliberately NOT the review enum's vocabulary — see the
#: module docstring; conflating the two re-introduces the coupling this record
#: exists to avoid.
FEEDBACK_ACTIONS = (ACTION_ACCEPT, ACTION_DISMISS, ACTION_DEFER)

#: The closed defer vocabulary. Mirrored in config/learning_signals.json, which
#: assigns each a multiplier; this tuple is what the API validates against.
DEFER_REASON_NO_CAPACITY = "no_capacity"
DEFER_REASON_BLOCKED = "blocked_by_dependency"
DEFER_REASON_AWAITING_APPROVAL = "awaiting_approval"
DEFER_REASON_NEEDS_EVIDENCE = "needs_more_evidence"
DEFER_REASON_TIMING = "timing_not_right"
DEFER_REASON_LOWER_PRIORITY = "lower_priority"
DEFER_REASON_OTHER = "other"

DEFER_REASONS = (
    DEFER_REASON_NO_CAPACITY,
    DEFER_REASON_BLOCKED,
    DEFER_REASON_AWAITING_APPROVAL,
    DEFER_REASON_NEEDS_EVIDENCE,
    DEFER_REASON_TIMING,
    DEFER_REASON_LOWER_PRIORITY,
    DEFER_REASON_OTHER,
)

#: How much elaboration is carried. Long enough to be useful in a review, short
#: enough that the column is not a document store.
MAX_REASON_DETAIL_CHARS = 500


class FeedbackError(ValueError):
    """A decision that cannot be recorded as stated."""


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def ensure_opportunity_feedback_table() -> None:
    """Create the table and its indexes. Startup-only, like the sibling stores."""
    from database.models.opportunity_feedback import ALL_OPPORTUNITY_FEEDBACK_DDL

    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                for statement in ALL_OPPORTUNITY_FEEDBACK_DDL:
                    cur.execute(statement)
            con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not ensure opportunity_feedback table: %s", exc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def validate_feedback(
    action: str, reason_code: Optional[str]
) -> tuple[str, Optional[str]]:
    """Normalise and check an action/reason pair.

    A defer without a reason is refused rather than defaulted. "Not now" with no
    stated why is the one decision shape that carries no information at all, and
    a layer that accepted it would be inventing the reason it then learned from.
    """
    normalised = _clean(action).lower()
    if normalised not in FEEDBACK_ACTIONS:
        raise FeedbackError(
            f"unknown feedback action {action!r}; expected one of "
            f"{', '.join(FEEDBACK_ACTIONS)}"
        )

    reason = _clean(reason_code).lower() or None
    if normalised == ACTION_DEFER:
        if not reason:
            raise FeedbackError(
                "a defer must state a reason: 'not now' with no stated why carries "
                f"no learnable information. Expected one of {', '.join(DEFER_REASONS)}"
            )
        if reason not in DEFER_REASONS:
            raise FeedbackError(
                f"unknown defer reason {reason_code!r}; expected one of "
                f"{', '.join(DEFER_REASONS)}. Free-text reasons are refused — use "
                "'other' with reason_detail for the genuinely unclassifiable case."
            )
    elif reason is not None and reason not in DEFER_REASONS:
        # Accept/dismiss may carry a reason, but only from the same vocabulary,
        # so grouping stays possible across every action.
        raise FeedbackError(
            f"unknown reason code {reason_code!r}; expected one of "
            f"{', '.join(DEFER_REASONS)}"
        )
    return normalised, reason


# --------------------------------------------------------------------------
# Writing — append only
# --------------------------------------------------------------------------


def record_feedback(
    org_id: str,
    opportunity_identity: str,
    action: str,
    *,
    actor_id: str,
    reason_code: Optional[str] = None,
    reason_detail: Optional[str] = None,
    detector_id: Optional[str] = None,
    pack_id: Optional[str] = None,
    signal_concept: Optional[str] = None,
    run_id: Optional[str] = None,
    recorded_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Append one analyst decision. Never updates; never deletes.

    An analyst who changes their mind appends a new row. What the team thought at
    the time is itself part of the learning record, and a store that edits its own
    history cannot answer "why was this ranked higher last month?".

    Returns:
        The stored record, including its ``feedbackId`` — the handle AC2's
        "links to the contributing decisions" resolves against.
    """
    org = _clean(org_id)
    identity = _clean(opportunity_identity)
    actor = _clean(actor_id)
    if not org:
        raise FeedbackError("org_id is required")
    if not identity:
        raise FeedbackError(
            "opportunity_identity is required: a decision keyed on a run-scoped "
            "opportunity id cannot inform the next run's ranking, which is the "
            "only thing this record exists to do"
        )
    if not actor:
        raise FeedbackError("actor_id is required — an unattributed decision is not auditable")

    normalised_action, reason = validate_feedback(action, reason_code)
    detail = _clean(reason_detail)[:MAX_REASON_DETAIL_CHARS] or None
    when = recorded_at or datetime.now(timezone.utc)
    feedback_id = f"fb_{uuid.uuid4().hex[:20]}"

    record = {
        "schemaVersion": FEEDBACK_SCHEMA_VERSION,
        "feedbackId": feedback_id,
        "orgId": org,
        "opportunityIdentity": identity,
        "action": normalised_action,
        "reasonCode": reason,
        "reasonDetail": detail,
        "actorId": actor,
        "detectorId": _clean(detector_id) or None,
        "packId": _clean(pack_id) or None,
        "signalConcept": _clean(signal_concept) or None,
        "runId": _clean(run_id) or None,
        "recordedAt": when.isoformat(),
    }

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_feedback ("
                "  feedback_id, org_id, opportunity_identity, action, reason_code,"
                "  reason_detail, actor_id, detector_id, pack_id, signal_concept,"
                "  run_id, recorded_at, record"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    feedback_id,
                    org,
                    identity,
                    normalised_action,
                    reason,
                    detail,
                    actor,
                    record["detectorId"],
                    record["packId"],
                    record["signalConcept"],
                    record["runId"],
                    when,
                    json.dumps(record),
                ),
            )
        con.commit()

    _audit(record)
    return record


def _audit(record: Mapping[str, Any]) -> None:
    """Audit row for the decision. Must never break a recorded decision.

    Note what is NOT here: a telemetry emission. The learning layer neither reads
    from nor writes to ``telemetry.py``, so that "learning from what was clicked"
    is impossible by construction rather than by convention — see
    ``tests/unit/test_learning_signal_isolation.py``.
    """
    try:
        from .middleware.audit import OPPORTUNITY_FEEDBACK_RECORDED, log_event

        log_event(
            OPPORTUNITY_FEEDBACK_RECORDED,
            org_id=record.get("orgId"),
            user_id=record.get("actorId"),
            run_id=record.get("runId"),
            target=record.get("opportunityIdentity"),
            feedback_id=record.get("feedbackId"),
            opportunity_identity=record.get("opportunityIdentity"),
            action=record.get("action"),
            reason_code=record.get("reasonCode"),
            detector_id=record.get("detectorId"),
            pack_id=record.get("packId"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feedback audit write failed: %s", exc)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_SELECT = "SELECT record FROM opportunity_feedback"


def _rows_to_records(rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        raw = row[0]
        if isinstance(raw, Mapping):
            out.append(dict(raw))
            continue
        try:
            parsed = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            logger.warning("Stored feedback record is not valid JSON; skipping")
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def get_feedback(org_id: str, feedback_id: str) -> Optional[Dict[str, Any]]:
    """One decision by id — what an explainability link resolves to."""
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND feedback_id = %s",
                (_clean(org_id), _clean(feedback_id)),
            )
            row = cur.fetchone()
    if not row:
        return None
    records = _rows_to_records([row])
    return records[0] if records else None


def get_feedback_history(
    org_id: str, opportunity_identity: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    """Every decision ever recorded about one opportunity, oldest first."""
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _SELECT + " WHERE org_id = %s AND opportunity_identity = %s"
                " ORDER BY recorded_at ASC, feedback_id ASC LIMIT %s",
                (
                    _clean(org_id),
                    _clean(opportunity_identity),
                    max(1, min(int(limit), 1000)),
                ),
            )
            rows = cur.fetchall()
    return _rows_to_records(rows)


def list_feedback(
    org_id: str,
    *,
    actions: Optional[Sequence[str]] = None,
    detector_ids: Optional[Sequence[str]] = None,
    pack_ids: Optional[Sequence[str]] = None,
    identities: Optional[Sequence[str]] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Decisions in one org, newest first. The signal set's read path.

    Every query is org-scoped in its WHERE clause, not filtered after the fact —
    AC6's isolation has to hold at the SQL layer or it does not hold.
    """
    sql = _SELECT + " WHERE org_id = %s"
    params: List[Any] = [_clean(org_id)]

    if actions:
        placeholders = ", ".join(["%s"] * len(actions))
        sql += f" AND action IN ({placeholders})"
        params.extend(_clean(a).lower() for a in actions)
    if detector_ids:
        placeholders = ", ".join(["%s"] * len(detector_ids))
        sql += f" AND detector_id IN ({placeholders})"
        params.extend(_clean(d) for d in detector_ids)
    if pack_ids:
        placeholders = ", ".join(["%s"] * len(pack_ids))
        sql += f" AND pack_id IN ({placeholders})"
        params.extend(_clean(p) for p in pack_ids)
    if identities:
        placeholders = ", ".join(["%s"] * len(identities))
        sql += f" AND opportunity_identity IN ({placeholders})"
        params.extend(_clean(i) for i in identities)

    sql += " ORDER BY recorded_at DESC, feedback_id DESC LIMIT %s"
    params.append(max(1, min(int(limit), 5000)))

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return _rows_to_records(rows)


def count_feedback(org_id: str) -> int:
    """Total decisions in one org — one half of the AC4 cold-start check."""
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM opportunity_feedback WHERE org_id = %s",
                (_clean(org_id),),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def latest_feedback_by_identity(
    org_id: str, *, limit: int = 2000
) -> Dict[str, Dict[str, Any]]:
    """The most recent decision per opportunity in one org.

    The signal set counts a team's CURRENT position on each finding, not the
    number of times someone clicked. An analyst who deferred and then accepted
    holds one position, and the history — which is preserved in full — is for
    audit and explanation, not for accumulating weight.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for record in list_feedback(org_id, limit=limit):
        identity = record.get("opportunityIdentity")
        if identity and identity not in latest:
            # list_feedback is newest-first, so the first sighting wins.
            latest[identity] = record
    return latest


__all__ = [
    "ACTION_ACCEPT",
    "ACTION_DEFER",
    "ACTION_DISMISS",
    "DEFER_REASONS",
    "FEEDBACK_ACTIONS",
    "FEEDBACK_SCHEMA_VERSION",
    "MAX_REASON_DETAIL_CHARS",
    "FeedbackError",
    "count_feedback",
    "ensure_opportunity_feedback_table",
    "get_feedback",
    "get_feedback_history",
    "latest_feedback_by_identity",
    "list_feedback",
    "record_feedback",
    "validate_feedback",
]
