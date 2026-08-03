"""2.0-B2 T5 — the dependent-finding re-evaluation work list.

The second half of AC4: "unmerge restores constituents **and flags dependent
findings for re-evaluation on the next run**."

Two things had to be decided before any of this could be honest.

**What a flag is keyed on.** ``(org_id, opportunity_identity)`` — the STABLE
cross-run identity — following 2.0-A2's lifecycle store, and for the same reason:
the need for re-evaluation is a property of the PROBLEM, not of the run that
happened to observe it. Run-scoped KV structurally cannot answer "which findings
are still awaiting re-evaluation?", and "on the next run" is exactly a cross-run
question. (``opportunity_identity`` is derived from detector + signal source, not
from graph entity row ids, so an unmerge never changes the key it is filed under —
which is what makes the flag findable after the change that raised it.)

**What "re-evaluated" means.** Not "someone looked at it". A flag is cleared by the
run that re-observes the finding, and that run's id is recorded. So the store can
answer "was this re-evaluated?" with a fact rather than an intention, and a finding
that stops appearing keeps its flag instead of being silently considered handled.

This module is deliberately not unmerge-specific: ``trigger_kind`` and ``reason``
are columns, and an entity unmerge is only the first producer. A future alias-table
edit or a deliberate re-merge needs the same work list, and should not have to
invent a second one.
"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from database.models.entity_unmerges import (
    ALL_ENTITY_UNMERGE_DDL,
    FINDING_REEVALUATION_FLAG_COLUMNS,
)

logger = logging.getLogger(__name__)

#: A flag is raised, then cleared by the run that re-observed the finding.
STATUS_PENDING = "pending"
STATUS_CLEARED = "cleared"

#: Why re-evaluation is needed. The reason is stored, not derived at read time, so
#: a flag raised months ago still explains itself.
REASON_ENTITY_UNMERGED = "entity_unmerged"

#: What raised it. ``entity_unmerge`` is the only producer today (see the module
#: docstring on why this is a column rather than an assumption).
TRIGGER_ENTITY_UNMERGE = "entity_unmerge"

_TABLES_READY = False


class ReevaluationFlagError(ValueError):
    """A flag could not be raised or cleared as asked."""


def ensure_reevaluation_tables() -> None:
    """Create the T5 tables if absent (idempotent, never raises).

    The authoritative creator is migration ``0034``; this is the dev-DB safety net,
    mirroring ``ensure_opportunity_lifecycle_tables``. Read-only existence check
    first, because production runs under a role with no CREATE on the schema, where
    an unconditional ``CREATE TABLE`` is refused even with ``IF NOT EXISTS``.
    """
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                    ("finding_reevaluation_flags",),
                )
                if cur.fetchone() is None:
                    for ddl in ALL_ENTITY_UNMERGE_DDL:
                        cur.execute(ddl)
            con.commit()
        _TABLES_READY = True
    except Exception as exc:  # noqa: BLE001 — a provisioned DB is the normal case.
        logger.warning(
            "ensure_reevaluation_tables skipped (assuming provisioned): %s", exc
        )


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


def _loads_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [_text(v) for v in value if _text(v)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:  # noqa: BLE001 — corrupt payload must not break a read.
            return []
        if isinstance(parsed, list):
            return [_text(v) for v in parsed if _text(v)]
    return []


# ── the flag ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReevaluationFlag:
    """One finding awaiting re-evaluation, in API shape."""

    org_id: str
    opportunity_identity: str
    status: str
    reason: str
    trigger_kind: str
    trigger_ref: Optional[str] = None
    entity_ids: Tuple[str, ...] = ()
    flagged_run_id: Optional[str] = None
    flagged_by: str = ""
    flagged_at: Optional[str] = None
    updated_at: Optional[str] = None
    cleared_run_id: Optional[str] = None
    cleared_at: Optional[str] = None

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opportunityIdentity": self.opportunity_identity,
            "status": self.status,
            "reason": self.reason,
            "triggerKind": self.trigger_kind,
            "triggerRef": self.trigger_ref,
            "entityIds": list(self.entity_ids),
            "flaggedRunId": self.flagged_run_id,
            "flaggedBy": self.flagged_by,
            "flaggedAt": self.flagged_at,
            "updatedAt": self.updated_at,
            "clearedRunId": self.cleared_run_id,
            "clearedAt": self.cleared_at,
        }


def _row_to_flag(row: Sequence[Any]) -> ReevaluationFlag:
    return ReevaluationFlag(
        org_id=_text(row[0]),
        opportunity_identity=_text(row[1]),
        status=_text(row[2]),
        reason=_text(row[3]),
        trigger_kind=_text(row[4]),
        trigger_ref=row[5],
        entity_ids=tuple(_loads_list(row[6])),
        flagged_run_id=row[7],
        flagged_by=_text(row[8]),
        flagged_at=_iso(row[9]),
        updated_at=_iso(row[10]),
        cleared_run_id=row[11],
        cleared_at=_iso(row[12]),
    )


_SELECT = f"SELECT {', '.join(FINDING_REEVALUATION_FLAG_COLUMNS)} FROM finding_reevaluation_flags"


@dataclass(frozen=True)
class FlagReport:
    """What one flagging pass did — reported per outcome, never summarised into a
    single number a reader has to trust."""

    flagged: int = 0
    refreshed: int = 0
    identities: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.flagged + self.refreshed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flagged": self.flagged,
            "refreshed": self.refreshed,
            "total": self.total,
            "identities": list(self.identities),
        }


def flag_findings(
    org_id: str,
    identities: Iterable[str],
    *,
    reason: str,
    trigger_kind: str = TRIGGER_ENTITY_UNMERGE,
    trigger_ref: Optional[str] = None,
    entity_ids: Optional[Sequence[str]] = None,
    run_ids: Optional[Dict[str, str]] = None,
    actor: str = "system",
) -> FlagReport:
    """Flag each finding identity for re-evaluation on the next run.

    Idempotent by key, and deliberately asymmetric about what a re-flag may change:
    the trigger, the entity list and the reason move to the newest cause, but
    ``flagged_at`` does NOT — the wait began when the finding was first flagged, and
    a second unmerge must not reset the clock on a finding that has been waiting
    through several runs. A flag that had already been cleared is re-raised: the
    finding was re-evaluated against the previous change, not this one.
    """
    org = _text(org_id)
    if not org:
        raise ReevaluationFlagError("flagging must be scoped to an org")
    reason_text = _text(reason)
    if not reason_text:
        raise ReevaluationFlagError("a flag must record why re-evaluation is needed")

    wanted = []
    seen = set()
    for identity in identities or ():
        text = _text(identity)
        if text and text not in seen:
            seen.add(text)
            wanted.append(text)
    if not wanted:
        return FlagReport()

    ensure_reevaluation_tables()
    now = _now()
    payload = json.dumps(list(entity_ids or []))
    run_map = run_ids or {}

    flagged = 0
    refreshed = 0
    with closing(db.connect()) as con:
        try:
            cur = con.cursor()
            for identity in wanted:
                cur.execute(
                    """
                    INSERT INTO finding_reevaluation_flags (
                        org_id, opportunity_identity, status, reason, trigger_kind,
                        trigger_ref, entity_ids, flagged_run_id, flagged_by,
                        flagged_at, updated_at, cleared_run_id, cleared_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
                    ON CONFLICT (org_id, opportunity_identity) DO UPDATE SET
                        status = %s,
                        reason = EXCLUDED.reason,
                        trigger_kind = EXCLUDED.trigger_kind,
                        trigger_ref = EXCLUDED.trigger_ref,
                        entity_ids = EXCLUDED.entity_ids,
                        flagged_by = EXCLUDED.flagged_by,
                        updated_at = EXCLUDED.updated_at,
                        cleared_run_id = NULL,
                        cleared_at = NULL
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        org, identity, STATUS_PENDING, reason_text, _text(trigger_kind),
                        trigger_ref, payload, run_map.get(identity), _text(actor) or "system",
                        now, now,
                        STATUS_PENDING,
                    ),
                )
                row = cur.fetchone()
                if row is not None and bool(row[0]):
                    flagged += 1
                else:
                    refreshed += 1
            con.commit()
        except Exception:
            con.rollback()
            raise

    logger.info(
        "re-evaluation flags raised for org %s: %d new, %d refreshed (reason %s)",
        org, flagged, refreshed, reason_text,
    )
    return FlagReport(flagged=flagged, refreshed=refreshed, identities=tuple(wanted))


