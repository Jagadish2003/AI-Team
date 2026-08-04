"""Audit and telemetry emission for ranking-adjustment governance.

Kept outside the ``learning_*`` modules on purpose: the learning layer's source
guards forbid importing telemetry so it can never learn from engagement data.
This helper only emits state-change facts after the adjustment store has already
committed.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

TELEMETRY_RANKING_ADJUSTMENT_CHANGED = "ranking_adjustment.changed"


def emit_ranking_adjustment_changed(
    *,
    org_id: str,
    actor_id: str,
    change_kind: str,
    previous_state: Sequence[Mapping[str, Any]],
    current_state: Sequence[Mapping[str, Any]],
    groups_changed: int,
    opportunities_affected: int,
    config_version: str | None,
    changed_at: str,
    reason: str | None = None,
) -> None:
    """Emit the governance trail for one adjustment-state change.

    Best-effort by design. The adjustment write has already committed before
    this function runs, so audit/telemetry failures are logged and swallowed.
    """
    payload = {
        "change_kind": change_kind,
        "target": "ranking_adjustments",
        "previous_state": [dict(row) for row in previous_state],
        "current_state": [dict(row) for row in current_state],
        "groups_changed": int(groups_changed),
        "opportunities_affected": int(opportunities_affected),
        "config_version": config_version,
        "changed_at": changed_at,
    }
    if reason:
        payload["reason"] = reason

    try:
        from .middleware.audit import RANKING_ADJUSTMENT_CHANGED, log_event

        log_event(
            RANKING_ADJUSTMENT_CHANGED,
            org_id=org_id,
            user_id=actor_id,
            **payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ranking-adjustment audit write failed: %s", exc)

    try:
        from .telemetry import record_event

        record_event(
            TELEMETRY_RANKING_ADJUSTMENT_CHANGED,
            {
                "org_id": org_id,
                "actor_id": actor_id,
                **payload,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ranking-adjustment telemetry emit failed: %s", exc)


__all__ = [
    "TELEMETRY_RANKING_ADJUSTMENT_CHANGED",
    "emit_ranking_adjustment_changed",
]
