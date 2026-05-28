"""Temporal signal storage API surface.

This module exposes temporal dataclasses, import-safe hooks, and lightweight
query helpers used by runner and future persistence code without creating
database-to-app circular imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

try:
    from app.db import connect
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.db import connect

try:
    from database.models.signal_snapshots import DetectorEvaluationLike, SignalSnapshot
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.database.models.signal_snapshots import (
        DetectorEvaluationLike,
        SignalSnapshot,
    )


BASELINE_MIN_RUNS = int(os.getenv("BASELINE_MIN_RUNS", "30"))


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


def _signal_snapshot_select() -> str:
    return """
        SELECT
            id,
            org_id,
            run_id,
            pack_id,
            detector_id,
            signal_key,
            metric_name,
            metric_value,
            metric_value AS value,
            threshold,
            fired,
            signal_source,
            captured_at,
            baseline_mean,
            baseline_stddev,
            baseline_window_days,
            baseline_calculated_at,
            baseline_calculated_at AS calculated_at
        FROM signal_snapshots
    """


def _fetch_dicts(cur) -> List[Dict[str, Any]]:
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def get_signal_history(
    org_id: str,
    detector_id: str,
    signal_key: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            _signal_snapshot_select()
            + """
            WHERE org_id = ? AND detector_id = ? AND signal_key = ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (org_id, detector_id, signal_key, limit),
        )
        return _fetch_dicts(cur)
    finally:
        con.close()


def get_baseline(
    org_id: str,
    detector_id: str,
) -> Optional[Dict[str, Any]]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT
                baseline_mean,
                baseline_stddev,
                baseline_window_days,
                baseline_calculated_at,
                baseline_calculated_at AS calculated_at,
                (
                    SELECT COUNT(*)
                    FROM signal_snapshots AS counted
                    WHERE counted.org_id = signal_snapshots.org_id
                      AND counted.detector_id = signal_snapshots.detector_id
                ) AS run_count
            FROM signal_snapshots
            WHERE org_id = ? AND detector_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (org_id, detector_id),
        )
        row = cur.fetchone()
        if not row:
            return None

        cols = [desc[0] for desc in cur.description]
        result = dict(zip(cols, row))
        result["insufficient_data"] = result["run_count"] < BASELINE_MIN_RUNS
        return result
    finally:
        con.close()


def get_run_signals(
    org_id: str,
    run_id: str,
) -> List[Dict[str, Any]]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            _signal_snapshot_select()
            + """
            WHERE run_id = ?
            ORDER BY captured_at DESC
            """,
            (run_id,),
        )
        results = _fetch_dicts(cur)
    finally:
        con.close()

    if not results:
        raise HTTPException(status_code=404, detail="run not found")
    if any(row["org_id"] != org_id for row in results):
        raise HTTPException(status_code=404, detail="run not found")

    return results


__all__ = [
    "DetectorEvaluation",
    "DetectorEvaluationLike",
    "SignalSnapshot",
    "get_baseline",
    "get_run_signals",
    "get_signal_history",
    "snapshot_signals",
]
