"""Deprecation lifecycle audit — 2.0-C4 T5 (AT-846).

The rule this module owns (sub-task scope):

    Deprecation, migration, and post-grace disable are audit events.

What was already true, and what was not
---------------------------------------
Two of the three transitions already reached the audit log when this task started:

* **migration** — ``pack_migration_applied`` / ``pack_migration_reverted``
  (AT-844, emitted at the API edge);
* **post-grace disable** — ``pack_deprecation_disabled``
  (AT-845, emitted by ``pack_grace`` when a grace period ends).

**Deprecation itself did not.** T1 declares a deprecation in ``PACK_REGISTRY`` — a
property of the shipped registry, changed by a code deploy — and T2 renders the notice
on three surfaces. Neither leaves a record that a particular ORGANISATION was ever
subject to it. So an auditor could see that a pack was migrated or retired, with no
recorded moment at which the customer came under notice in the first place. Closing
that is the substance of this task; the rest is making the three readable as one trail.

What counts as "the deprecation transition" for an org
-------------------------------------------------------
A declaration is global; an audit entry is org-scoped. The auditable, org-level fact is
the first time a deprecation actually BEARS on this organisation — which is when the
platform evaluates its pack selection for a run. That is why the announcement is
emitted from the activation path (``pack_activation``'s stage 0) and not from a read:

* it is a fact with a **consequence** — the org configured or ran discovery with a
  superseded pack in the selection, and was on notice at that moment;
* it happens on the ONE resolution both API edges and ``discovery/runner.py`` share,
  so a CLI caller cannot produce a run whose deprecation exposure went unrecorded;
* rendering the pack picker is not a transition, and emitting audit rows from a GET
  would make the trail a record of page views rather than of decisions.

Announced once, and again only when the terms CHANGE
----------------------------------------------------
An expired or superseded pack is re-evaluated on every single activation, so a naive
emit would bury the trail under one row per run forever. The announcement is therefore
keyed on ``(org_id, pack_id, declaration fingerprint)`` and written once.

The fingerprint is over the declared TERMS — reason, dates, replacement, version scope,
status. That is deliberate rather than incidental: if the vendor moves the grace end
date, changes the replacement, or restates the reason, the customer is under
materially different notice and must be told again. Keying on the pack id alone would
silently swallow exactly the changes that matter most.

Where the ledger lives
----------------------
``kv``, under ``pack_deprecation_announcements:{org_id}`` — the same choice, for the
same reasons, as AT-844's migration ledger: ``kv`` is in
``history_retention.PROTECTED_TABLES`` so the record inherits the 2.0-C1 T4
never-delete guarantee, and it needs no schema migration (and so cannot collide with
the concurrently-developed 2.0-C3 numbering).

Note what the ledger is and is not. It is a *de-duplication* record — "have we already
told this org these terms?" The audit log remains the trail; losing the ledger would
cause a re-announcement, never a lost entry.

Failure posture
---------------
Nothing here can fail an activation. An announcement is a record, and a run must not be
refused because a record could not be written — the same posture as
``log_event`` itself, which is documented never to raise. Every path is fail-soft, and
a failure to persist the ledger deliberately errs toward re-announcing rather than
toward silence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── The three transitions (parent-story AC4) ─────────────────────────────────
#
# Written down ONCE, here, because "all three transitions are audit events" is a
# claim that has to be checkable. A structural test asserts this mapping covers every
# deprecation-lifecycle audit event the codebase emits, so adding a fourth transition
# without an audit event — or an event nobody can find — fails the build.

#: The vendor put this pack on notice, and this org came under it.
TRANSITION_DEPRECATED = "deprecated"
#: This org's configuration was moved onto the replacement (or moved back).
TRANSITION_MIGRATED = "migrated"
#: The grace period ended and the pack was safe-disabled.
TRANSITION_RETIRED = "retired"

#: Audit event emitted when an org first comes under a pack's deprecation terms.
PACK_DEPRECATION_ANNOUNCED = "pack_deprecation_announced"

#: ``transition -> the audit event types that record it``.
DEPRECATION_AUDIT_EVENTS: Dict[str, Tuple[str, ...]] = {
    TRANSITION_DEPRECATED: (PACK_DEPRECATION_ANNOUNCED,),
    # Two events, one transition: applying and reverting are both movements of the
    # org's configuration, and a trail that showed only one would misrepresent the
    # end state.
    TRANSITION_MIGRATED: ("pack_migration_applied", "pack_migration_reverted"),
    TRANSITION_RETIRED: ("pack_deprecation_disabled",),
}

#: Flat set of every audit event type in the deprecation lifecycle.
DEPRECATION_AUDIT_EVENT_TYPES: Tuple[str, ...] = tuple(
    event
    for events in DEPRECATION_AUDIT_EVENTS.values()
    for event in events
)

#: ``audit event type -> transition``, so a trail row can name which of the three it is.
TRANSITION_FOR_EVENT: Dict[str, str] = {
    event: transition
    for transition, events in DEPRECATION_AUDIT_EVENTS.items()
    for event in events
}

#: kv namespace for the announced-terms ledger.
ANNOUNCEMENT_LEDGER_PREFIX = "pack_deprecation_announcements"


@dataclass(frozen=True)
class DeprecationAnnouncement:
    """One org being placed under a pack's deprecation terms."""

    pack_id: str
    fingerprint: str
    phase: str
    grace_ends_on: str = ""
    replacement_pack_id: str = ""
    summary: str = ""
    #: False when these exact terms had already been announced to this org.
    announced: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packId": self.pack_id,
            "fingerprint": self.fingerprint,
            "phase": self.phase,
            "graceEndsOn": self.grace_ends_on,
            "replacementPackId": self.replacement_pack_id,
            "summary": self.summary,
            "announced": self.announced,
        }


