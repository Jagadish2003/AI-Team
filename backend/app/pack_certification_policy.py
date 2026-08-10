"""Pack certification policy — 2.0-C2 T4 (AT-834).

The rule this module owns (parent story AC3):

    An org policy restricting to Certified prevents activation of Partner/Community
    packs, with a clear reason.

A policy is a FLOOR, not a list
-------------------------------
An org declares the MINIMUM certification level a pack must hold to be activated
(``certified`` > ``partner`` > ``community``). A floor rather than an enumeration of
permitted levels, because the levels are genuinely ordered: an org that accepts
Partner packs necessarily accepts Certified ones, and expressing that as a set
invites the configuration where someone permits ``community`` but not ``partner``,
which means nothing.

``community`` is the DEFAULT floor and imposes no restriction — every pack clears it.
So provisioning this table changes no behaviour until an org opts in, exactly like
``pack_state``'s "absence of a row means active".

Fail CLOSED — deliberately unlike the rest of the pack lifecycle
----------------------------------------------------------------
``pack_state``'s reads are fail-soft: an unreadable store treats every pack as
active, because a display label must never hide a finding. This module does the
opposite, and the difference is the point.

This is a **security control**. A federal deployment sets "Certified only" precisely
so that an uncertified pack cannot run. If a policy read failed open, a transient
database problem would silently lift the restriction — the one moment it matters
most. So a policy that cannot be READ, or a pack level that cannot be VERIFIED,
refuses activation with an explicit reason rather than assuming compliance.

The availability cost is close to zero: runs are persisted in the same database, so a
deployment that cannot read its policy cannot start a run either way.

Be precise about what an unrestricted org does pay. One policy read per activation —
unavoidable, because you cannot know a policy is absent without looking, and treating
"could not look" as "absent" is the hole this posture exists to close. What it does
NOT pay is certification verification: the default ``community`` floor short-circuits
before any signature is checked, so only an org that has opted in does that work.

Refuse, don't exclude
---------------------
A policy violation REFUSES the activation (409), matching AT-826's compatibility gate
rather than AT-827's disable behaviour:

* **disabled** = "this customer turned the pack off" — a deliberate ongoing state, so
  the run proceeds without it;
* **policy violation** = "this selection is not allowed here" — a configuration error
  the operator must resolve, and quietly dropping the pack would leave a federal
  reviewer unable to tell a policy block from a pack that simply found nothing.

Nothing is ever deleted
-----------------------
Lifting a restriction WRITES ``community``; it does not delete the row. Every change
is an audit event, so "who lowered the floor, and when" is answerable — which for
this particular setting is the whole point.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app import db
from discovery.packs.pack_certification import (
    CERTIFICATION_LEVELS,
    LEVEL_COMMUNITY,
    LEVEL_LABELS,
    LEVEL_RANK,
    meets_minimum_level,
)

logger = logging.getLogger(__name__)

#: The default floor: no restriction. Every pack clears it.
DEFAULT_MINIMUM_LEVEL = LEVEL_COMMUNITY

#: Levels an org may set as its floor — the same three, ordered.
POLICY_LEVELS: List[str] = list(CERTIFICATION_LEVELS)


class PackCertificationPolicyError(ValueError):
    """The requested minimum level is not a legal certification level."""


class PackCertificationPolicyUnavailable(RuntimeError):
    """The org's policy could not be read, so compliance cannot be asserted.

    Raised instead of assuming "no restriction" — see the fail-closed note in the
    module docstring. ``str(exc)`` says plainly that the policy could not be
    verified, so an operator sees a policy problem rather than a mystery 500.
    """

    def __init__(self, org_id: str) -> None:
        self.org_id = org_id
        super().__init__(
            "The pack certification policy for this organisation could not be "
            "read, so pack activation cannot be verified against it. Activation is "
            "refused rather than proceeding as if no policy were set."
        )


@dataclass(frozen=True)
class PolicyViolation:
    """One pack that does not clear the org's floor."""

    pack_id: str
    level: str
    declared_level: str
    minimum_level: str

    @property
    def detail(self) -> str:
        """One sentence naming the pack, what it holds, and what is required."""
        held = LEVEL_LABELS.get(self.level, self.level)
        required = LEVEL_LABELS.get(self.minimum_level, self.minimum_level)
        # When the pack CLAIMED more than it can prove, say so: "Community" alone
        # would send an operator hunting for a pack that is, on paper, Certified.
        if self.declared_level and self.declared_level != self.level:
            claimed = LEVEL_LABELS.get(self.declared_level, self.declared_level)
            held = (
                f"{held} (it claims {claimed}, but the claim could not be verified)"
            )
        return (
            f"pack '{self.pack_id}' is {held}; this organisation requires "
            f"{required} or higher"
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "packId": self.pack_id,
            "level": self.level,
            "declaredLevel": self.declared_level,
            "minimumLevel": self.minimum_level,
            "reason": self.detail,
        }


