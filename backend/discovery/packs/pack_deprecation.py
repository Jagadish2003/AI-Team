"""Pack deprecation metadata — 2.0-C4 T1 (AT-842).

The rule this module owns (sub-task scope):

    A pack version can be marked deprecated with a reason, a grace period, and an
    optional replacement pack.

Why this exists
---------------
With an ecosystem, packs get superseded. A customer whose configuration references
a superseded pack needs three things in this order: **notice** (this is going away,
here is why, here is the date), a **grace period** (it keeps working meanwhile), and
a **path** (this is what replaces it). Deleting the pack, or refusing to run it the
day it is superseded, gives them a broken configuration instead.

This module is the metadata + evaluation half of 2.0-C4. It answers "is this pack
version deprecated, since when, until when, and what replaces it" and nothing else.
Surfacing the notice (AT-843), migration assist (AT-844), the safe-disable that
follows an expired grace (AT-845), and the audit events for all three (AT-846) are
separate tasks layered on top of what this module reports.

What is declared, and where
---------------------------
In the pack registry, under ``pack_config.DEPRECATION_KEY`` — the same declaration
surface as ``compatibility`` (AT-826) and ``certification`` (AT-831), so everything
a pack states about itself is read from one place. Deprecation is a statement by
whoever ships the registry, which is why it lives beside them and not in per-org
``pack_states`` (that table is the CUSTOMER's dimension — disable, rollback — and
merging the two would let a customer "undeprecate" a superseded pack, or make a
vendor notice look like a customer decision).

No signature
------------
Unlike certification, a deprecation notice is not a claim about a third party — it
is the registry shipper stating that its own pack is superseded. There is nobody to
self-certify against, so there is nothing for a signature to protect.

Version-scoped
--------------
A deprecation names the versions it applies to (``versions``). An empty list means
EVERY version of the pack — the "this pack is superseded" case. Naming versions
covers the narrower "this release line is superseded, the current one is fine" case,
which matters because 2.0-C1 T3 rollback lets an org pin an archived version.

Three phases
------------
``active`` (not deprecated) → ``grace`` (deprecated, still runs normally) →
``grace_expired`` (AT-845 moves it to safe-disabled). The phase is DERIVED from the
dates on every read rather than stored, so a grace period expires on its own without
a job having to notice.

Open-ended grace is a first-class state
---------------------------------------
A deprecation with no end date (no ``graceEndsOn``, no ``gracePeriodDays``) is
"deprecated, no removal date announced yet" — a real and common state. It surfaces
the notice and NEVER expires, so it can never trigger a safe-disable. Silently
defaulting to some grace length would auto-disable a customer's pack on a date
nobody declared.

Failure posture: notice loudly, never auto-disable on bad data
--------------------------------------------------------------
Evaluation never raises; the verdict is the return value. A malformed declaration
degrades in the direction that is safe for the customer:

* a deprecation missing its reason or date STILL reports as deprecated (suppressing
  a real notice would leave a customer unwarned), with the defect named in
  :attr:`PackDeprecation.issues`;
* a grace period that cannot be read becomes OPEN-ENDED rather than expired — a
  typo must never take a working pack offline;
* a replacement that is not a registered pack is dropped and named, because pointing
  a customer at a pack that does not exist is worse than offering no path at all.

Shipped declarations are held to a stricter bar than that by structural tests
(``discovery/tests/test_pack_deprecation.py``): a declaration with any issue fails
the build, so the tolerant runtime behaviour is a safety net, not the contract.

Deliberately dependency-free of ``app``
---------------------------------------
Same posture as ``platform_capabilities.py`` / ``pack_compatibility.py`` /
``pack_certification.py``: no ``app`` import and no DB, so the activation edges AND
the discovery runner can both consult deprecation without the runner taking an
``app`` dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .pack_config import (
    get_pack,
    get_pack_deprecation_declaration,
    get_pack_version,
    get_rollbackable_versions,
    normalize_pack_ids,
)

logger = logging.getLogger(__name__)

# ── Declared status ───────────────────────────────────────────────────────────

#: Not deprecated. The default, and what the absence of a declaration means.
STATUS_ACTIVE = "active"
#: Superseded. Runs normally until the grace period ends (AT-845).
STATUS_DEPRECATED = "deprecated"

DEPRECATION_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DEPRECATED})


# ── Derived phase ─────────────────────────────────────────────────────────────

#: No deprecation applies to this pack version.
PHASE_ACTIVE = "active"
#: Deprecated and inside its grace period — the pack runs normally (AC3).
PHASE_GRACE = "grace"
#: Deprecated and past its grace period — AT-845 moves it to safe-disabled.
PHASE_GRACE_EXPIRED = "grace_expired"

PHASE_LABELS: Dict[str, str] = {
    PHASE_ACTIVE: "Active",
    PHASE_GRACE: "Deprecated",
    PHASE_GRACE_EXPIRED: "Deprecated — grace period ended",
}


# ── Declaration defects ───────────────────────────────────────────────────────
#
# Named rather than boolean, because "this deprecation is malformed" gives a
# maintainer nothing to fix. Every one of these fails the build for a SHIPPED
# declaration; at runtime they only annotate the verdict.

#: Deprecated with no reason. A notice a customer cannot act on.
ISSUE_MISSING_REASON = "missing_reason"
#: Deprecated with no ``deprecatedOn`` date — AC1 requires the notice to carry one.
ISSUE_MISSING_DEPRECATED_ON = "missing_deprecated_on"
#: A declared date is not a readable ISO (``YYYY-MM-DD``) date.
ISSUE_UNREADABLE_DATE = "unreadable_date"
#: ``gracePeriodDays`` is not a non-negative whole number of days.
ISSUE_INVALID_GRACE_PERIOD = "invalid_grace_period"
#: Both ``graceEndsOn`` and ``gracePeriodDays`` are declared and disagree.
ISSUE_CONFLICTING_GRACE = "conflicting_grace_period"
#: The named replacement is not a registered pack, so the path leads nowhere.
ISSUE_UNKNOWN_REPLACEMENT = "unknown_replacement_pack"
#: The pack names ITSELF as its replacement.
ISSUE_SELF_REPLACEMENT = "self_replacement"
#: A scoped version is neither the current version nor an archived one — a typo,
#: and a dangerous one: the deprecation silently applies to nothing.
ISSUE_UNKNOWN_VERSION_SCOPE = "unknown_version_scope"
#: ``status`` is a value this module does not recognise.
ISSUE_INVALID_STATUS = "invalid_status"


# ── Date handling ─────────────────────────────────────────────────────────────


def parse_deprecation_date(value: Optional[str]) -> Optional[date]:
    """Parse a declared deprecation date (``YYYY-MM-DD``), or ``None``.

    Deliberately strict about the shape: these dates are written by whoever ships
    the registry, and they drive a safe-disable. A tolerant parser would only ever
    succeed in hiding a malformed date until the day it disabled something.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _today(as_of: Optional[date] = None) -> date:
    """Evaluation date. ``as_of`` is injected by tests so a grace-period test does
    not become a time bomb the day the fixture dates age out."""
    return as_of or datetime.now(timezone.utc).date()


