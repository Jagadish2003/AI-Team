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
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from discovery.packs.pack_compatibility import (
    PackCompatibility,
    PackIncompatibleError,
    assert_selection_activatable,
)
from discovery.packs.pack_config import normalize_pack_ids
from discovery.packs.platform_capabilities import get_platform_version

logger = logging.getLogger(__name__)


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

    @property
    def activated_pack_ids(self) -> List[str]:
        return [report.pack_id for report in self.activated]

    @property
    def excluded_pack_ids(self) -> List[str]:
        return [item.pack_id for item in self.excluded]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activatedPackIds": self.activated_pack_ids,
            "excludedPacks": [item.to_dict() for item in self.excluded],
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
        super().__init__(
            f"Every selected pack is disabled for this organisation ({names}). "
            f"Re-enable a pack or select a different one before starting a run."
        )

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

    try:
        record_event(
            "pack.execution_skipped",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_ids": [item.pack_id for item in excluded],
                "reason": "pack_disabled",
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

    Two stages, in this order:

    1. **Drop disabled packs** (AT-827). A disabled pack is intentionally turned
       off, so it is excluded rather than refused — and the exclusion is recorded
       loudly (run record, run health, telemetry), never silent.
    2. **Gate the remainder on compatibility** (AT-826). An incompatible pack is a
       configuration error and still REFUSES the activation with a 409.

    Disabled is evaluated FIRST on purpose: a pack the customer has already turned
    off must not be able to fail a run on compatibility grounds. It is not going to
    execute either way, so refusing the run over it would be noise.

    Raises :class:`AllPacksDisabledError` when the exclusion leaves nothing to run,
    and :class:`PackIncompatibleError` when a pack that WOULD have run is
    incompatible.
    """
    from .pack_state import STATE_DISABLED as _DISABLED, disabled_pack_ids_safe

    selection = normalize_pack_ids(list(pack_ids or []))
    if not selection:
        # An empty selection is the historical default-pack path. Resolve it to the
        # default pack id HERE so the disabled check covers it too — the runner
        # resolves the same default before it reaches this function, so without this
        # an API edge would pass a run whose default pack is disabled and the runner
        # would then fail it. The two must agree.
        from discovery.packs.pack_config import DEFAULT_PACK

        selection = [DEFAULT_PACK]

    disabled = disabled_pack_ids_safe(org_id)

    excluded = [
        ExcludedPack(pack_id=pack_id, state=_DISABLED, reason="pack_disabled")
        for pack_id in selection
        if pack_id in disabled
    ]
    remaining = [pack_id for pack_id in selection if pack_id not in disabled]

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
    return ActivationDecision(activated=activated, excluded=excluded)
