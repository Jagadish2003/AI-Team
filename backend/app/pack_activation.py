"""Pack activation gate (app layer) — 2.0-C1 T1 (AT-826).

The ONE place the API layer refuses an incompatible pack, so the two activation
edges cannot drift:

  * ``POST /api/stack-builder/launch``  (``routes_stack_builder_launch.py``)
  * ``POST /api/runs/{run_id}/compute`` (``routes_sprint4_t1.py``)

Both call :func:`gate_pack_activation`, which delegates the actual verdict to
``discovery.packs.pack_compatibility`` (the single source of truth for the rule)
and adds the two app-layer concerns the discovery layer must not own: refusal
telemetry, and the run-scoped compatibility snapshot the run record persists.

HTTP translation stays at the routes: this module raises
:class:`~discovery.packs.pack_compatibility.PackIncompatibleError`, whose
``str()`` is the user-facing reason naming every unmet requirement (AC1). The
routes turn that into a 409, mirroring the roadmap-connector connect guard.

The discovery runner re-asserts the same gate at the execution point, so a
CLI/direct caller that never touches an API edge cannot run an incompatible pack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from discovery.packs.pack_compatibility import (
    PackCompatibility,
    PackIncompatibleError,
    assert_selection_activatable,
)
from discovery.packs.pack_config import normalize_pack_ids
from discovery.packs.platform_capabilities import get_platform_version

logger = logging.getLogger(__name__)

#: 2.0-C4 T4 (AT-845). Imported eagerly (``pack_grace`` pulls in nothing heavy at
#: module load) so the exception below can name a retired pack without a deferred
#: import inside a constructor.
from .pack_grace import (  # noqa: E402
    EXCLUSION_REASON_GRACE_EXPIRED as _GRACE_EXPIRED_REASON,
)


def record_activation_refused(
    *,
    org_id: str,
    error: PackIncompatibleError,
    run_id: Optional[str] = None,
) -> None:
    """Emit the pack-activation refusal for run health / support.

    Observability only — a telemetry failure must never mask the refusal, which is
    already being raised to the caller. Carries pack ids, the NAMED unmet
    requirements, and the platform version; no credentials and no PII.
    """
    from .telemetry import record_event

    try:
        record_event(
            "pack.activation_refused",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_ids": error.pack_ids,
                "platform_version": get_platform_version(),
                "unmet": [
                    unmet.to_dict()
                    for report in error.reports
                    for unmet in report.unmet
                ],
                "reason": str(error),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.activation_refused telemetry failed (non-blocking)", exc_info=True
        )


def gate_pack_activation(
    *,
    org_id: str,
    pack_ids: Optional[Iterable[str]] = None,
    run_id: Optional[str] = None,
) -> List[PackCompatibility]:
    """Refuse an incompatible pack selection, or return its compatibility reports.

    Raises :class:`PackIncompatibleError` — naming EVERY incompatible pack in the
    selection — after recording the refusal. Returns the per-pack reports when the
    whole selection is activatable.
    """
    try:
        return assert_selection_activatable(pack_ids)
    except PackIncompatibleError as exc:
        logger.warning(
            "Pack activation refused for org=%s run=%s: %s", org_id, run_id, exc
        )
        record_activation_refused(org_id=org_id, error=exc, run_id=run_id)
        raise


def compatibility_snapshot(
    reports: Iterable[PackCompatibility],
) -> Dict[str, Dict[str, Any]]:
    """The run-scoped compatibility snapshot, keyed by pack id.

    Persisted at launch for the same reason ``packVersions`` is: a later registry
    or platform change must not rewrite what a historical run was launched
    against, so run health reports the verdict as evaluated then (AC5) instead of
    re-deriving it from a mutable registry.
    """
    return {report.pack_id: report.to_dict() for report in reports}


def certification_snapshot(
    pack_ids: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    """The run-scoped certification snapshot, keyed by pack id (2.0-C2 T3 / AT-833).

    Captured at ACTIVATION for the same reason as the compatibility snapshot: it
    records the level each pack held when the run was launched, so an audit of an old
    run can say what was true then rather than what is true now.

    It is deliberately NOT what the display surfaces read. A badge that no longer
    verifies must stop reading as Certified everywhere at once (2.0-C2 AC1), so
    findings, the packs panel, and the selection list all show the LIVE verified
    level; this snapshot is the audit record beside them.

    Fail-soft: certification is a label, and failing to resolve one must never fail
    a launch.
    """
    try:
        from discovery.packs.pack_certification import certification_badges

        return certification_badges(list(pack_ids))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not snapshot pack certification at activation", exc_info=True
        )
        return {}


def deprecation_snapshot(
    pack_ids: Iterable[str],
) -> Dict[str, Any]:
    """The run-scoped deprecation snapshot (2.0-C4 T2 / AT-843).

    Captured at ACTIVATION for the same reason as the compatibility and
    certification snapshots: it records where each activated pack stood in its
    deprecation lifecycle when the run was launched, so an audit of an old run can
    say what the customer was told AT THE TIME — including that they were told
    nothing, because ``evaluated`` lists every pack that was checked.

    It is deliberately NOT what the display surfaces read. Run configuration, run
    health, and findings all show the LIVE position, because "is this pack still
    supported, and until when" is a question about now; this snapshot is the audit
    record beside them.

    Fail-soft: a deprecation notice is a label, and failing to resolve one must
    never fail a launch.
    """
    try:
        from discovery.packs.pack_deprecation import deprecation_summary

        return deprecation_summary(list(pack_ids))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not snapshot pack deprecation at activation", exc_info=True
        )
        return {}


# ── 2.0-C1 T2 (AT-827) — disabled packs are excluded from future runs ─────────


@dataclass(frozen=True)
class ExcludedPack:
    """One pack dropped from a run's selection, with the reason it was dropped."""

    pack_id: str
    reason: str
    state: str

    def to_dict(self) -> Dict[str, str]:
        return {"packId": self.pack_id, "state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class ActivationDecision:
    """What a run will actually execute, and what was dropped on the way there."""

    activated: List[PackCompatibility]
    excluded: List[ExcludedPack]
    #: 2.0-C1 T3 (AT-828): ``{pack_id: pinned_version}`` for packs this org has
    #: rolled back. Only packs that are actually running appear here.
    pinned_versions: Dict[str, str] = field(default_factory=dict)
    #: ``{pack_id: archived_config_path}`` for the pinned packs — what the runner
    #: publishes to the per-run context so detectors read the pinned config.
    pinned_config_paths: Dict[str, str] = field(default_factory=dict)

    @property
    def activated_pack_ids(self) -> List[str]:
        return [report.pack_id for report in self.activated]

    @property
    def excluded_pack_ids(self) -> List[str]:
        return [item.pack_id for item in self.excluded]

    def effective_version(self, pack_id: str) -> Optional[str]:
        """The version ``pack_id`` will execute and be stamped with, if it is running."""
        if pack_id in self.pinned_versions:
            return self.pinned_versions[pack_id]
        for report in self.activated:
            if report.pack_id == pack_id:
                return report.pack_version
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activatedPackIds": self.activated_pack_ids,
            "excludedPacks": [item.to_dict() for item in self.excluded],
            "pinnedPackVersions": dict(self.pinned_versions),
        }