# ── Declaration fingerprint ───────────────────────────────────────────────────


def declaration_fingerprint(deprecation: Any) -> str:
    """Digest of the declared TERMS a customer is being put under.

    Covers the reason, both dates, the replacement, the version scope, and the
    declared status — everything that changes what the customer is being told. It
    deliberately excludes the evaluation date and the derived phase: a pack sliding
    from ``grace`` into ``grace_expired`` is the announced terms coming true, not new
    terms, and AT-845's retirement event records that moment already.
    """
    payload = {
        "packId": getattr(deprecation, "pack_id", ""),
        "version": getattr(deprecation, "version", ""),
        "status": getattr(deprecation, "declared_status", ""),
        "reason": getattr(deprecation, "reason", ""),
        "deprecatedOn": getattr(deprecation, "deprecated_on", ""),
        "graceEndsOn": getattr(deprecation, "grace_ends_on", ""),
        "gracePeriodDays": getattr(deprecation, "grace_period_days", None),
        "appliesToVersions": list(
            getattr(deprecation, "applies_to_versions", []) or []
        ),
        "replacementPackId": getattr(deprecation, "replacement_pack_id", ""),
        "replacementMinVersion": getattr(
            deprecation, "replacement_min_version", ""
        ),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# ── Announcement ledger ───────────────────────────────────────────────────────


class AnnouncementLedger:
    """``{pack_id: fingerprint}`` of the terms already announced to an org.

    A de-duplication record, not the audit trail. Losing it causes a
    re-announcement — never a lost audit entry.
    """

    def read(self, org_id: str) -> Dict[str, str]:
        raise NotImplementedError

    def write(self, org_id: str, announced: Dict[str, str]) -> None:
        raise NotImplementedError


class InMemoryAnnouncementLedger(AnnouncementLedger):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, str]] = {}
        self._lock = threading.RLock()

    def read(self, org_id: str) -> Dict[str, str]:
        with self._lock:
            return dict(self._rows.get(org_id, {}))

    def write(self, org_id: str, announced: Dict[str, str]) -> None:
        with self._lock:
            self._rows[org_id] = dict(announced)


