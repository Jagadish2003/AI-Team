"""2.0-A2 T1 — persisted opportunity lifecycle, keyed on the stable identity.

The current row is convenient state; the append-only history is the record. Every
key and every query includes ``org_id`` — one org's lifecycle is never readable or
writable from another, and a cross-org read returns "not found" rather than
revealing that the identity exists elsewhere.

**Why the key is ``(org_id, opportunity_identity)`` and not a run.** Lifecycle is a
property of the PROBLEM, not of one observation of it. ``opportunity_identity`` is
computed only from run-invariant inputs, so the same real-world problem keeps one
id run after run; storing state per-run would reset it every time a run
re-surfaced the finding. :func:`ensure_tracked` is therefore insert-only — a run
that re-surfaces an opportunity never resets its state.

**The non-inference rule.** :func:`record_action` is the ONLY way to reach
``actioned``, it takes ``action_date`` as a required argument with no default, and
it is hard-wired to ``ACTOR_HUMAN``. :func:`system_transition` refuses to target
``actioned`` at all. Legality and date rules live in
``opportunity_lifecycle_states`` — this module persists decisions, it does not
make them.

This subtask ships no measurement: it is the skeleton T2–T7 hang on.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import uuid4

from . import db
from .opportunity_lifecycle_states import (
    ACTOR_HUMAN,
    ACTOR_SYSTEM,
    INITIAL_STATE,
    STATE_ACTIONED,
    STATE_DISMISSED,
    STATE_OPEN,
    LifecycleTransitionError,
    is_measurable,
    legal_transitions_from,
    validate_transition,
)
from database.models.opportunity_lifecycle import ALL_OPPORTUNITY_LIFECYCLE_DDL

logger = logging.getLogger(__name__)

_TABLES_READY = False


class OpportunityLifecycleNotFound(LookupError):
    """No lifecycle row for this (org, identity). Also raised cross-org."""


def ensure_opportunity_lifecycle_tables() -> None:
    """Create the lifecycle tables if absent (idempotent, never raises).

    The authoritative creator is migration ``0031``; this runtime helper is a
    safety net for a dev DB that has not been migrated yet, mirroring
    ``ensure_opportunity_instances_table()``.

    Least-privilege roles: production runs under a role with no CREATE on the
    schema, where the tables are already provisioned. So the existence check is
    READ-ONLY first (information_schema needs only SELECT) and the DDL runs only
    when the table is genuinely absent — issuing ``CREATE TABLE`` up front is
    rejected with "permission denied for schema public" even with IF NOT EXISTS.
    """
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    ("opportunity_lifecycle",),
                )
                if cur.fetchone() is None:
                    for ddl in ALL_OPPORTUNITY_LIFECYCLE_DDL:
                        cur.execute(ddl)
            con.commit()
        _TABLES_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_opportunity_lifecycle_tables skipped (assuming provisioned): %s",
            exc,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LifecycleTransitionError(f"{name} is required")
    return text


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------
# Row shaping
# --------------------------------------------------------------------------

_STATE_SELECT = (
    "SELECT org_id, opportunity_identity, state, action_date, actioned_by, "
    "actioned_at, revision, first_seen_run_id, last_run_id, last_transition_at, "
    "updated_by, created_at, updated_at FROM opportunity_lifecycle"
)


def _row_to_state(row: Sequence[Any]) -> Dict[str, Any]:
    """API-shaped (camelCase) lifecycle state.

    ``measurable`` is served rather than left for a caller to derive: T3/T7 must
    not re-implement "which states permit a measurement", and a UI should not
    either.
    """
    state = row[2]
    return {
        "orgId": row[0],
        "opportunityIdentity": row[1],
        "state": state,
        "actionDate": _iso(row[3]),
        "actionedBy": row[4],
        "actionedAt": _iso(row[5]),
        "revision": row[6],
        "firstSeenRunId": row[7],
        "lastRunId": row[8],
        "lastTransitionAt": _iso(row[9]),
        "updatedBy": row[10],
        "createdAt": _iso(row[11]),
        "updatedAt": _iso(row[12]),
        "measurable": is_measurable(state),
        "legalNextStates": sorted({t.to_state for t in legal_transitions_from(state)}),
    }


def _row_to_history(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "id": row[0],
        "opportunityIdentity": row[1],
        "revision": row[2],
        "fromState": row[3],
        "toState": row[4],
        "actor": row[5],
        "actorId": row[6],
        "actionDate": _iso(row[7]),
        "reason": row[8],
        "note": row[9],
        "runId": row[10],
        "transitionedAt": _iso(row[11]),
    }


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_lifecycle(org_id: str, opportunity_identity: str) -> Optional[Dict[str, Any]]:
    """The current state, or ``None`` when this org has no row for the identity."""
    org = _required(org_id, "org_id")
    identity = _required(opportunity_identity, "opportunity_identity")

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                _STATE_SELECT + " WHERE org_id = %s AND opportunity_identity = %s",
                (org, identity),
            )
            row = cur.fetchone()
    return _row_to_state(row) if row else None


def get_lifecycle_or_raise(org_id: str, opportunity_identity: str) -> Dict[str, Any]:
    state = get_lifecycle(org_id, opportunity_identity)
    if state is None:
        raise OpportunityLifecycleNotFound(
            f"no lifecycle record for opportunity {opportunity_identity!r}"
        )
    return state


def list_lifecycles(
    org_id: str,
    *,
    states: Optional[Sequence[str]] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Every lifecycle row in ONE org, newest transition first.

    The read 2.0-A2 T6's portfolio view builds on. Filtering by state is
    supported so "all actioned opportunities" is one query.
    """
    org = _required(org_id, "org_id")
    sql = _STATE_SELECT + " WHERE org_id = %s"
    params: List[Any] = [org]
    if states:
        placeholders = ", ".join(["%s"] * len(states))
        sql += f" AND state IN ({placeholders})"
        params.extend(states)
    sql += " ORDER BY last_transition_at DESC NULLS LAST, opportunity_identity ASC LIMIT %s"
    params.append(max(1, min(int(limit), 1000)))

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [_row_to_state(r) for r in rows]