class PackCertificationPolicyViolation(Exception):
    """Raised when a selection would activate a pack below the org's floor.

    ``str(exc)`` names EVERY violating pack and the level each holds, so a caller
    fixing a multi-pack selection sees all of it at once (the same discipline as
    ``PackIncompatibleError``).
    """

    def __init__(self, violations: Sequence[PolicyViolation]) -> None:
        self.violations: List[PolicyViolation] = list(violations)
        minimum = (
            self.violations[0].minimum_level if self.violations else DEFAULT_MINIMUM_LEVEL
        )
        required = LEVEL_LABELS.get(minimum, minimum)
        super().__init__(
            f"This organisation's pack certification policy requires {required} "
            f"packs or higher. Refused: "
            f"{'; '.join(item.detail for item in self.violations)}."
        )

    @property
    def pack_ids(self) -> List[str]:
        return [item.pack_id for item in self.violations]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "pack_certification_policy",
            "message": str(self),
            "packs": [item.to_dict() for item in self.violations],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_level(level: Any) -> str:
    normalized = str(level or "").strip().lower()
    if normalized not in LEVEL_RANK:
        raise PackCertificationPolicyError(
            f"minimumLevel must be one of {', '.join(POLICY_LEVELS)}, got {level!r}"
        )
    return normalized


@dataclass(frozen=True)
class PackCertificationPolicy:
    """One org's activation floor."""

    org_id: str
    minimum_level: str = DEFAULT_MINIMUM_LEVEL
    revision: int = 0
    reason: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def restricted(self) -> bool:
        """True when the floor actually excludes something."""
        return LEVEL_RANK.get(self.minimum_level, 0) > LEVEL_RANK[LEVEL_COMMUNITY]

    @property
    def label(self) -> str:
        if not self.restricted:
            return "No certification restriction"
        return f"{LEVEL_LABELS.get(self.minimum_level, self.minimum_level)} or higher"

    def permits(self, level: str) -> bool:
        return meets_minimum_level(level, self.minimum_level)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orgId": self.org_id,
            "minimumLevel": self.minimum_level,
            "minimumLevelLabel": LEVEL_LABELS.get(
                self.minimum_level, self.minimum_level
            ),
            "restricted": self.restricted,
            "label": self.label,
            "revision": self.revision,
            "reason": self.reason,
            "updatedBy": self.updated_by,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class PolicyOutcome:
    """The result of a policy write."""

    policy: PackCertificationPolicy
    previous_level: str
    changed: bool
    actor_id: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            **self.policy.to_dict(),
            "previousMinimumLevel": self.previous_level,
            "changed": self.changed,
            "actorId": self.actor_id,
        }


# ── Store contract ────────────────────────────────────────────────────────────


