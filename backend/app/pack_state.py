"""Pack lifecycle state — 2.0-C1 T2 (AT-827) safe disable state machine.

The rule this module owns (parent story AC2):

    active → disabled stops the pack executing in future runs while ALL historical
    findings, evidence, and run records remain intact and viewable, clearly marked
    as produced by a now-disabled pack. Re-enable is supported.

The state machine
-----------------
Two states and two transitions, per (org, pack):

    active  --disable--> disabled
    disabled --enable--> active

``active`` is the DEFAULT and is represented by the ABSENCE of a row. Provisioning
the tables therefore changes no behaviour until a customer disables something —
there is no seed step and no backfill. Both transitions are idempotent: disabling
an already-disabled pack returns ``changed=False`` and writes no history row
(mirroring ``runbook_match_decisions.decide``).

Nothing is ever deleted
-----------------------
There is **no delete path in this module** — not for state, not for history, and
emphatically not for findings, evidence, or run records. Disabling writes a state
row; re-enabling writes a new state and a new history row rather than removing the
disable. That is what makes the transition history a real audit trail, and it is
why disable cannot destroy run history (2.0-C1 AC4; AT-829 tests it exhaustively).

Disable EXCLUDES, it does not refuse
------------------------------------
A disabled pack is dropped from a run's pack selection — it is not an activation
error. This is deliberately different from AT-826's compatibility gate, and the
distinction is principled:

* **incompatible** = "this pack CANNOT work on this platform" — a configuration
  error, so the activation edge REFUSES with a 409 naming the unmet requirement;
* **disabled** = "this pack is intentionally turned off" — a deliberate, ongoing
  customer state, so the run proceeds with the packs that remain.

Refusing every run after a disable would make disable unusable: the customer would
also have to edit every template and industry default that references the pack.
Excluding matches how this codebase treats a source that is not connected —
degrade, don't crash.

The exclusion is **loud, never silent** (the same discipline as MSP-B7's noise
floors and run budgets): every excluded pack is recorded on the run record, in
run health, and as telemetry. If the exclusion would leave NOTHING to run, that IS
an error — a run with zero packs is meaningless.

Read posture
------------
Reads are **fail-soft** (:func:`disabled_pack_ids_safe`): if the state store cannot
be read, every pack reads as active. This is deliberate for the display path —
"historical findings remain retrievable and viewable" outranks the label, so a
state-store hiccup must never hide a finding. Writes are NOT fail-soft: a disable
that did not persist must never look like it succeeded.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from app import db

logger = logging.getLogger(__name__)

# ── States and transitions ────────────────────────────────────────────────────

#: The default state. Represented by the ABSENCE of a row — never written on read.
STATE_ACTIVE = "active"
#: Excluded from future runs; all historical output stays intact and labelled.
STATE_DISABLED = "disabled"

PACK_STATES = frozenset({STATE_ACTIVE, STATE_DISABLED})

TRANSITION_DISABLE = "disable"
TRANSITION_ENABLE = "enable"
#: 2.0-C1 T3 (AT-828): version transitions. ``rollback`` pins the pack to a prior
#: archived version; ``restore`` clears the pin so the pack runs its current version
#: again. They share this table's ``revision`` counter and history trail with the
#: enable/disable transitions — one audit trail per (org, pack) answers "what has
#: this org done to this pack".
TRANSITION_ROLLBACK = "rollback"
TRANSITION_RESTORE = "restore"

#: The only legal state transitions. A target state maps to exactly one name.
_TRANSITION_FOR_TARGET = {
    STATE_DISABLED: TRANSITION_DISABLE,
    STATE_ACTIVE: TRANSITION_ENABLE,
}

#: Label carried by a finding whose producing pack is disabled TODAY. Present so a
#: reader can never mistake historical output for something a live pack produced.
DISABLED_PACK_LABEL = "Produced by a now-disabled pack"


class PackNotFound(LookupError):
    """The pack id is not in the registry, so it has no state to change.

    Deliberately strict — unlike ``get_pack()``, which resolves an unknown id to
    the default pack. Silently disabling ``service_cloud`` because an operator
    typo'd a pack id would be a serious foot-gun.
    """


class PackStateError(ValueError):
    """The requested state is not a legal pack state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _known_pack_ids() -> Set[str]:
    from discovery.packs.pack_config import PACK_REGISTRY

    return set(PACK_REGISTRY)