class AllPacksDisabledError(Exception):
    """Every selected pack is disabled, so there is nothing left to run.

    Excluding a disabled pack is normal; excluding ALL of them is not — a run with
    zero packs would produce nothing and report success. ``str(exc)`` names the
    disabled packs so the caller knows exactly what to re-enable or select.
    """

    def __init__(self, excluded: Sequence[ExcludedPack]) -> None:
        self.excluded: List[ExcludedPack] = list(excluded)
        names = ", ".join(item.pack_id for item in self.excluded)
        message = (
            f"Every selected pack is disabled for this organisation ({names}). "
            f"Re-enable a pack or select a different one before starting a run."
        )
        # 2.0-C4 T4 (AT-845): a pack retired by an expired grace period is NOT
        # something the customer can re-enable their way out of, so the generic
        # "re-enable a pack" advice would send them down a dead end. Name those packs
        # separately and point at the migration instead. The base sentence is left
        # intact for the ordinary disabled case.
        retired = [
            item.pack_id
            for item in self.excluded
            if item.reason == _GRACE_EXPIRED_REASON
        ]
        if retired:
            message += (
                f" {', '.join(retired)} reached the end of its deprecation grace "
                f"period and cannot be re-enabled — migrate to the replacement pack."
            )
        super().__init__(message)

    @property
    def pack_ids(self) -> List[str]:
        return [item.pack_id for item in self.excluded]