def get_lifecycle_history(
    org_id: str, opportunity_identity: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    """The append-only transition history, oldest first.

    Oldest-first because the series tells a story — including an analyst
    unwinding their own mistake, which is a forward row rather than an edit.
    """
    org = _required(org_id, "org_id")
    identity = _required(opportunity_identity, "opportunity_identity")

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, opportunity_identity, revision, from_state, to_state, "
                "actor, actor_id, action_date, reason, note, run_id, transitioned_at "
                "FROM opportunity_lifecycle_history "
                "WHERE org_id = %s AND opportunity_identity = %s "
                "ORDER BY revision ASC LIMIT %s",
                (org, identity, max(1, min(int(limit), 1000))),
            )
            rows = cur.fetchall()
    return [_row_to_history(r) for r in rows]


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------


def ensure_tracked(
    org_id: str,
    opportunity_identity: str,
    *,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Start tracking an opportunity at ``open``, or leave its state untouched.

    INSERT-ONLY on conflict. This is what makes "a run that re-surfaces an
    opportunity does not reset its lifecycle" true: a second run updates only
    ``last_run_id``, never ``state``, ``action_date`` or ``revision``.
    """
    org = _required(org_id, "org_id")
    identity = _required(opportunity_identity, "opportunity_identity")
    now = _now()

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_lifecycle ("
                "  org_id, opportunity_identity, state, revision, first_seen_run_id,"
                "  last_run_id, created_at, updated_at"
                ") VALUES (%s, %s, %s, 0, %s, %s, %s, %s) "
                # DO UPDATE touches ONLY the last-seen run pointer. Any clause
                # here that wrote state/action_date would silently undo an
                # analyst's recorded action on the next run.
                "ON CONFLICT (org_id, opportunity_identity) DO UPDATE SET "
                "  last_run_id = COALESCE(EXCLUDED.last_run_id, opportunity_lifecycle.last_run_id),"
                "  updated_at = EXCLUDED.updated_at",
                (org, identity, INITIAL_STATE, run_id, run_id, now, now),
            )
        con.commit()
    return get_lifecycle_or_raise(org, identity)


def ensure_tracked_many(
    org_id: str, opportunity_identities: Sequence[str], *, run_id: Optional[str] = None
) -> int:
    """Track a run's opportunities. Non-blocking: never breaks a discovery run.

    Returns the number tracked (existing rows count — they are confirmed tracked,
    just not reset).
    """
    tracked = 0
    for identity in opportunity_identities or ():
        if not str(identity or "").strip():
            continue
        try:
            ensure_tracked(org_id, identity, run_id=run_id)
            tracked += 1
        except Exception as exc:  # noqa: BLE001 - lifecycle must not fail a run
            logger.warning(
                "Could not track opportunity lifecycle (identity %s): %s",
                identity,
                exc,
            )
    return tracked


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def _apply_transition(
    org_id: str,
    opportunity_identity: str,
    to_state: str,
    actor: str,
    actor_id: str,
    *,
    action_date: object = None,
    note: Optional[str] = None,
    run_id: Optional[str] = None,
    now_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Validate, persist, append history, audit and emit. The one write path.

    Every transition — human or system — goes through here, so no caller can
    skip validation, forget the history row, or omit the audit event.
    """
    org = _required(org_id, "org_id")
    identity = _required(opportunity_identity, "opportunity_identity")
    who = _required(actor_id, "actor_id")

    current = get_lifecycle_or_raise(org, identity)
    validated = validate_transition(
        current["state"], to_state, actor, action_date=action_date, now=now_date
    )

    now = _now()
    revision = int(current["revision"]) + 1
    stored_action_date: Optional[date]
    if validated.action_date is not None:
        stored_action_date = validated.action_date
    elif validated.clear_action_date:
        stored_action_date = None
    else:
        # Carried forward: a system move must never disturb the human-recorded
        # pivot every later measurement is computed from.
        existing = current.get("actionDate")
        stored_action_date = date.fromisoformat(existing) if existing else None

    # Who/when the action was recorded. Resolved in Python rather than with
    # nested SQL CASE expressions: the three cases are a decision about intent,
    # and they read far more clearly here than as conditional column updates.
    if validated.to_state == STATE_ACTIONED:
        actioned_by, actioned_at = who, now
    elif validated.clear_action_date:
        # The unwind clears the attribution along with the date.
        actioned_by, actioned_at = None, None
    else:
        actioned_by = current.get("actionedBy")
        existing_at = current.get("actionedAt")
        actioned_at = datetime.fromisoformat(existing_at) if existing_at else None

    with closing(db.connect()) as con:
        with con.cursor() as cur:
            # Guarded by the revision we read, so two concurrent transitions
            # cannot both apply — the loser updates 0 rows and is told to retry.
            cur.execute(
                "UPDATE opportunity_lifecycle SET "
                "  state = %s, action_date = %s, revision = %s,"
                "  actioned_by = %s, actioned_at = %s,"
                "  last_run_id = COALESCE(%s, last_run_id),"
                "  last_transition_at = %s, updated_by = %s, updated_at = %s "
                "WHERE org_id = %s AND opportunity_identity = %s AND revision = %s",
                (
                    validated.to_state,
                    stored_action_date,
                    revision,
                    actioned_by,
                    actioned_at,
                    run_id,
                    now,
                    who,
                    now,
                    org,
                    identity,
                    current["revision"],
                ),
            )
            if cur.rowcount != 1:
                raise LifecycleTransitionError(
                    "lifecycle changed concurrently; re-read the state and retry"
                )

            cur.execute(
                "INSERT INTO opportunity_lifecycle_history ("
                "  id, org_id, opportunity_identity, revision, from_state, to_state,"
                "  actor, actor_id, action_date, reason, note, run_id, transitioned_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    f"lch_{uuid4().hex[:20]}",
                    org,
                    identity,
                    revision,
                    current["state"],
                    validated.to_state,
                    actor,
                    who,
                    stored_action_date,
                    validated.reason,
                    note,
                    run_id,
                    now,
                ),
            )
        con.commit()

    _audit_and_emit(
        org_id=org,
        opportunity_identity=identity,
        from_state=current["state"],
        to_state=validated.to_state,
        actor=actor,
        actor_id=who,
        action_date=stored_action_date,
        revision=revision,
        run_id=run_id,
    )
    return get_lifecycle_or_raise(org, identity)