def _validated_pack_id(pack_id: Any) -> str:
    pack = _required(pack_id, "pack_id")
    if pack not in _known_pack_ids():
        raise PackNotFound(f"unknown pack '{pack}'")
    return pack


def _validated_state(state: Any) -> str:
    normalized = str(state or "").strip().lower()
    if normalized not in PACK_STATES:
        raise PackStateError(
            f"state must be one of {sorted(PACK_STATES)}, got {state!r}"
        )
    return normalized


def _validated_pin(pack_id: str, version: Any) -> Optional[str]:
    """Validate a rollback target, returning the pin to store.

    ``None``/empty means "clear the pin" (restore to the current version).

    A version equal to the pack's CURRENT ``packVersion`` also normalises to
    ``None``: pinning to the current version and having no pin are the same
    position, and storing it would leave a stale pin behind after the next bump —
    the pack would silently keep running the old version.

    Any other version must have an ARCHIVED artifact
    (``pack_config.get_rollbackable_versions``), else :class:`PackVersionUnavailable`
    is raised naming the versions that are available. This is the honesty boundary:
    the platform will not stamp a run with a version whose behaviour it cannot serve.
    """
    from discovery.packs.pack_config import (
        PackVersionUnavailable,
        get_pack_version,
        get_rollbackable_versions,
    )

    wanted = str(version or "").strip()
    if not wanted:
        return None
    if wanted == get_pack_version(pack_id):
        return None

    if wanted not in get_rollbackable_versions(pack_id):
        raise PackVersionUnavailable(
            pack_id=pack_id,
            version=wanted,
            available=get_rollbackable_versions(pack_id),
            current=get_pack_version(pack_id),
        )
    return wanted


@dataclass(frozen=True)
class PackStateOutcome:
    """The result of a lifecycle transition attempt (state OR version).

    ``previous_version`` / ``current_version`` are the 2.0-C1 T3 pin values —
    ``None`` means "not pinned, runs the current registry version". They are carried
    on every outcome (not just rollbacks) so a caller always sees the full lifecycle
    position after any transition.
    """

    org_id: str
    pack_id: str
    transition: str
    previous_state: str
    current_state: str
    revision: int
    changed: bool
    reason: Optional[str]
    changed_at: str
    actor_id: str
    previous_version: Optional[str] = None
    current_version: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "orgId": self.org_id,
            "packId": self.pack_id,
            "transition": self.transition,
            "previousState": self.previous_state,
            "state": self.current_state,
            "revision": self.revision,
            "changed": self.changed,
            "reason": self.reason,
            "changedAt": self.changed_at,
            "actorId": self.actor_id,
            "previousPinnedVersion": self.previous_version,
            "pinnedVersion": self.current_version,
        }


# ── Store contract ────────────────────────────────────────────────────────────