class KvAnnouncementLedger(AnnouncementLedger):
    """Production ledger, on the protected ``kv`` table."""

    def _key(self, org_id: str) -> str:
        return f"{ANNOUNCEMENT_LEDGER_PREFIX}:{org_id}"

    def read(self, org_id: str) -> Dict[str, str]:
        from . import db

        stored = db.kv_get(self._key(org_id))
        if not isinstance(stored, dict):
            return {}
        return {str(k): str(v) for k, v in stored.items() if k and v}

    def write(self, org_id: str, announced: Dict[str, str]) -> None:
        from . import db

        db.kv_set(self._key(org_id), dict(announced))


_LEDGER: Optional[AnnouncementLedger] = None


def get_announcement_ledger() -> AnnouncementLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = KvAnnouncementLedger()
    return _LEDGER


def set_announcement_ledger(ledger: Optional[AnnouncementLedger]) -> None:
    """Test/offline injection seam; ``None`` restores the production ledger."""
    global _LEDGER
    _LEDGER = ledger


# ── Emission ──────────────────────────────────────────────────────────────────


def announce_deprecations(
    *,
    org_id: str,
    pack_ids: Iterable[str],
    run_id: Optional[str] = None,
    as_of: Optional[date] = None,
) -> List[DeprecationAnnouncement]:
    """Record that this org has come under each selected pack's deprecation terms.

    Returns one entry per DEPRECATED pack in the selection — the common case is an
    empty list, because no pack ships a deprecation. ``announced`` is True only for
    the entries that actually wrote an audit event; a repeat activation under
    unchanged terms returns them with ``announced=False`` and writes nothing.

    Never raises. An activation must not fail because a record could not be written.
    """
    org = str(org_id or "").strip()
    if not org:
        return []

    try:
        deprecations = _deprecated_in(pack_ids, as_of=as_of)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not evaluate deprecations for org=%s; no announcement recorded",
            org,
            exc_info=True,
        )
        return []
    if not deprecations:
        return []

    try:
        ledger = get_announcement_ledger()
        already = ledger.read(org)
    except Exception:  # noqa: BLE001
        # Err toward re-announcing rather than toward silence: a duplicate entry is
        # noise, a missing one is a hole in the trail.
        logger.warning(
            "Could not read the deprecation announcement ledger for org=%s; "
            "announcing again rather than staying silent",
            org,
            exc_info=True,
        )
        already = {}

    announcements: List[DeprecationAnnouncement] = []
    updated = dict(already)
    for deprecation in deprecations:
        fingerprint = declaration_fingerprint(deprecation)
        pack_id = str(deprecation.pack_id)
        is_new = already.get(pack_id) != fingerprint
        announcement = DeprecationAnnouncement(
            pack_id=pack_id,
            fingerprint=fingerprint,
            phase=str(deprecation.phase),
            grace_ends_on=str(deprecation.grace_ends_on or ""),
            replacement_pack_id=str(deprecation.replacement_pack_id or ""),
            summary=str(deprecation.summary or ""),
            announced=is_new,
        )
        if is_new:
            _audit_announcement(org, deprecation, announcement, run_id=run_id)
            _record_announcement(org, announcement, run_id=run_id)
            updated[pack_id] = fingerprint
        announcements.append(announcement)

    if updated != already:
        try:
            ledger.write(org, updated)
        except Exception:  # noqa: BLE001
            # The audit entry is already written, which is the part that matters.
            # A failed ledger write only risks a duplicate entry next time.
            logger.warning(
                "Could not persist the deprecation announcement ledger for org=%s",
                org,
                exc_info=True,
            )
    return announcements


def _deprecated_in(pack_ids: Iterable[str], *, as_of: Optional[date]) -> List[Any]:
    from discovery.packs.pack_deprecation import get_pack_deprecation

    out: List[Any] = []
    seen: set = set()
    for pack_id in pack_ids:
        deprecation = get_pack_deprecation(pack_id, as_of=as_of)
        if deprecation.deprecated and deprecation.pack_id not in seen:
            seen.add(deprecation.pack_id)
            out.append(deprecation)
    return out