def _iso(value: Optional[date]) -> str:
    return value.isoformat() if value is not None else ""


# ── The verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackDeprecation:
    """One pack version's deprecation position, as of an evaluation date.

    ``deprecated`` is the fact; ``phase`` is where that fact has got to in time.
    Both are derived on read — a grace period ends because the date passed, not
    because something noticed.
    """

    pack_id: str
    pack_name: str
    #: The version this verdict is about (the pack's current version by default).
    version: str
    deprecated: bool
    phase: str
    #: The status the registry DECLARED, normalised. ``active`` when undeclared.
    declared_status: str = STATUS_ACTIVE
    reason: str = ""
    deprecated_on: str = ""
    #: Last day the pack runs normally. Empty ⇒ open-ended (never expires).
    grace_ends_on: str = ""
    #: The declared period, when the end date was derived from one.
    grace_period_days: Optional[int] = None
    #: Versions the deprecation is scoped to. Empty ⇒ every version of the pack.
    applies_to_versions: List[str] = field(default_factory=list)
    replacement_pack_id: str = ""
    replacement_pack_name: str = ""
    replacement_min_version: str = ""
    replacement_notes: str = ""
    #: Declaration defects, named. Empty for a well-formed declaration.
    issues: List[str] = field(default_factory=list)
    #: The date this verdict was evaluated against (``YYYY-MM-DD``).
    evaluated_on: str = ""

    # ── Derived state ─────────────────────────────────────────────────────────

    @property
    def in_grace(self) -> bool:
        """Deprecated and still running normally (parent-story AC3)."""
        return self.phase == PHASE_GRACE

    @property
    def grace_expired(self) -> bool:
        """Deprecated and past its grace period — the AT-845 safe-disable trigger."""
        return self.phase == PHASE_GRACE_EXPIRED

    @property
    def open_ended_grace(self) -> bool:
        """Deprecated with no announced end date, so it can never auto-disable."""
        return self.deprecated and not self.grace_ends_on

    @property
    def has_replacement(self) -> bool:
        """True when a REGISTERED replacement pack was named (AC1's "path")."""
        return bool(self.replacement_pack_id)

    @property
    def valid(self) -> bool:
        """True when the declaration carries no named defect."""
        return not self.issues

    @property
    def days_remaining(self) -> Optional[int]:
        """Whole days of grace left, ``0`` once expired, ``None`` if open-ended.

        What a "deprecated — 12 days left" surface reads, and what makes the notice
        actionable before the pack stops running rather than after.
        """
        if not self.deprecated:
            return None
        ends = parse_deprecation_date(self.grace_ends_on)
        evaluated = parse_deprecation_date(self.evaluated_on)
        if ends is None or evaluated is None:
            return None
        return max(0, (ends - evaluated).days)

    # ── Display ───────────────────────────────────────────────────────────────

    @property
    def label(self) -> str:
        """The badge text for this phase."""
        return PHASE_LABELS.get(self.phase, PHASE_LABELS[PHASE_ACTIVE])

    @property
    def status_label(self) -> str:
        """Badge plus its qualifier, for a single-line surface."""
        if not self.deprecated:
            return PHASE_LABELS[PHASE_ACTIVE]
        if self.grace_expired:
            return PHASE_LABELS[PHASE_GRACE_EXPIRED]
        if self.grace_ends_on:
            return f"Deprecated — runs until {self.grace_ends_on}"
        return "Deprecated"

    @property
    def replacement_label(self) -> str:
        """The replacement, named for a human. Empty when none was offered."""
        if not self.has_replacement:
            return ""
        name = self.replacement_pack_name or self.replacement_pack_id
        if self.replacement_min_version:
            return f"{name} ({self.replacement_pack_id} v{self.replacement_min_version}+)"
        return f"{name} ({self.replacement_pack_id})"

    @property
    def summary(self) -> str:
        """One human sentence carrying the reason, the dates, and the path.

        This is the notice text AT-843 surfaces at run configuration, in run health,
        and on findings — one sentence composed once, so those three surfaces cannot
        word the same deprecation differently.
        """
        if not self.deprecated:
            return f"Pack '{self.pack_id}' v{self.version} is not deprecated."

        parts = [
            f"Pack '{self.pack_id}' v{self.version} is deprecated"
            + (f" as of {self.deprecated_on}" if self.deprecated_on else "")
            + (f": {self.reason}" if self.reason else ".")
        ]
        if not self.reason:
            parts[0] = parts[0].rstrip(".") + "."

        if self.grace_expired:
            parts.append(
                f"Its grace period ended on {self.grace_ends_on}, so it no longer "
                f"runs; existing findings and run history remain intact."
            )
        elif self.grace_ends_on:
            parts.append(
                f"It runs normally until {self.grace_ends_on}, after which it will "
                f"be disabled."
            )
        else:
            parts.append("It runs normally; no removal date has been announced.")

        if self.has_replacement:
            parts.append(f"Replaced by {self.replacement_label}.")
            if self.replacement_notes:
                parts.append(self.replacement_notes)
        else:
            parts.append("No replacement pack has been named.")
        return " ".join(part.strip() for part in parts if part.strip())

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable audit shape — the full picture, defects included.

        :func:`deprecation_notice` is the compact projection a surface renders.
        """
        return {
            "packId": self.pack_id,
            "packName": self.pack_name,
            "version": self.version,
            "deprecated": self.deprecated,
            "phase": self.phase,
            "phaseLabel": self.label,
            "statusLabel": self.status_label,
            "declaredStatus": self.declared_status,
            "reason": self.reason,
            "deprecatedOn": self.deprecated_on,
            "graceEndsOn": self.grace_ends_on,
            "gracePeriodDays": self.grace_period_days,
            "graceOpenEnded": self.open_ended_grace,
            "daysRemaining": self.days_remaining,
            "appliesToVersions": list(self.applies_to_versions),
            "replacementPackId": self.replacement_pack_id,
            "replacementPackName": self.replacement_pack_name,
            "replacementMinVersion": self.replacement_min_version,
            "replacementNotes": self.replacement_notes,
            "replacementLabel": self.replacement_label,
            "issues": list(self.issues),
            "evaluatedOn": self.evaluated_on,
            "summary": self.summary,
        }


# ── Evaluation ────────────────────────────────────────────────────────────────


def get_pack_deprecation(
    pack_id: Optional[str] = None,
    *,
    version: Optional[str] = None,
    as_of: Optional[date] = None,
) -> PackDeprecation:
    """Evaluate one pack version's deprecation position. Never raises.

    ``version`` defaults to the pack's CURRENT ``packVersion``; pass an archived
    version to ask about a rolled-back pin (2.0-C1 T3). ``pack_id`` resolves through
    ``get_pack()``, so an unknown id reports the default pack exactly as it reports
    its detectors — an unknown id is not a deprecation.

    ``as_of`` injects the evaluation date so grace-period tests stay deterministic
    instead of depending on the day CI runs.
    """
    pack = get_pack(pack_id)
    resolved_id = pack["packId"]
    pack_name = pack.get("packName", resolved_id)
    resolved_version = str(version or "").strip() or get_pack_version(pack_id)
    evaluated = _today(as_of)

    declaration = get_pack_deprecation_declaration(pack_id)
    issues: List[str] = []

    status = _resolve_status(declaration, issues)
    scope = list(declaration["versions"])
    _check_version_scope(resolved_id, scope, issues)

    if status != STATUS_DEPRECATED or not _applies_to(scope, resolved_version):
        # Not deprecated — but declaration defects are still reported, because a
        # deprecation that applies to nothing because of a typo is exactly the
        # failure a maintainer needs told about.
        return PackDeprecation(
            pack_id=resolved_id,
            pack_name=pack_name,
            version=resolved_version,
            deprecated=False,
            phase=PHASE_ACTIVE,
            declared_status=status,
            applies_to_versions=scope,
            issues=issues,
            evaluated_on=_iso(evaluated),
        )

    reason = declaration["reason"]
    if not reason:
        issues.append(ISSUE_MISSING_REASON)

    deprecated_on = _resolve_date(
        declaration["deprecatedOn"], issues, required=True
    )
    grace_ends_on, grace_days = _resolve_grace(declaration, deprecated_on, issues)
    replacement = _resolve_replacement(resolved_id, declaration, issues)

    # An unreadable or absent end date leaves the grace OPEN-ENDED, so a malformed
    # declaration can never take a working pack offline.
    phase = (
        PHASE_GRACE_EXPIRED
        if grace_ends_on is not None and evaluated > grace_ends_on
        else PHASE_GRACE
    )

    if issues:
        logger.warning(
            "Pack %r declares a deprecation with defects (%s); surfacing the notice "
            "anyway with the defects named",
            resolved_id,
            ", ".join(issues),
        )

    return PackDeprecation(
        pack_id=resolved_id,
        pack_name=pack_name,
        version=resolved_version,
        deprecated=True,
        phase=phase,
        declared_status=status,
        reason=reason,
        deprecated_on=_iso(deprecated_on),
        grace_ends_on=_iso(grace_ends_on),
        grace_period_days=grace_days,
        applies_to_versions=scope,
        replacement_pack_id=replacement["packId"],
        replacement_pack_name=replacement["packName"],
        replacement_min_version=replacement["minVersion"],
        replacement_notes=replacement["notes"],
        issues=issues,
        evaluated_on=_iso(evaluated),
    )


def _resolve_status(declaration: Mapping[str, Any], issues: List[str]) -> str:
    """The declared status, inferring ``deprecated`` from a populated block.

    A declaration that carries a reason or a date but forgets ``status`` is a real
    notice with a missing field — reading it as active would suppress it, which is
    the one failure mode this feature exists to prevent.
    """
    declared = str(declaration.get("status") or "").strip().lower()
    populated = (
        any(
            declaration.get(key)
            for key in ("reason", "deprecatedOn", "graceEndsOn")
        )
        # `is not None` rather than truthiness: a zero-day grace period is a real
        # (if severe) declaration, not an empty field.
        or declaration.get("gracePeriodDays") is not None
        or bool((declaration.get("replacement") or {}).get("packId"))
    )

    if not declared:
        return STATUS_DEPRECATED if populated else STATUS_ACTIVE
    if declared not in DEPRECATION_STATUSES:
        issues.append(ISSUE_INVALID_STATUS)
        return STATUS_DEPRECATED if populated else STATUS_ACTIVE
    return declared


def _applies_to(scope: List[str], version: str) -> bool:
    """True when a deprecation scoped to ``scope`` covers ``version``.

    An empty scope means EVERY version — the "this pack is superseded" case.
    """
    return not scope or version in scope


def _check_version_scope(
    pack_id: str, scope: List[str], issues: List[str]
) -> None:
    """Flag a scoped version the pack does not declare (current or archived)."""
    if not scope:
        return
    known = {get_pack_version(pack_id), *get_rollbackable_versions(pack_id)}
    if any(version not in known for version in scope):
        issues.append(ISSUE_UNKNOWN_VERSION_SCOPE)


def _resolve_date(
    raw: str, issues: List[str], *, required: bool
) -> Optional[date]:
    text = str(raw or "").strip()
    if not text:
        if required:
            issues.append(ISSUE_MISSING_DEPRECATED_ON)
        return None
    parsed = parse_deprecation_date(text)
    if parsed is None:
        issues.append(ISSUE_UNREADABLE_DATE)
    return parsed


def _resolve_grace(
    declaration: Mapping[str, Any],
    deprecated_on: Optional[date],
    issues: List[str],
) -> tuple[Optional[date], Optional[int]]:
    """Resolve the last day the pack runs normally, and the declared period.

    ``graceEndsOn`` is the authoritative end date when present; ``gracePeriodDays``
    DERIVES one from ``deprecatedOn`` when it is not. When both are declared and
    they disagree the conflict is named and the LATER date wins — a declaration
    mistake must never shorten a customer's grace.

    ``(None, ...)`` means open-ended: no announced removal date, so the pack never
    reaches ``grace_expired`` and can never be auto-disabled.
    """
    declared_end = _resolve_date(
        declaration.get("graceEndsOn", ""), issues, required=False
    )

    raw_days = declaration.get("gracePeriodDays")
    days: Optional[int] = None
    if raw_days is not None:
        if isinstance(raw_days, bool) or not isinstance(raw_days, int):
            issues.append(ISSUE_INVALID_GRACE_PERIOD)
        elif raw_days < 0:
            issues.append(ISSUE_INVALID_GRACE_PERIOD)
        else:
            days = raw_days

    derived_end = (
        deprecated_on + timedelta(days=days)
        if days is not None and deprecated_on is not None
        else None
    )

    if declared_end is not None and derived_end is not None:
        if declared_end != derived_end:
            issues.append(ISSUE_CONFLICTING_GRACE)
        return max(declared_end, derived_end), days
    return (declared_end or derived_end), days


def _resolve_replacement(
    pack_id: str, declaration: Mapping[str, Any], issues: List[str]
) -> Dict[str, str]:
    """Resolve the optional replacement pack, dropping one that leads nowhere.

    The replacement must be a REGISTERED pack: an unregistered id is dropped and
    named, because a migration path pointing at a pack that does not exist is worse
    than no path at all (AT-844 would offer a migration that cannot be applied).
    Membership is checked against the registry directly rather than via
    ``get_pack()``, which resolves an unknown id to the default pack.
    """
    from .pack_config import PACK_REGISTRY

    declared = declaration.get("replacement") or {}
    replacement_id = str(declared.get("packId") or "").strip()
    empty = {"packId": "", "packName": "", "minVersion": "", "notes": ""}
    if not replacement_id:
        return empty

    if replacement_id == pack_id:
        issues.append(ISSUE_SELF_REPLACEMENT)
        return empty
    if replacement_id not in PACK_REGISTRY:
        issues.append(ISSUE_UNKNOWN_REPLACEMENT)
        return empty

    return {
        "packId": replacement_id,
        "packName": PACK_REGISTRY[replacement_id].get("packName", replacement_id),
        "minVersion": str(declared.get("minVersion") or "").strip(),
        "notes": str(declared.get("notes") or "").strip(),
    }


# ── Query helpers ─────────────────────────────────────────────────────────────


def is_pack_deprecated(
    pack_id: Optional[str] = None,
    *,
    version: Optional[str] = None,
    as_of: Optional[date] = None,
) -> bool:
    """True when a deprecation applies to this pack version (in grace or expired)."""
    return get_pack_deprecation(
        pack_id, version=version, as_of=as_of
    ).deprecated


def is_grace_expired(
    pack_id: Optional[str] = None,
    *,
    version: Optional[str] = None,
    as_of: Optional[date] = None,
) -> bool:
    """True when the grace period has ended — AT-845's safe-disable trigger.

    False for a pack that is not deprecated AND for one whose grace is open-ended,
    so a deprecation with no announced removal date never disables anything.
    """
    return get_pack_deprecation(
        pack_id, version=version, as_of=as_of
    ).grace_expired


def replacement_pack_id(
    pack_id: Optional[str] = None, *, version: Optional[str] = None
) -> Optional[str]:
    """The registered pack that supersedes this one, or ``None``.

    AT-844's migration target: a value here means a migration can actually be
    previewed and applied, and ``None`` means the honest answer is "no path yet".
    """
    return (
        get_pack_deprecation(pack_id, version=version).replacement_pack_id or None
    )


# ── Surfacing projections ─────────────────────────────────────────────────────


def deprecation_notice(
    pack_id: Optional[str] = None,
    *,
    version: Optional[str] = None,
    as_of: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """The COMPACT notice a surface renders, or ``None`` when there is no notice.

    :meth:`PackDeprecation.to_dict` is the full audit shape (declaration defects,
    scope, declared status). That is the right payload for a governance API and the
    wrong one to staple onto every finding in a 200-item list, so surfacing gets this
    projection instead.

    ``None`` — rather than an object saying "not deprecated" — is deliberate: a
    renderer shows a notice or shows nothing, and handing it a falsy-but-present
    object invites an empty banner on every non-deprecated pack.
    """
    deprecation = get_pack_deprecation(pack_id, version=version, as_of=as_of)
    if not deprecation.deprecated:
        return None
    return {
        "packId": deprecation.pack_id,
        "version": deprecation.version,
        "phase": deprecation.phase,
        "label": deprecation.label,
        "statusLabel": deprecation.status_label,
        "reason": deprecation.reason,
        "deprecatedOn": deprecation.deprecated_on,
        "graceEndsOn": deprecation.grace_ends_on,
        "daysRemaining": deprecation.days_remaining,
        "replacementPackId": deprecation.replacement_pack_id,
        "replacementLabel": deprecation.replacement_label,
        "summary": deprecation.summary,
    }


def deprecation_notices(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    as_of: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """``{pack_id: notice}`` for the DEPRECATED packs in a selection.

    Resolved ONCE per surface and threaded down, mirroring
    ``pack_certification.certification_badges``. Non-deprecated packs are absent, so
    ``notices.get(pack_id)`` is falsy for them — exactly what a renderer wants.

    An empty/omitted selection covers every registered pack.
    """
    notices: Dict[str, Dict[str, Any]] = {}
    for pack_id in normalize_pack_ids(list(pack_ids or [])) or _all_pack_ids():
        notice = deprecation_notice(pack_id, as_of=as_of)
        if notice is not None:
            notices[notice["packId"]] = notice
    return notices


def deprecated_pack_ids(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    as_of: Optional[date] = None,
) -> List[str]:
    """Ids of the deprecated packs in a selection, in selection order."""
    return list(deprecation_notices(pack_ids, as_of=as_of))


def deprecation_summary(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """JSON-serialisable deprecation snapshot for a selection.

    Persisted alongside a run (as the compatibility and certification summaries are)
    so a report states the deprecation position WHEN THE RUN EXECUTED rather than
    re-deriving it later from a registry that has moved on — and, just as importantly,
    so a run that had no deprecated packs can prove it evaluated them.
    """
    selection = list(normalize_pack_ids(list(pack_ids or []))) or _all_pack_ids()
    evaluated = _today(as_of)

    reports: List[PackDeprecation] = []
    seen: set = set()
    for pack_id in selection:
        report = get_pack_deprecation(pack_id, as_of=evaluated)
        if report.pack_id in seen:
            continue
        seen.add(report.pack_id)
        reports.append(report)

    return {
        "evaluatedOn": _iso(evaluated),
        "evaluated": [report.pack_id for report in reports],
        "deprecated": [report.pack_id for report in reports if report.deprecated],
        "inGrace": [report.pack_id for report in reports if report.in_grace],
        "graceExpired": [
            report.pack_id for report in reports if report.grace_expired
        ],
        "replacements": {
            report.pack_id: report.replacement_pack_id
            for report in reports
            if report.has_replacement
        },
        # Only the deprecated packs carry a full record: the evaluated list above
        # already proves coverage, and a snapshot on every run should not carry a
        # paragraph per pack that has nothing to say.
        "packs": [
            report.to_dict() for report in reports if report.deprecated
        ],
    }


def _all_pack_ids() -> List[str]:
    """Every registered pack id — the default set when no selection is given."""
    from .pack_config import PACK_REGISTRY

    return list(PACK_REGISTRY)


__all__ = [
    "DEPRECATION_STATUSES",
    "ISSUE_CONFLICTING_GRACE",
    "ISSUE_INVALID_GRACE_PERIOD",
    "ISSUE_INVALID_STATUS",
    "ISSUE_MISSING_DEPRECATED_ON",
    "ISSUE_MISSING_REASON",
    "ISSUE_SELF_REPLACEMENT",
    "ISSUE_UNKNOWN_REPLACEMENT",
    "ISSUE_UNKNOWN_VERSION_SCOPE",
    "ISSUE_UNREADABLE_DATE",
    "PHASE_ACTIVE",
    "PHASE_GRACE",
    "PHASE_GRACE_EXPIRED",
    "PHASE_LABELS",
    "PackDeprecation",
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
    "deprecated_pack_ids",
    "deprecation_notice",
    "deprecation_notices",
    "deprecation_summary",
    "get_pack_deprecation",
    "is_grace_expired",
    "is_pack_deprecated",
    "parse_deprecation_date",
    "replacement_pack_id",
]