def clear_flags_for_run(
    org_id: str, run_id: str, identities: Iterable[str]
) -> List[str]:
    """Clear the pending flags for identities this run re-observed.

    This is the "on the next run" half of AC4, and the reason it is a call rather
    than a schedule: a flag is cleared by the run that actually re-measured the
    finding, and that run's id is written down. Only ``pending`` rows are touched,
    so a re-run never rewrites the clearing run of a flag already closed.

    Returns the identities actually cleared, so the caller can report the number
    rather than assume it.
    """
    org = _text(org_id)
    run = _text(run_id)
    wanted = sorted({_text(i) for i in (identities or ()) if _text(i)})
    if not org or not wanted:
        return []

    ensure_reevaluation_tables()
    now = _now()
    with closing(db.connect()) as con:
        try:
            cur = con.cursor()
            cur.execute(
                """
                UPDATE finding_reevaluation_flags
                   SET status = %s, cleared_run_id = %s, cleared_at = %s, updated_at = %s
                 WHERE org_id = %s
                   AND status = %s
                   AND opportunity_identity = ANY(%s)
             RETURNING opportunity_identity
                """,
                (STATUS_CLEARED, run or None, now, now, org, STATUS_PENDING, wanted),
            )
            cleared = [_text(r[0]) for r in (cur.fetchall() or [])]
            con.commit()
        except Exception:
            con.rollback()
            raise

    if cleared:
        logger.info(
            "run %s cleared %d re-evaluation flag(s) for org %s",
            run or "(unknown)", len(cleared), org,
        )
    return cleared


