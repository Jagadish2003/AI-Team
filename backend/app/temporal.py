from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    from database.models.signal_snapshots import DetectorEvaluationLike, SignalSnapshot
except ModuleNotFoundError:
    try:
        from backend.database.models.signal_snapshots import (
            DetectorEvaluationLike,
            SignalSnapshot,
        )
    except ModuleNotFoundError:
        DetectorEvaluationLike = None  # type: ignore[assignment]
        SignalSnapshot = None  # type: ignore[assignment]


@dataclass
class DetectorEvaluation:
    """
    One detector's evaluation on one discovery run.

    Produced for every detector in the active pack, whether the detector
    fired or stayed below threshold.
    """

    detector_id: str
    detector_cls: type
    signal_source: str
    metric_value: float
    threshold: float
    fired: bool
    raw_evidence: dict


def snapshot_signals(
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: list[Any],
    all_evaluated: list[DetectorEvaluation],
    run_completed_at: datetime,
) -> None:
    """
    Import-safe Task 4 hook.

    Task 4 owns persistence. Task 3 exposes the callable now so runner imports
    from backend.app.temporal do not introduce circular dependencies.
    """

    return None


__all__ = ["DetectorEvaluation", "snapshot_signals"]
if SignalSnapshot is not None:
    __all__.extend(["DetectorEvaluationLike", "SignalSnapshot"])