def record_packs_excluded(
    *,
    org_id: str,
    excluded: Sequence[ExcludedPack],
    run_id: Optional[str] = None,
) -> None:
    """Emit the disabled-pack exclusion so it is never silent.

    Observability only — a telemetry failure must not stop a run whose remaining
    packs are perfectly runnable.
    """
    if not excluded:
        return
    from .telemetry import record_event

    # One selection can now be excluded for two different reasons (customer disable
    # vs. an expired deprecation grace, AT-845), so the top-level reason is derived
    # from what is actually in the list rather than assumed. A homogeneous exclusion
    # still reports exactly the single reason it always did.
    reasons = sorted({item.reason for item in excluded})
    try:
        record_event(
            "pack.execution_skipped",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_ids": [item.pack_id for item in excluded],
                "reason": ",".join(reasons),
                "excluded": [item.to_dict() for item in excluded],
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.execution_skipped telemetry failed (non-blocking)", exc_info=True
        )


def resolve_activatable_packs(
    *,
    org_id: str,
    pack_ids: Optional[Iterable[str]] = None,
    run_id: Optional[str] = None,
) -> ActivationDecision:
    """The single activation resolution both API edges and the runner use.

    Five stages, in this order:

    0. **Record and enforce deprecation** (AT-846, then AT-845). First the org is
       recorded as having come under each selected pack's deprecation terms — once
       per set of declared terms, and never able to fail the activation. Then:
       **retire packs whose deprecation grace has ended** (AT-845). The expiry is
       DERIVED from the declared dates on every activation, and the pack is moved to
       safe-disabled through 2.0-C1's own path so its history stays intact. A pack
       still INSIDE its grace is untouched here and runs exactly as before — that
       negative is the promise a grace period makes.
    1. **Drop disabled packs** (AT-827). A disabled pack is intentionally turned
       off, so it is excluded rather than refused — and the exclusion is recorded
       loudly (run record, run health, telemetry), never silent.
    2. **Gate the remainder on compatibility** (AT-826). An incompatible pack is a
       configuration error and still REFUSES the activation with a 409.
    3. **Gate on the org's certification policy** (AT-834). A pack below the org's
       minimum certification level REFUSES the activation with a 409 naming the pack
       and the level it holds. Refuse rather than exclude, because "this selection
       is not allowed here" is an operator decision to resolve — quietly dropping
       the pack would leave a federal reviewer unable to tell a policy block from a
       pack that simply found nothing.
    4. **Apply version pins** (AT-828). A rolled-back pack contributes its pinned
       version and that version's archived config path, so the run executes AND is
       stamped with the pinned version.

    Disabled is evaluated FIRST on purpose: a pack the customer has already turned
    off must not be able to fail a run on compatibility grounds. It is not going to
    execute either way, so refusing the run over it would be noise.

    Compatibility is checked against the pack's CURRENT declaration, not the pinned
    version's: a pack's declared platform range lives in the registry and is a
    property of the pack, not of an archived config artifact. A rollback that would
    land on an unsupported platform is prevented at the rollback edge instead, where
    the operator can act on it.

    Raises :class:`AllPacksDisabledError` when the exclusion leaves nothing to run,
    :class:`PackIncompatibleError` when a pack that WOULD have run is incompatible,
    and :class:`~app.pack_certification_policy.PackCertificationPolicyViolation` when
    one is below the org's certification floor (or
    :class:`~app.pack_certification_policy.PackCertificationPolicyUnavailable` when
    that floor cannot be read — the policy gate fails CLOSED, unlike every other
    read here, because it is a security control). A pin that has become unservable (its artifact was removed) is
    NOT fatal — it degrades to the current version with a loud warning rather than
    failing every run for the org.
    """
    from .pack_state import (
        STATE_DISABLED as _DISABLED,
        disabled_pack_ids_safe,
        pinned_pack_versions_safe,
    )

    selection = normalize_pack_ids(list(pack_ids or []))
    if not selection:
        # An empty selection is the historical default-pack path. Resolve it to the
        # default pack id HERE so the disabled check covers it too — the runner
        # resolves the same default before it reaches this function, so without this
        # an API edge would pass a run whose default pack is disabled and the runner
        # would then fail it. The two must agree.
        from discovery.packs.pack_config import DEFAULT_PACK

        selection = [DEFAULT_PACK]

    # ── Stage 0: deprecation grace expiry (AT-845) ────────────────────────────
    # Runs BEFORE the disabled check because it FEEDS it: an expired pack is moved to
    # safe-disabled here, and stage 1's existing exclusion machinery then does the
    # work. That way retirement reuses the disable path end to end (run record, run
    # health, telemetry, the all-excluded guard) instead of growing a parallel one.
    #
    # The state read below happens AFTER this call so it sees the rows just written —
    # but the exclusion does not depend on that. `grace_expired` is derived, so a pack
    # whose disable could not be persisted is still excluded from this run.
    # 2.0-C4 T5 (AT-846): record that this org has come under each selected pack's
    # deprecation terms — the FIRST of the story's three transitions, and the only
    # one with no audit record before that task. Emitted BEFORE the retirement below
    # so the trail reads in the order the facts happened: told, then retired. Written
    # once per (org, pack, declared terms), and it can never fail an activation.
    from .pack_deprecation_audit import announce_deprecations
    from .pack_grace import enforce_grace_expiry

    announce_deprecations(org_id=org_id, pack_ids=selection, run_id=run_id)

    grace_expired = {
        item.pack_id
        for item in enforce_grace_expiry(
            org_id=org_id, pack_ids=selection, run_id=run_id
        )
    }

    disabled = disabled_pack_ids_safe(org_id)

    # A pack that is BOTH customer-disabled and grace-expired reports the expiry:
    # it is the reason the pack can never come back, so it is the one the operator
    # has to act on.
    excluded = [
        ExcludedPack(
            pack_id=pack_id,
            state=_DISABLED,
            reason=(
                _GRACE_EXPIRED_REASON
                if pack_id in grace_expired
                else "pack_disabled"
            ),
        )
        for pack_id in selection
        if pack_id in disabled or pack_id in grace_expired
    ]
    remaining = [
        pack_id
        for pack_id in selection
        if pack_id not in disabled and pack_id not in grace_expired
    ]

    # An explicit selection that is now entirely disabled cannot fall back to the
    # default pack — that would silently run something the caller never asked for.
    if selection and not remaining:
        logger.warning(
            "Every selected pack is disabled for org=%s run=%s: %s",
            org_id, run_id, [item.pack_id for item in excluded],
        )
        record_packs_excluded(org_id=org_id, excluded=excluded, run_id=run_id)
        raise AllPacksDisabledError(excluded)

    if excluded:
        logger.info(
            "Excluding disabled pack(s) from org=%s run=%s: %s",
            org_id, run_id, [item.pack_id for item in excluded],
        )
        record_packs_excluded(org_id=org_id, excluded=excluded, run_id=run_id)

    activated = gate_pack_activation(
        org_id=org_id, pack_ids=remaining, run_id=run_id
    )

    # ── Stage 3: certification policy (AT-834) ────────────────────────────────
    # Evaluated AFTER compatibility so a pack that cannot run here at all is
    # reported as incompatible rather than as a policy violation — the operator
    # needs the more fundamental reason first. Both refuse with a 409, so a caller
    # fixing one will see the other on the next attempt.
    from .pack_certification_policy import (
        PackCertificationPolicyViolation,
        assert_selection_permitted,
        record_policy_refusal,
    )

    try:
        assert_selection_permitted(
            org_id, [report.pack_id for report in activated]
        )
    except PackCertificationPolicyViolation as exc:
        logger.warning(
            "Pack activation refused by certification policy for org=%s run=%s: %s",
            org_id, run_id, exc,
        )
        record_policy_refusal(org_id=org_id, error=exc, run_id=run_id)
        raise

    # ── Stage 4: version pins (AT-828) ────────────────────────────────────────
    pinned_versions, pinned_config_paths = _resolve_version_pins(
        org_id=org_id,
        pack_ids=[report.pack_id for report in activated],
        run_id=run_id,
        pins=pinned_pack_versions_safe(org_id),
    )
    return ActivationDecision(
        activated=activated,
        excluded=excluded,
        pinned_versions=pinned_versions,
        pinned_config_paths=pinned_config_paths,
    )


