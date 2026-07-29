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
from typing import Any, Dict, Iterable, List, Optional

from discovery.packs.pack_compatibility import (
    PackCompatibility,
    PackIncompatibleError,
    assert_selection_activatable,
)
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