class PackCertificationPolicyStore:
    """Read/write contract. No delete operation exists — lifting a restriction
    writes ``community``, so the change is a recorded transition rather than the
    disappearance of one."""

    def get(self, org_id: str) -> PackCertificationPolicy:
        raise NotImplementedError

    def set_minimum_level(
        self,
        org_id: str,
        minimum_level: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PolicyOutcome:
        raise NotImplementedError


class InMemoryPackCertificationPolicyStore(PackCertificationPolicyStore):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get(self, org_id: str) -> PackCertificationPolicy:
        org = str(org_id or "").strip()
        with self._lock:
            row = self._rows.get(org)
        if not row:
            return PackCertificationPolicy(org_id=org)
        return PackCertificationPolicy(
            org_id=org,
            minimum_level=str(row["minimum_level"]),
            revision=int(row["revision"]),
            reason=row.get("reason"),
            updated_by=row.get("updated_by"),
            updated_at=row.get("updated_at"),
        )

    def set_minimum_level(
        self,
        org_id: str,
        minimum_level: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PolicyOutcome:
        org = str(org_id or "").strip()
        target = _validated_level(minimum_level)
        actor = str(actor_id or "").strip()
        now = _now()

        with self._lock:
            row = self._rows.get(org)
            previous = str(row["minimum_level"]) if row else DEFAULT_MINIMUM_LEVEL
            revision = int(row["revision"]) if row else 0

            if previous == target:
                return PolicyOutcome(self.get(org), previous, False, actor)

            revision += 1
            self._rows[org] = {
                "minimum_level": target,
                "revision": revision,
                "reason": reason,
                "updated_by": actor,
                "created_at": (row or {}).get("created_at", now),
                "updated_at": now,
            }
        return PolicyOutcome(self.get(org), previous, True, actor)


class PostgresPackCertificationPolicyStore(PackCertificationPolicyStore):
    """Production store. Migration 0035 / provision.sql provision its table."""

    def get(self, org_id: str) -> PackCertificationPolicy:
        org = str(org_id or "").strip()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT minimum_level, revision, reason, updated_by, updated_at "
                "FROM pack_certification_policies WHERE org_id = %s",
                (org,),
            )
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            return PackCertificationPolicy(org_id=org)
        return PackCertificationPolicy(
            org_id=org,
            minimum_level=str(row[0]),
            revision=int(row[1]),
            reason=row[2],
            updated_by=row[3],
            updated_at=_iso(row[4]),
        )

    def set_minimum_level(
        self,
        org_id: str,
        minimum_level: str,
        actor_id: str,
        reason: Optional[str] = None,
    ) -> PolicyOutcome:
        org = str(org_id or "").strip()
        target = _validated_level(minimum_level)
        actor = str(actor_id or "").strip()
        now = _now()

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT minimum_level, revision FROM pack_certification_policies "
                "WHERE org_id = %s FOR UPDATE",
                (org,),
            )
            row = cur.fetchone()
            previous = str(row[0]) if row else DEFAULT_MINIMUM_LEVEL
            revision = int(row[1]) if row else 0

            if previous == target:
                con.commit()
                return PolicyOutcome(self.get(org), previous, False, actor)

            revision += 1
            cur.execute(
                """
                INSERT INTO pack_certification_policies
                    (org_id, minimum_level, revision, reason, updated_by,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id) DO UPDATE SET
                    minimum_level = EXCLUDED.minimum_level,
                    revision      = EXCLUDED.revision,
                    reason        = EXCLUDED.reason,
                    updated_by    = EXCLUDED.updated_by,
                    updated_at    = EXCLUDED.updated_at
                """,
                (org, target, revision, reason, actor, now, now),
            )
            con.commit()
        finally:
            con.close()
        return PolicyOutcome(self.get(org), previous, True, actor)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# ── Store selection ───────────────────────────────────────────────────────────

_STORE: Optional[PackCertificationPolicyStore] = None


def get_policy_store() -> PackCertificationPolicyStore:
    global _STORE
    if _STORE is None:
        _STORE = PostgresPackCertificationPolicyStore()
    return _STORE


