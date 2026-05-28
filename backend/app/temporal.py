TP_T3_S10-A-TEMPORAL_STORAGE
from typing import Any, Dict, List, Optional
from fastapi import HTTPException
from app.db import connect

BASELINE_MIN_RUNS = 30


def get_signal_history(
    org_id: str,
    detector_id: str,
    signal_key: str,
    limit: int = 100
) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("""
        SELECT
            id, org_id, detector_id, signal_key, run_id,
            value, baseline_mean, baseline_stddev,
            baseline_window_days, calculated_at, run_count,
            captured_at
        FROM signal_snapshots
        WHERE org_id = ? AND detector_id = ? AND signal_key = ?
        ORDER BY captured_at DESC
        LIMIT ?
    """, (org_id, detector_id, signal_key, limit))
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    con.close()
    return [dict(zip(cols, row)) for row in rows]


def get_baseline(
    org_id: str,
    detector_id: str
) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("""
        SELECT
            baseline_mean, baseline_stddev, baseline_window_days,
            calculated_at, run_count
        FROM signal_snapshots
        WHERE org_id = ? AND detector_id = ?
        ORDER BY captured_at DESC
        LIMIT 1
    """, (org_id, detector_id))
    row = cur.fetchone()
    cols = [desc[0] for desc in cur.description]
    con.close()
    if not row:
        return None
    result = dict(zip(cols, row))
    result["insufficient_data"] = result["run_count"] < BASELINE_MIN_RUNS
    return result


def get_run_signals(
    org_id: str,
    run_id: str
) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("""
        SELECT
            id, org_id, detector_id, signal_key, run_id,
            value, baseline_mean, baseline_stddev,
            baseline_window_days, calculated_at, run_count,
            captured_at
        FROM signal_snapshots
        WHERE run_id = ?
    """, (run_id,))
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    con.close()

    if not rows:
        raise HTTPException(status_code=404, detail="run not found")

    results = [dict(zip(cols, row)) for row in rows]

    if any(r["org_id"] != org_id for r in results):
        raise HTTPException(status_code=404, detail="run not found")

    return results

"""Temporal signal storage API surface.

This module exposes temporal dataclasses and import-safe hooks used by runner
and future persistence code without creating database-to-app circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    from database.models.signal_snapshots import DetectorEvaluationLike, SignalSnapshot
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.database.models.signal_snapshots import (
        DetectorEvaluationLike,
        SignalSnapshot,
    )


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
    raw_evidence: dict[str, Any]


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


__all__ = [
    "DetectorEvaluation",
    "DetectorEvaluationLike",
    "SignalSnapshot",
    "snapshot_signals",
]
AgentIQ_Platform