def _resolve_version_pins(
    *,
    org_id: str,
    pack_ids: Sequence[str],
    run_id: Optional[str],
    pins: Dict[str, str],
) -> "tuple[Dict[str, str], Dict[str, str]]":
    """Resolve each running pack's version pin to (version, archived config path).

    A pin whose archived artifact is no longer declared cannot be honoured. Rather
    than failing every run for the org, that pin is SKIPPED with a loud warning and
    the pack runs — and is stamped with — its current version. The run therefore
    stays self-consistent: it is never stamped one version while behaving as
    another, which is the property AC3 actually protects. An operator sees the
    warning and either re-archives the artifact or clears the stale pin.
    """
    from discovery.packs.pack_config import (
        PackVersionUnavailable,
        resolve_pack_at_version,
    )

    versions: Dict[str, str] = {}
    config_paths: Dict[str, str] = {}
    for pack_id in pack_ids:
        wanted = pins.get(pack_id)
        if not wanted:
            continue
        try:
            resolved = resolve_pack_at_version(pack_id, wanted)
        except PackVersionUnavailable as exc:
            logger.warning(
                "Pinned version for pack %s (org=%s run=%s) can no longer be served, "
                "running the current version instead: %s",
                pack_id, org_id, run_id, exc,
            )
            _record_pin_unservable(
                org_id=org_id, pack_id=pack_id, version=wanted, run_id=run_id
            )
            continue
        pinned_version = resolved.get("pinnedVersion")
        if not pinned_version:
            # The pin equals the current version — nothing to override.
            continue
        versions[pack_id] = str(pinned_version)
        config_path = resolved.get("config_path")
        if config_path:
            config_paths[pack_id] = str(config_path)

    if versions:
        logger.info(
            "Run %s (org=%s) uses pinned pack version(s): %s", run_id, org_id, versions
        )
        _record_versions_pinned(org_id=org_id, versions=versions, run_id=run_id)
    return versions, config_paths


def _record_versions_pinned(
    *, org_id: str, versions: Dict[str, str], run_id: Optional[str]
) -> None:
    """Emit which pinned versions a run used, so a rollback is visible in run health."""
    from .telemetry import record_event

    try:
        record_event(
            "pack.version_pinned",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pinned_versions": dict(versions),
                "pack_ids": sorted(versions),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.version_pinned telemetry failed (non-blocking)", exc_info=True
        )


def _record_pin_unservable(
    *, org_id: str, pack_id: str, version: str, run_id: Optional[str]
) -> None:
    """Emit a pin that could not be honoured — a stale pin must not be silent."""
    from .telemetry import record_event

    try:
        record_event(
            "pack.version_pin_unservable",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_id": pack_id,
                "version": version,
                "reason": "archived_version_unavailable",
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.version_pin_unservable telemetry failed (non-blocking)",
            exc_info=True,
        )