class PackStateStore:
    """Read/write contract for pack lifecycle state. No delete operation exists."""

    def get_state(self, org_id: str, pack_id: str) -> str:
        raise NotImplementedError

    def all_states(self, org_id: str) -> Dict[str, Dict[str, Any]]:
        """Every EXPLICIT state row for this org, keyed by pack id.

        Packs with no row are active and are deliberately absent — callers treat a
        missing key as active rather than relying on this to enumerate the registry.
        """
        raise NotImplementedError

    def set_state(
        self,
        org_id: str,
        pack_id: str,
        state: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        raise NotImplementedError

    def history(self, org_id: str, pack_id: str) -> List[Dict[str, Any]]:
        """Append-only transition history, newest first (repo audit convention)."""
        raise NotImplementedError

    def set_pinned_version(
        self,
        org_id: str,
        pack_id: str,
        version: Optional[str],
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        """Pin the pack to ``version``, or clear the pin when ``version`` is None.

        2.0-C1 T3 (AT-828). Writes a ``rollback``/``restore`` row to the SAME
        append-only history as the state transitions.
        """
        raise NotImplementedError


class InMemoryPackStateStore(PackStateStore):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._rows: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def get_state(self, org_id: str, pack_id: str) -> str:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        with self._lock:
            row = self._rows.get((org, pack))
        return str(row["state"]) if row else STATE_ACTIVE

    def all_states(self, org_id: str) -> Dict[str, Dict[str, Any]]:
        org = _required(org_id, "org_id")
        with self._lock:
            return {
                pack: dict(row)
                for (row_org, pack), row in self._rows.items()
                if row_org == org
            }

    def set_state(
        self,
        org_id: str,
        pack_id: str,
        state: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        org = _required(org_id, "org_id")
        pack = _validated_pack_id(pack_id)
        actor = _required(actor_id, "actor_id")
        target = _validated_state(state)
        now = _now()

        with self._lock:
            row = self._rows.get((org, pack))
            previous_state = str(row["state"]) if row else STATE_ACTIVE
            revision = int(row["revision"]) if row else 0

            pinned = (row or {}).get("pinned_version")

            if previous_state == target:
                # Idempotent — no history row for a no-op transition.
                return PackStateOutcome(
                    org, pack, _TRANSITION_FOR_TARGET[target], previous_state,
                    previous_state, revision, False, (row or {}).get("reason"),
                    (row or {}).get("updated_at", now), actor,
                    previous_version=pinned, current_version=pinned,
                )

            revision += 1
            self._rows[(org, pack)] = {
                "org_id": org,
                "pack_id": pack,
                "state": target,
                "revision": revision,
                "reason": reason,
                "updated_by": actor,
                "created_at": (row or {}).get("created_at", now),
                "updated_at": now,
                # A state change never touches the version pin — the two lifecycle
                # dimensions are independent (disabling a rolled-back pack must not
                # silently un-pin it).
                "pinned_version": pinned,
            }
            self._history.append(
                {
                    "id": f"psh_{uuid4().hex}",
                    "org_id": org,
                    "pack_id": pack,
                    "revision": revision,
                    "transition": _TRANSITION_FOR_TARGET[target],
                    "previous_state": previous_state,
                    "resulting_state": target,
                    "reason": reason,
                    "actor_id": actor,
                    "changed_at": now,
                    "previous_version": pinned,
                    "resulting_version": pinned,
                }
            )
            return PackStateOutcome(
                org, pack, _TRANSITION_FOR_TARGET[target], previous_state, target,
                revision, True, reason, now, actor,
                previous_version=pinned, current_version=pinned,
            )

    def set_pinned_version(
        self,
        org_id: str,
        pack_id: str,
        version: Optional[str],
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        org = _required(org_id, "org_id")
        pack = _validated_pack_id(pack_id)
        actor = _required(actor_id, "actor_id")
        target = _validated_pin(pack, version)
        now = _now()
        transition = TRANSITION_RESTORE if target is None else TRANSITION_ROLLBACK

        with self._lock:
            row = self._rows.get((org, pack))
            previous_version = (row or {}).get("pinned_version")
            state = str(row["state"]) if row else STATE_ACTIVE
            revision = int(row["revision"]) if row else 0

            if previous_version == target:
                # Idempotent — no history row for a no-op pin.
                return PackStateOutcome(
                    org, pack, transition, state, state, revision, False,
                    (row or {}).get("reason"), (row or {}).get("updated_at", now),
                    actor, previous_version=previous_version, current_version=target,
                )

            revision += 1
            self._rows[(org, pack)] = {
                "org_id": org,
                "pack_id": pack,
                # A version pin never changes the enable/disable state.
                "state": state,
                "revision": revision,
                "reason": reason,
                "updated_by": actor,
                "created_at": (row or {}).get("created_at", now),
                "updated_at": now,
                "pinned_version": target,
            }
            self._history.append(
                {
                    "id": f"psh_{uuid4().hex}",
                    "org_id": org,
                    "pack_id": pack,
                    "revision": revision,
                    "transition": transition,
                    "previous_state": state,
                    "resulting_state": state,
                    "reason": reason,
                    "actor_id": actor,
                    "changed_at": now,
                    "previous_version": previous_version,
                    "resulting_version": target,
                }
            )
            return PackStateOutcome(
                org, pack, transition, state, state, revision, True, reason, now,
                actor, previous_version=previous_version, current_version=target,
            )

    def history(self, org_id: str, pack_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        with self._lock:
            rows = [
                dict(event)
                for event in self._history
                if event["org_id"] == org and event["pack_id"] == pack
            ]
        return sorted(rows, key=lambda event: int(event["revision"]), reverse=True)


class PostgresPackStateStore(PackStateStore):
    """Production store. Migration 0031 / provision.sql provision its two tables."""

    def get_state(self, org_id: str, pack_id: str) -> str:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT state FROM pack_states WHERE org_id = %s AND pack_id = %s",
                (org, pack),
            )
            row = cur.fetchone()
        finally:
            con.close()
        return str(row[0]) if row else STATE_ACTIVE

    def all_states(self, org_id: str) -> Dict[str, Dict[str, Any]]:
        org = _required(org_id, "org_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT pack_id, state, revision, reason, updated_by, updated_at, "
                "       pinned_version "
                "FROM pack_states WHERE org_id = %s",
                (org,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return {
            str(row[0]): {
                "org_id": org,
                "pack_id": str(row[0]),
                "state": str(row[1]),
                "revision": int(row[2]),
                "reason": row[3],
                "updated_by": row[4],
                "updated_at": _iso(row[5]),
                "pinned_version": row[6],
            }
            for row in rows
        }

    def set_pinned_version(
        self,
        org_id: str,
        pack_id: str,
        version: Optional[str],
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        org = _required(org_id, "org_id")
        pack = _validated_pack_id(pack_id)
        actor = _required(actor_id, "actor_id")
        target = _validated_pin(pack, version)
        now = _now()
        transition = TRANSITION_RESTORE if target is None else TRANSITION_ROLLBACK

        con = db.connect()
        try:
            cur = con.cursor()
            # Lock the row for the read-modify-write so two concurrent transitions
            # cannot both claim the same revision number.
            cur.execute(
                "SELECT state, revision, reason, created_at, updated_at, "
                "       pinned_version "
                "FROM pack_states WHERE org_id = %s AND pack_id = %s FOR UPDATE",
                (org, pack),
            )
            row = cur.fetchone()
            state = str(row[0]) if row else STATE_ACTIVE
            revision = int(row[1]) if row else 0
            previous_version = row[5] if row else None

            if previous_version == target:
                con.commit()
                return PackStateOutcome(
                    org, pack, transition, state, state, revision, False,
                    (row[2] if row else None),
                    _iso(row[4]) if row else now, actor,
                    previous_version=previous_version, current_version=target,
                )

            revision += 1
            cur.execute(
                """
                INSERT INTO pack_states
                    (org_id, pack_id, state, revision, reason, updated_by,
                     created_at, updated_at, pinned_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, pack_id) DO UPDATE SET
                    revision       = EXCLUDED.revision,
                    reason         = EXCLUDED.reason,
                    updated_by     = EXCLUDED.updated_by,
                    updated_at     = EXCLUDED.updated_at,
                    pinned_version = EXCLUDED.pinned_version
                """,
                (org, pack, state, revision, reason, actor, now, now, target),
            )
            cur.execute(
                """
                INSERT INTO pack_state_history
                    (id, org_id, pack_id, revision, transition, previous_state,
                     resulting_state, reason, actor_id, changed_at,
                     previous_version, resulting_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"psh_{uuid4().hex}", org, pack, revision, transition,
                    state, state, reason, actor, now, previous_version, target,
                ),
            )
            con.commit()
        finally:
            con.close()

        return PackStateOutcome(
            org, pack, transition, state, state, revision, True, reason, now, actor,
            previous_version=previous_version, current_version=target,
        )

    def set_state(
        self,
        org_id: str,
        pack_id: str,
        state: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PackStateOutcome:
        org = _required(org_id, "org_id")
        pack = _validated_pack_id(pack_id)
        actor = _required(actor_id, "actor_id")
        target = _validated_state(state)
        now = _now()
        transition = _TRANSITION_FOR_TARGET[target]

        con = db.connect()
        try:
            cur = con.cursor()
            # Lock the row for the read-modify-write so two concurrent transitions
            # cannot both claim the same revision number.
            cur.execute(
                "SELECT state, revision, reason, created_at, updated_at, "
                "       pinned_version "
                "FROM pack_states WHERE org_id = %s AND pack_id = %s FOR UPDATE",
                (org, pack),
            )
            row = cur.fetchone()
            previous_state = str(row[0]) if row else STATE_ACTIVE
            revision = int(row[1]) if row else 0
            # A state change never touches the version pin — the two lifecycle
            # dimensions are independent, so it is carried through unchanged.
            pinned = row[5] if row else None

            if previous_state == target:
                con.commit()
                return PackStateOutcome(
                    org, pack, transition, previous_state, previous_state, revision,
                    False, (row[2] if row else None),
                    _iso(row[4]) if row else now, actor,
                    previous_version=pinned, current_version=pinned,
                )

            revision += 1
            cur.execute(
                """
                INSERT INTO pack_states
                    (org_id, pack_id, state, revision, reason, updated_by,
                     created_at, updated_at, pinned_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, pack_id) DO UPDATE SET
                    state      = EXCLUDED.state,
                    revision   = EXCLUDED.revision,
                    reason     = EXCLUDED.reason,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = EXCLUDED.updated_at
                """,
                (org, pack, target, revision, reason, actor, now, now, pinned),
            )
            cur.execute(
                """
                INSERT INTO pack_state_history
                    (id, org_id, pack_id, revision, transition, previous_state,
                     resulting_state, reason, actor_id, changed_at,
                     previous_version, resulting_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"psh_{uuid4().hex}", org, pack, revision, transition,
                    previous_state, target, reason, actor, now, pinned, pinned,
                ),
            )
            con.commit()
        finally:
            con.close()

        return PackStateOutcome(
            org, pack, transition, previous_state, target, revision, True,
            reason, now, actor, previous_version=pinned, current_version=pinned,
        )

    def history(self, org_id: str, pack_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        pack = _required(pack_id, "pack_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, revision, transition, previous_state, resulting_state,
                       reason, actor_id, changed_at,
                       previous_version, resulting_version
                FROM pack_state_history
                WHERE org_id = %s AND pack_id = %s
                ORDER BY revision DESC
                """,
                (org, pack),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [
            {
                "id": str(row[0]),
                "org_id": org,
                "pack_id": pack,
                "revision": int(row[1]),
                "transition": str(row[2]),
                "previous_state": str(row[3]),
                "resulting_state": str(row[4]),
                "reason": row[5],
                "actor_id": row[6],
                "changed_at": _iso(row[7]),
                "previous_version": row[8],
                "resulting_version": row[9],
            }
            for row in rows
        ]


def _iso(value: Any) -> Optional[str]:
    """Normalise a DB timestamp to ISO text; pass through an already-ISO string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# ── Store selection ───────────────────────────────────────────────────────────

_STORE: Optional[PackStateStore] = None


def get_pack_state_store() -> PackStateStore:
    global _STORE
    if _STORE is None:
        _STORE = PostgresPackStateStore()
    return _STORE


def set_pack_state_store(store: Optional[PackStateStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


# ── Public read/write API ─────────────────────────────────────────────────────


def set_pack_state(
    org_id: str,
    pack_id: str,
    state: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
) -> PackStateOutcome:
    """Transition a pack between ``active`` and ``disabled``.

    Raises :class:`PackNotFound` for an unregistered pack id and
    :class:`PackStateError` for a state outside the two legal values. Not
    fail-soft: a write failure propagates, because a disable that did not persist
    must never appear to have succeeded.
    """
    return get_pack_state_store().set_state(
        org_id, pack_id, state, actor_id, reason
    )


def disable_pack(
    org_id: str, pack_id: str, *, actor_id: str, reason: Optional[str] = None
) -> PackStateOutcome:
    """Stop a pack executing in future runs. Historical output is untouched."""
    return set_pack_state(
        org_id, pack_id, STATE_DISABLED, actor_id=actor_id, reason=reason
    )


def enable_pack(
    org_id: str, pack_id: str, *, actor_id: str, reason: Optional[str] = None
) -> PackStateOutcome:
    """Return a disabled pack to active. Re-enable is always supported (AC2)."""
    return set_pack_state(
        org_id, pack_id, STATE_ACTIVE, actor_id=actor_id, reason=reason
    )


def set_pinned_pack_version(
    org_id: str,
    pack_id: str,
    version: Optional[str],
    *,
    actor_id: str,
    reason: Optional[str] = None,
) -> PackStateOutcome:
    """Set or clear a pack's version pin — the single write the API edge calls.

    ``version`` ``None`` (or the pack's current version) clears the pin; any other
    value must be an archived version. :func:`rollback_pack_version` and
    :func:`restore_pack_version` are the intention-revealing wrappers.
    """
    return get_pack_state_store().set_pinned_version(
        org_id, pack_id, version, actor_id, reason
    )


def rollback_pack_version(
    org_id: str,
    pack_id: str,
    version: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
) -> PackStateOutcome:
    """Pin a pack to a PRIOR version so subsequent runs use it (2.0-C1 T3 / AC3).

    Only affects FUTURE runs. Existing findings keep the version stamp they were
    produced with, and nothing historical is rewritten or backfilled — the pin is a
    forward-looking configuration row, not a migration.

    Raises :class:`~discovery.packs.pack_config.PackVersionUnavailable` when the
    version has no archived artifact (naming the versions that do), and
    :class:`PackNotFound` for an unregistered pack id.
    """
    return get_pack_state_store().set_pinned_version(
        org_id, pack_id, version, actor_id, reason
    )


def restore_pack_version(
    org_id: str,
    pack_id: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
) -> PackStateOutcome:
    """Clear a version pin so the pack runs its CURRENT version again.

    The rollback stays on the append-only history — restoring does not erase it.
    """
    return get_pack_state_store().set_pinned_version(
        org_id, pack_id, None, actor_id, reason
    )


def get_pinned_pack_version(org_id: str, pack_id: str) -> Optional[str]:
    """The version this org pinned a pack to, or ``None`` when un-pinned."""
    return pack_state_rows(org_id).get(pack_id, {}).get("pinned_version")


def pinned_pack_versions(org_id: str) -> Dict[str, str]:
    """``{pack_id: pinned_version}`` for this org. Raises if unreadable."""
    return {
        pack_id: str(row["pinned_version"])
        for pack_id, row in pack_state_rows(org_id).items()
        if row.get("pinned_version")
    }


def pinned_pack_versions_safe(org_id: Optional[str]) -> Dict[str, str]:
    """:func:`pinned_pack_versions`, degrading to ``{}`` on any failure.

    Fail-soft for the same reason as :func:`disabled_pack_ids_safe`: an unreadable
    state store must not stop a run. Note the direction of the degradation — with no
    pin readable a run executes the CURRENT version, which is the platform's own
    shipped behaviour, and stamps that same current version. So the run stays
    self-consistent (it is never stamped with a version it did not execute); it
    simply does not honour the rollback. That is the safe failure for AC3: an honest
    current-version run beats a run stamped one version and behaving as another.
    """
    if not org_id:
        return {}
    try:
        return pinned_pack_versions(org_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pinned pack versions for org=%s; running current versions",
            org_id,
            exc_info=True,
        )
        return {}


def get_pack_state(org_id: str, pack_id: str) -> str:
    """This org's state for one pack. Unknown/unset ⇒ ``active``."""
    return get_pack_state_store().get_state(org_id, pack_id)


def pack_state_rows(org_id: str) -> Dict[str, Dict[str, Any]]:
    """Explicit state rows for this org, keyed by pack id (active packs absent)."""
    return get_pack_state_store().all_states(org_id)


def pack_state_history(org_id: str, pack_id: str) -> List[Dict[str, Any]]:
    """Append-only transition history for one pack, newest first."""
    return get_pack_state_store().history(org_id, pack_id)


def disabled_pack_ids(org_id: str) -> Set[str]:
    """Pack ids this org has disabled. Raises if the store cannot be read."""
    return {
        pack_id
        for pack_id, row in pack_state_rows(org_id).items()
        if str(row.get("state")) == STATE_DISABLED
    }


def disabled_pack_ids_safe(org_id: Optional[str]) -> Set[str]:
    """:func:`disabled_pack_ids`, degrading to an empty set on any failure.

    Used by READ paths (the finding-display label, run-health surfacing). A state
    store that is unreachable — or a deployment that has not yet applied migration
    0031 — must never stop a historical finding from being served: "all historical
    findings remain retrievable and viewable" outranks the label. The failure is
    logged so a persistently unlabelled disable is diagnosable.
    """
    if not org_id:
        return set()
    try:
        return disabled_pack_ids(org_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pack state for org=%s; treating every pack as active",
            org_id,
            exc_info=True,
        )
        return set()


def is_pack_disabled(org_id: str, pack_id: str) -> bool:
    """True when this org has disabled the pack. Fail-soft (⇒ False)."""
    return pack_id in disabled_pack_ids_safe(org_id)


def pack_state_view(org_id: str) -> List[Dict[str, Any]]:
    """Every pack with lifecycle state for this org — the surfacing shape.

    Covers the whole registry (packs with no explicit row report ``active`` at
    ``revision: 0``) PLUS any **orphaned** rows: state this org set for a pack that
    has since been REMOVED from the registry.

    2.0-C1 T4 (AT-829): orphaned rows are included on purpose. A removed pack's
    lifecycle row and its append-only history still exist in the database, and
    dropping them from the view would make that history unreachable — history you
    cannot reach is functionally deleted, which is exactly what AC4 forbids. Such a
    row is flagged ``registered: False``, and its version fields are reported as
    ``None`` because the registry no longer declares them: the platform states what
    it still knows and does not invent a version for a pack it no longer ships.
    """
    from discovery.packs.pack_config import (
        PACK_REGISTRY,
        get_pack_version,
        get_rollbackable_versions,
    )

    rows = _safe_state_rows(org_id)
    view: List[Dict[str, Any]] = []
    for pack_id, pack in PACK_REGISTRY.items():
        row = rows.get(pack_id) or {}
        current_version = get_pack_version(pack_id)
        pinned = row.get("pinned_version") or None
        view.append(
            {
                "packId": pack_id,
                "packName": pack.get("packName", pack_id),
                # The version the REGISTRY currently ships…
                "packVersion": current_version,
                "state": str(row.get("state") or STATE_ACTIVE),
                "revision": int(row.get("revision") or 0),
                "reason": row.get("reason"),
                "updatedBy": row.get("updated_by"),
                "updatedAt": row.get("updated_at"),
                # …and the 2.0-C1 T3 version position. `pinnedVersion` is None when
                # un-pinned; `effectiveVersion` is what a run STARTED NOW would
                # execute and stamp, which is the number an operator actually cares
                # about. `availableVersions` are the rollback targets (empty ⇒ this
                # pack cannot be rolled back).
                "pinnedVersion": pinned,
                "effectiveVersion": pinned or current_version,
                "availableVersions": get_rollbackable_versions(pack_id),
                "registered": True,
            }
        )

    for pack_id in sorted(set(rows) - set(PACK_REGISTRY)):
        row = rows[pack_id]
        view.append(
            {
                "packId": pack_id,
                "packName": pack_id,
                "packVersion": None,
                "state": str(row.get("state") or STATE_ACTIVE),
                "revision": int(row.get("revision") or 0),
                "reason": row.get("reason"),
                "updatedBy": row.get("updated_by"),
                "updatedAt": row.get("updated_at"),
                "pinnedVersion": row.get("pinned_version") or None,
                "effectiveVersion": None,
                "availableVersions": [],
                # The pack is no longer in the registry: it cannot run, but its
                # lifecycle state and history are retained and reachable.
                "registered": False,
            }
        )
    return view


def has_pack_lifecycle_record(org_id: str, pack_id: str) -> bool:
    """True when this org has ANY lifecycle state or history for a pack.

    2.0-C1 T4 (AT-829): lets a read surface serve a REMOVED pack's retained history
    instead of 404-ing on a registry lookup. Fail-soft — an unreadable store reports
    False, so an unknown id still reads as not-found rather than erroring.
    """
    org = str(org_id or "").strip()
    pack = str(pack_id or "").strip()
    if not org or not pack:
        return False
    if pack in _safe_state_rows(org):
        return True
    try:
        return bool(pack_state_history(org, pack))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pack lifecycle history for org=%s pack=%s",
            org,
            pack,
            exc_info=True,
        )
        return False


def _safe_state_rows(org_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if not org_id:
        return {}
    try:
        return pack_state_rows(org_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pack state rows for org=%s; reporting all active",
            org_id,
            exc_info=True,
        )
        return {}