def _audit_announcement(
    org_id: str,
    deprecation: Any,
    announcement: DeprecationAnnouncement,
    *,
    run_id: Optional[str],
) -> None:
    """Write the first of the three transitions to the org-wide audit stream."""
    from .middleware.audit import log_event

    log_event(
        PACK_DEPRECATION_ANNOUNCED,
        org_id=org_id,
        run_id=run_id,
        pack_id=announcement.pack_id,
        pack_version=str(getattr(deprecation, "version", "") or ""),
        phase=announcement.phase,
        reason=str(getattr(deprecation, "reason", "") or ""),
        deprecated_on=str(getattr(deprecation, "deprecated_on", "") or ""),
        grace_ends_on=announcement.grace_ends_on,
        replacement_pack_id=announcement.replacement_pack_id,
        fingerprint=announcement.fingerprint,
    )


def _record_announcement(
    org_id: str, announcement: DeprecationAnnouncement, *, run_id: Optional[str]
) -> None:
    """Mirror the announcement into telemetry. Never fails the activation."""
    from .telemetry import record_event

    try:
        record_event(
            "pack.deprecation_announced",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_id": announcement.pack_id,
                "phase": announcement.phase,
                "grace_ends_on": announcement.grace_ends_on,
                "replacement_pack_id": announcement.replacement_pack_id,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.deprecation_announced telemetry failed (non-blocking)",
            exc_info=True,
        )


# ── The consolidated trail ────────────────────────────────────────────────────


def deprecation_audit_trail(
    org_id: str, *, pack_id: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    """The three transitions for one org, newest first.

    The events are already in ``audit_log`` and already reachable through
    ``GET /api/audit-log``. This exists because reachable is not the same as usable:
    that endpoint is an unfiltered firehose across every event type in the product,
    and answering "what has happened to this pack's lifecycle" by scrolling it is not
    an audit, it is an archaeology exercise.

    Each row is stamped with which of the three transitions it is, so a reader does
    not have to know that ``pack_migration_reverted`` and ``pack_migration_applied``
    are two halves of one thing.

    Reads are org-scoped in SQL. Raises on a read failure rather than returning an
    empty list — an audit surface that silently reports "nothing happened" when it
    cannot read is worse than one that reports an error.
    """
    from . import db

    org = str(org_id or "").strip()
    if not org:
        return []

    sql = """
        SELECT id, event_type, user_id, run_id, payload, timestamp
        FROM audit_log
        WHERE org_id = %s AND event_type = ANY(%s)
        ORDER BY timestamp DESC
        LIMIT %s
    """
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(sql, (org, list(DEPRECATION_AUDIT_EVENT_TYPES), int(limit)))
        rows = cur.fetchall()
    finally:
        con.close()

    trail: List[Dict[str, Any]] = []
    for row in rows:
        payload = row[4]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        entry_pack_id = str((payload or {}).get("pack_id") or "")
        if pack_id and entry_pack_id != pack_id:
            continue
        trail.append(
            {
                "id": str(row[0]),
                "eventType": str(row[1]),
                "transition": TRANSITION_FOR_EVENT.get(str(row[1]), ""),
                "actorId": row[2],
                "runId": row[3],
                "packId": entry_pack_id,
                "payload": payload,
                "at": _iso(row[5]),
            }
        )
    return trail


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


__all__ = [
    "ANNOUNCEMENT_LEDGER_PREFIX",
    "AnnouncementLedger",
    "DEPRECATION_AUDIT_EVENTS",
    "DEPRECATION_AUDIT_EVENT_TYPES",
    "DeprecationAnnouncement",
    "InMemoryAnnouncementLedger",
    "KvAnnouncementLedger",
    "PACK_DEPRECATION_ANNOUNCED",
    "TRANSITION_DEPRECATED",
    "TRANSITION_FOR_EVENT",
    "TRANSITION_MIGRATED",
    "TRANSITION_RETIRED",
    "announce_deprecations",
    "declaration_fingerprint",
    "deprecation_audit_trail",
    "get_announcement_ledger",
    "set_announcement_ledger",
]