def get_flag(org_id: str, opportunity_identity: str) -> Optional[ReevaluationFlag]:
    """One finding's flag, or ``None`` when it has never been flagged."""
    org, identity = _text(org_id), _text(opportunity_identity)
    if not org or not identity:
        return None
    ensure_reevaluation_tables()
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            f"{_SELECT} WHERE org_id = %s AND opportunity_identity = %s",
            (org, identity),
        )
        row = cur.fetchone()
    return _row_to_flag(row) if row is not None else None


def list_flags(
    org_id: str, *, status: Optional[str] = STATUS_PENDING, limit: int = 200
) -> List[ReevaluationFlag]:
    """One org's flags, newest first. ``status=None`` returns every flag."""
    org = _text(org_id)
    if not org:
        return []
    ensure_reevaluation_tables()
    sql = f"{_SELECT} WHERE org_id = %s"
    params: List[Any] = [org]
    if status is not None:
        sql += " AND status = %s"
        params.append(_text(status))
    sql += " ORDER BY flagged_at DESC, opportunity_identity ASC LIMIT %s"
    params.append(max(1, min(int(limit or 200), 1000)))

    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    return [_row_to_flag(r) for r in rows]


def pending_identities(org_id: str) -> List[str]:
    """The identities awaiting re-evaluation — the read a run makes.

    Kept separate from :func:`list_flags` because a run wants the SET, not the
    rendered rows, and the set is what it intersects with what it just observed.
    """
    org = _text(org_id)
    if not org:
        return []
    ensure_reevaluation_tables()
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT opportunity_identity FROM finding_reevaluation_flags "
            "WHERE org_id = %s AND status = %s",
            (org, STATUS_PENDING),
        )
        rows = cur.fetchall() or []
    return sorted({_text(r[0]) for r in rows if _text(r[0])})


__all__ = [
    "STATUS_PENDING",
    "STATUS_CLEARED",
    "REASON_ENTITY_UNMERGED",
    "TRIGGER_ENTITY_UNMERGE",
    "ReevaluationFlag",
    "ReevaluationFlagError",
    "FlagReport",
    "ensure_reevaluation_tables",
    "flag_findings",
    "clear_flags_for_run",
    "get_flag",
    "list_flags",
    "pending_identities",
]
