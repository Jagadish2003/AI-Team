"""Deprecation grace behaviour — 2.0-C4 T4 (AT-845).

The rule this module owns (sub-task scope):

    During grace the pack runs normally; after grace it moves to disabled via
    2.0-C1's safe-disable path — history intact, never deleted.

Why grace exists at all
-----------------------
A grace period is a promise: *nothing changes yet*. AT-842 declares the dates,
AT-843 shows the notice, AT-844 offers the path. If the pack quietly started
behaving differently the moment the notice appeared, all three would be worthless —
the notice would be the change rather than a warning about one. So the first half of
this task is a **negative**: a pack in grace is untouched by anything here. There is
no code path in this module that can exclude, degrade, or re-version a pack whose
grace has not ended.

The second half is the transition, and it reuses 2.0-C1 rather than inventing a
parallel one. "Safe-disabled" already means something exact in this codebase
(``pack_state``): the pack stops executing in FUTURE runs, and every historical
finding, its evidence, and its run records stay intact, retrievable, and labelled as
produced by a now-disabled pack. That is precisely what an expired grace should do,
so this module *calls* ``pack_state.disable_pack`` instead of reimplementing it. The
never-delete guarantee (2.0-C1 T4 / AT-829) comes along for free — there is no delete
path here because there is none there.

Derived first, persisted second
-------------------------------
The exclusion is **derived** from the declared dates on every activation, exactly as
AT-842's phase is: a grace period ends because the date passed, not because something
noticed. The persistent ``disabled`` row is written *as well*, and the ordering
matters:

* the derived exclusion is what makes the guarantee unconditional — no job to
  schedule, nothing that can fail to run, no window in which an expired pack still
  executes;
* the persisted row is what makes it **visible and auditable** — it appears in the
  pack picker, in the transition history, and in the audit log, which is what the
  sub-task means by "moves to disabled via C1's safe-disable path".

So the write is best-effort: if it fails, the pack is still excluded and the failure
is logged. The reverse (persist-only) would be a real hazard — a missed write would
silently let a superseded pack keep running.

No background job, deliberately
-------------------------------
Nothing sweeps the estate on a timer. A job would add a window between expiry and
enforcement, a second code path that can disagree with the derived one, and an
operational dependency for a guarantee that costs nothing to evaluate inline. The
notice already tells a customer the pack no longer runs from the day it expires
(AT-843), and the first activation after that makes it so.

A customer cannot un-expire a pack
----------------------------------
Re-enabling an auto-disabled pack does not resurrect it: the next activation derives
the same expiry and disables it again. That is the AT-842 boundary holding —
deprecation is the registry shipper's dimension, pack state is the customer's, and
merging them would let a customer "undeprecate" a superseded pack. Re-enabling is not
refused (nothing here reaches into ``pack_state``'s API surface), it simply does not
change what runs.

Failure posture
---------------
Fail-soft, and in ONE direction: if the deprecation position cannot be read, NOTHING
is treated as expired. A read failure must never take a working pack offline — the
same direction AT-842 chose for a malformed declaration, and for the same reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Actor recorded on the state row, the transition history, and the audit entry.
#: A named non-human actor, so a reader can tell "the platform disabled this because
#: the announced grace ended" from "an owner turned it off" — two very different
#: facts that would otherwise be indistinguishable in the trail.
SYSTEM_ACTOR = "system:pack_deprecation"

#: The exclusion reason carried on the run record, run health, and telemetry.
#: Deliberately NOT ``pack_disabled``: an operator seeing a pack missing from a run
#: needs to know whether their organisation turned it off (they can re-enable it) or
#: whether it was retired by the vendor (they cannot, and should migrate).
EXCLUSION_REASON_GRACE_EXPIRED = "deprecation_grace_expired"


@dataclass(frozen=True)
class GraceExpiry:
    """One pack whose announced grace period has ended.

    ``disabled``/``already_disabled``/``persisted`` describe only what happened to the
    STORED state. None of them gates the exclusion — a pack in this list is excluded
    from the run whatever the write did, which is the point of deriving it.
    """

    pack_id: str
    grace_ends_on: str
    replacement_pack_id: str = ""
    summary: str = ""
    #: A NEW disable was written by this call (a real transition worth auditing).
    disabled: bool = False
    #: The pack was already disabled, so nothing was written (idempotent).
    already_disabled: bool = False
    #: The state write succeeded at all. False ⇒ derived exclusion only.
    persisted: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packId": self.pack_id,
            "graceEndsOn": self.grace_ends_on,
            "replacementPackId": self.replacement_pack_id,
            "summary": self.summary,
            "disabled": self.disabled,
            "alreadyDisabled": self.already_disabled,
            "persisted": self.persisted,
        }


def state_reason(expiry_or_deprecation: Any) -> str:
    """The note stored on the pack-state row and the transition history.

    Written for a human opening the pack's history months later, so it says WHY and
    points at the replacement rather than leaving them to correlate dates.
    """
    ends_on = getattr(expiry_or_deprecation, "grace_ends_on", "") or ""
    replacement = getattr(expiry_or_deprecation, "replacement_pack_id", "") or ""
    reason = (
        f"Deprecation grace period ended on {ends_on}"
        if ends_on
        else "Deprecation grace period ended"
    )
    if replacement:
        reason += f"; replaced by {replacement}"
    return reason[:1000]


def expired_grace_packs(
    pack_ids: Iterable[str], *, as_of: Optional[date] = None
) -> List[Any]:
    """The ``PackDeprecation`` verdicts, among ``pack_ids``, whose grace has ENDED.

    Fail-soft to an EMPTY list: if the deprecation position cannot be read, nothing is
    treated as expired. The degradation direction is the one that cannot hurt a
    customer — a pack that should have stopped runs one more time, rather than a
    working pack being taken offline by a read error.

    A pack in grace, a pack with open-ended grace, and a pack that is not deprecated
    are all absent here. ``is_grace_expired`` is False for every one of them, which is
    what makes "during grace the pack runs normally" true by construction rather than
    by a rule written somewhere else.
    """
    try:
        from discovery.packs.pack_deprecation import get_pack_deprecation
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not load pack deprecation metadata; treating no pack as expired",
            exc_info=True,
        )
        return []

    expired: List[Any] = []
    for pack_id in pack_ids:
        try:
            deprecation = get_pack_deprecation(pack_id, as_of=as_of)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not evaluate deprecation for pack %s; treating it as active",
                pack_id,
                exc_info=True,
            )
            continue
        if deprecation.grace_expired:
            expired.append(deprecation)
    return expired


def enforce_grace_expiry(
    *,
    org_id: str,
    pack_ids: Iterable[str],
    run_id: Optional[str] = None,
    as_of: Optional[date] = None,
) -> List[GraceExpiry]:
    """Move every grace-expired pack in ``pack_ids`` to safe-disabled.

    Returns one :class:`GraceExpiry` per expired pack — the set the caller must
    exclude from the run. An empty list is the overwhelmingly common case and means
    every selected pack is either not deprecated or still inside its grace, so the run
    proceeds exactly as it did before the notice appeared.

    The state write goes through 2.0-C1's :func:`~app.pack_state.disable_pack`, so it
    is idempotent (an already-disabled pack writes no second history row) and it
    cannot delete anything. A write failure is logged and the pack is STILL returned:
    the exclusion is derived, not looked up.
    """
    org = str(org_id or "").strip()
    expiries: List[GraceExpiry] = []

    for deprecation in expired_grace_packs(pack_ids, as_of=as_of):
        expiry = _safe_disable(org, deprecation, run_id=run_id)
        expiries.append(expiry)

    if expiries:
        logger.info(
            "Deprecation grace expired for org=%s run=%s: %s",
            org, run_id, [item.pack_id for item in expiries],
        )
    return expiries


def _safe_disable(
    org_id: str, deprecation: Any, *, run_id: Optional[str]
) -> GraceExpiry:
    """Persist the safe-disable for one expired pack. Never raises."""
    pack_id = str(getattr(deprecation, "pack_id", "") or "")
    base = GraceExpiry(
        pack_id=pack_id,
        grace_ends_on=str(getattr(deprecation, "grace_ends_on", "") or ""),
        replacement_pack_id=str(
            getattr(deprecation, "replacement_pack_id", "") or ""
        ),
        summary=str(getattr(deprecation, "summary", "") or ""),
    )
    if not org_id or not pack_id:
        return GraceExpiry(**{**base.__dict__, "persisted": False})

    try:
        from .pack_state import disable_pack

        outcome = disable_pack(
            org_id,
            pack_id,
            actor_id=SYSTEM_ACTOR,
            reason=state_reason(base),
        )
    except Exception:  # noqa: BLE001
        # The pack is excluded regardless — this only costs the customer the visible
        # row and the history entry, and a persistently failing write shows up here.
        logger.warning(
            "Could not safe-disable grace-expired pack %s for org=%s; it is still "
            "excluded from this run",
            pack_id,
            org_id,
            exc_info=True,
        )
        return GraceExpiry(**{**base.__dict__, "persisted": False})

    expiry = GraceExpiry(
        **{
            **base.__dict__,
            "disabled": bool(outcome.changed),
            "already_disabled": not outcome.changed,
            "persisted": True,
        }
    )
    # Only a REAL transition is an audit event. An expired pack is re-evaluated on
    # every activation, so auditing the no-op would bury the one entry that matters
    # under one row per run forever.
    if expiry.disabled:
        _audit_grace_disable(org_id, expiry, run_id=run_id, revision=outcome.revision)
        _record_grace_disable(org_id, expiry, run_id=run_id)
    return expiry


def _audit_grace_disable(
    org_id: str, expiry: GraceExpiry, *, run_id: Optional[str], revision: int
) -> None:
    """Place the automatic safe-disable in the org-wide audit stream.

    A dedicated event rather than ``pack_state_changed``: "the platform retired this
    pack on the announced date" and "an owner turned this pack off" are different
    facts with different remedies, and a reviewer must be able to tell them apart
    without inferring it from the actor string.
    """
    from .middleware.audit import PACK_DEPRECATION_DISABLED, log_event

    log_event(
        PACK_DEPRECATION_DISABLED,
        org_id=org_id,
        user_id=SYSTEM_ACTOR,
        run_id=run_id,
        pack_id=expiry.pack_id,
        grace_ends_on=expiry.grace_ends_on,
        replacement_pack_id=expiry.replacement_pack_id,
        revision=revision,
    )


def _record_grace_disable(
    org_id: str, expiry: GraceExpiry, *, run_id: Optional[str]
) -> None:
    """Mirror the transition into telemetry. Never fails the activation."""
    from .telemetry import record_event

    try:
        record_event(
            "pack.deprecation_disabled",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_id": expiry.pack_id,
                "grace_ends_on": expiry.grace_ends_on,
                "replacement_pack_id": expiry.replacement_pack_id,
                "actor_id": SYSTEM_ACTOR,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.deprecation_disabled telemetry failed (non-blocking)", exc_info=True
        )


__all__ = [
    "EXCLUSION_REASON_GRACE_EXPIRED",
    "GraceExpiry",
    "SYSTEM_ACTOR",
    "enforce_grace_expiry",
    "expired_grace_packs",
    "state_reason",
]