def _audit_and_emit(
    *,
    org_id: str,
    opportunity_identity: str,
    from_state: str,
    to_state: str,
    actor: str,
    actor_id: str,
    action_date: Optional[date],
    revision: int,
    run_id: Optional[str],
) -> None:
    """Audit row + telemetry event. Neither may break a completed transition.

    The transition is already committed by the time this runs, so a failure here
    must not raise — but it is logged, because a state change with no audit row
    is exactly the gap an enterprise review looks for.
    """
    try:
        from .middleware.audit import OPPORTUNITY_LIFECYCLE_TRANSITIONED, log_event

        # Fields are passed as individual kwargs, not pre-serialised: log_event
        # pops org_id/user_id/run_id/connector_id into their own columns and
        # JSON-encodes whatever remains into `payload`. Passing a JSON string
        # would double-encode it and make the payload awkward to query.
        log_event(
            OPPORTUNITY_LIFECYCLE_TRANSITIONED,
            org_id=org_id,
            user_id=actor_id,
            run_id=run_id,
            target=opportunity_identity,
            opportunity_identity=opportunity_identity,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            action_date=action_date.isoformat() if action_date else None,
            revision=revision,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lifecycle audit write failed: %s", exc)

    try:
        from .telemetry import record_event

        record_event(
            "opportunity.lifecycle_transitioned",
            {
                "org_id": org_id,
                "opportunity_identity": opportunity_identity,
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "action_date": action_date.isoformat() if action_date else None,
                "revision": revision,
                "run_id": run_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lifecycle telemetry emit failed: %s", exc)


# --------------------------------------------------------------------------
# The public transition API
# --------------------------------------------------------------------------


def record_action(
    org_id: str,
    opportunity_identity: str,
    action_date: object,
    actor_id: str,
    *,
    note: Optional[str] = None,
    run_id: Optional[str] = None,
    now_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Record that a human deployed a change, on an explicit date.

    ``action_date`` is a REQUIRED POSITIONAL argument with no default — the one
    signature decision that makes the non-inference rule structural rather than
    aspirational. A caller cannot reach ``actioned`` without supplying the date,
    and a missing or future date raises rather than being coerced.

    Hard-wired to :data:`ACTOR_HUMAN`: there is no parameter by which a
    background job could present itself as a person.
    """
    return _apply_transition(
        org_id,
        opportunity_identity,
        STATE_ACTIONED,
        ACTOR_HUMAN,
        actor_id,
        action_date=action_date,
        note=note,
        run_id=run_id,
        now_date=now_date,
    )


def dismiss(
    org_id: str,
    opportunity_identity: str,
    actor_id: str,
    *,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyst-driven dismissal, legal from any non-dismissed state."""
    return _apply_transition(
        org_id,
        opportunity_identity,
        STATE_DISMISSED,
        ACTOR_HUMAN,
        actor_id,
        note=note,
    )


def reopen(
    org_id: str,
    opportunity_identity: str,
    actor_id: str,
    *,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Unwind to ``open``, clearing any recorded action date.

    The reversibility the subtask requires: an analyst who actioned the wrong
    opportunity can undo it. The unwind is a NEW forward history row — the
    record of the original mistake is never rewritten away.
    """
    return _apply_transition(
        org_id, opportunity_identity, STATE_OPEN, ACTOR_HUMAN, actor_id, note=note
    )


def system_transition(
    org_id: str,
    opportunity_identity: str,
    to_state: str,
    *,
    run_id: Optional[str] = None,
    note: Optional[str] = None,
    actor_id: str = "system",
) -> Dict[str, Any]:
    """A platform-driven move (monitoring / measured / stalled).

    Cannot reach ``actioned``: that transition is declared ``ACTOR_HUMAN`` only,
    so :func:`validate_transition` refuses it here with a named reason. This is
    the second half of the non-inference rule — T3's background measurement
    caller physically cannot decide that something was deployed.
    """
    return _apply_transition(
        org_id,
        opportunity_identity,
        to_state,
        ACTOR_SYSTEM,
        actor_id,
        note=note,
        run_id=run_id,
    )


__all__ = [
    "OpportunityLifecycleNotFound",
    "dismiss",
    "ensure_opportunity_lifecycle_tables",
    "ensure_tracked",
    "ensure_tracked_many",
    "get_lifecycle",
    "get_lifecycle_history",
    "get_lifecycle_or_raise",
    "list_lifecycles",
    "record_action",
    "reopen",
    "system_transition",
]