def set_policy_store(store: Optional[PackCertificationPolicyStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


# ── Public API ────────────────────────────────────────────────────────────────


def get_certification_policy(org_id: str) -> PackCertificationPolicy:
    """This org's activation floor.

    Raises :class:`PackCertificationPolicyUnavailable` if the store cannot be read —
    NOT the default policy. Returning "no restriction" on a read failure would lift
    the restriction exactly when it matters most (see the module docstring).
    """
    try:
        return get_policy_store().get(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Could not read the pack certification policy for org=%s; refusing to "
            "assume no restriction",
            org_id,
            exc_info=True,
        )
        raise PackCertificationPolicyUnavailable(str(org_id or "")) from exc


def set_certification_policy(
    org_id: str,
    minimum_level: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
) -> PolicyOutcome:
    """Set this org's activation floor. Owner-level operation.

    ``community`` lifts the restriction. Not fail-soft: a policy change that did not
    persist must never look as though it succeeded — least of all a TIGHTENING that
    an operator then believes is in force.
    """
    return get_policy_store().set_minimum_level(
        org_id, minimum_level, actor_id, reason
    )


def check_selection(
    org_id: str, pack_ids: Iterable[str]
) -> List[PolicyViolation]:
    """Every pack in the selection that does not clear this org's floor.

    Returns an empty list when the selection complies — including the common case
    of no restriction, which short-circuits before any certification is verified, so
    a deployment that has not opted in pays nothing for this check.
    """
    policy = get_certification_policy(org_id)
    if not policy.restricted:
        return []

    packs = [str(pack_id).strip() for pack_id in pack_ids if str(pack_id).strip()]
    if not packs:
        return []

    from discovery.packs.pack_certification import certification_badges

    try:
        badges = certification_badges(packs)
    except Exception as exc:  # noqa: BLE001
        # A restriction is in force and the levels cannot be verified. Compliance is
        # unprovable, so it is not assumed.
        logger.error(
            "Could not verify pack certification for org=%s under a %s policy",
            org_id,
            policy.minimum_level,
            exc_info=True,
        )
        raise PackCertificationPolicyUnavailable(str(org_id or "")) from exc

    violations: List[PolicyViolation] = []
    for pack_id in packs:
        badge = badges.get(pack_id)
        if badge is None:
            # An unresolvable badge under an active restriction is a violation, not
            # a pass: "we could not tell" must never read as "it qualifies".
            violations.append(
                PolicyViolation(
                    pack_id=pack_id,
                    level=LEVEL_COMMUNITY,
                    declared_level="",
                    minimum_level=policy.minimum_level,
                )
            )
            continue
        if not policy.permits(badge["level"]):
            violations.append(
                PolicyViolation(
                    pack_id=pack_id,
                    level=badge["level"],
                    declared_level=badge.get("declaredLevel", ""),
                    minimum_level=policy.minimum_level,
                )
            )
    return violations


def assert_selection_permitted(
    org_id: str, pack_ids: Iterable[str]
) -> PackCertificationPolicy:
    """Gate a selection against the org's floor — the one call activation makes.

    Raises :class:`PackCertificationPolicyViolation` naming every offending pack, or
    :class:`PackCertificationPolicyUnavailable` when compliance cannot be verified.
    Returns the policy when the selection is permitted.
    """
    violations = check_selection(org_id, pack_ids)
    if violations:
        raise PackCertificationPolicyViolation(violations)
    return get_certification_policy(org_id)


def annotate_activation_blocked(
    org_id: str, rows: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Stamp pack-selection rows with whether the org's policy blocks them.

    Additive (the ``connector_roadmap.annotate_connector`` pattern): a selection
    surface can grey out a pack it would only be refused for later, which is a much
    better experience than a 409 after the user has configured a whole run.

    Fail-soft HERE and only here — this is the display path, not the gate. An
    unreadable policy leaves the rows unannotated; the enforcement point still
    refuses (:func:`assert_selection_permitted`), so a surfacing hiccup can never
    become a way past the policy.
    """
    try:
        policy = get_certification_policy(org_id)
    except PackCertificationPolicyUnavailable:
        return [dict(row) for row in rows]
    if not policy.restricted:
        return [
            {**row, "activationBlocked": False, "activationBlockedReason": None}
            for row in rows
        ]

    annotated: List[Dict[str, Any]] = []
    for row in rows:
        certification = row.get("certification") or {}
        level = str(certification.get("level") or "")
        blocked = not (level and policy.permits(level))
        reason = None
        if blocked:
            reason = PolicyViolation(
                pack_id=str(row.get("packId") or ""),
                level=level or LEVEL_COMMUNITY,
                declared_level=str(certification.get("declaredLevel") or ""),
                minimum_level=policy.minimum_level,
            ).detail
        annotated.append(
            {
                **row,
                "activationBlocked": blocked,
                "activationBlockedReason": reason,
            }
        )
    return annotated


def record_policy_refusal(
    *,
    org_id: str,
    error: PackCertificationPolicyViolation,
    run_id: Optional[str] = None,
) -> None:
    """Emit the refusal so it is observable beyond the HTTP 409 the caller saw once.

    Observability only — a telemetry failure must never mask the refusal itself.
    """
    from .telemetry import record_event

    try:
        record_event(
            "pack.certification_policy_refused",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_ids": error.pack_ids,
                "minimum_level": (
                    error.violations[0].minimum_level
                    if error.violations
                    else DEFAULT_MINIMUM_LEVEL
                ),
                "violations": [item.to_dict() for item in error.violations],
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.certification_policy_refused telemetry failed (non-blocking)",
            exc_info=True,
        )


__all__ = [
    "DEFAULT_MINIMUM_LEVEL",
    "InMemoryPackCertificationPolicyStore",
    "POLICY_LEVELS",
    "PackCertificationPolicy",
    "PackCertificationPolicyError",
    "PackCertificationPolicyStore",
    "PackCertificationPolicyUnavailable",
    "PackCertificationPolicyViolation",
    "PolicyOutcome",
    "PolicyViolation",
    "PostgresPackCertificationPolicyStore",
    "annotate_activation_blocked",
    "assert_selection_permitted",
    "check_selection",
    "get_certification_policy",
    "get_policy_store",
    "record_policy_refusal",
    "set_certification_policy",
    "set_policy_store",
]
