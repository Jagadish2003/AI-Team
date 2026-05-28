"""Temporal signal storage API surface.

This module exposes temporal dataclasses, import-safe hooks, and lightweight
query helpers used by runner and future persistence code without creating
database-to-app circular imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

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


class RunSignalSnapshotPayload(TypedDict):
    pack_id: str
    signal_count: int
    detector_count: int
    fired_count: int
    below_threshold: int


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


def record_event(
    *,
    org_id: str,
    event_type: str,
    source: str,
    run_id: str,
    success: bool,
    count: int,
    payload: RunSignalSnapshotPayload,
) -> None:
    """Import-safe bridge for T1-S10-C telemetry when it is available."""

    try:
        try:
            from app.telemetry import record_event as telemetry_record_event
        except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
            from backend.app.telemetry import record_event as telemetry_record_event
    except ModuleNotFoundError:
        return None

    telemetry_record_event(
        org_id=org_id,
        event_type=event_type,
        source=source,
        run_id=run_id,
        success=success,
        count=count,
        payload=payload,
    )


def _build_signal_snapshots(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    all_evaluated: list[DetectorEvaluation],
    run_completed_at: datetime,
) -> list[SignalSnapshot]:
    snapshots: list[SignalSnapshot] = []
    for evaluation in all_evaluated:
        snapshots.append(
            SignalSnapshot.from_primary_metric(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                evaluation=evaluation,
                run_completed_at=run_completed_at,
            )
        )
        snapshots.extend(
            SignalSnapshot.from_signal_metrics(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                evaluation=evaluation,
                run_completed_at=run_completed_at,
            )
        )
    return snapshots


def _insert_signal_snapshots(snapshots: list[SignalSnapshot]) -> None:
    if not snapshots:
        return None

    columns = [
        "id",
        "org_id",
        "run_id",
        "pack_id",
        "detector_id",
        "signal_key",
        "metric_name",
        "metric_value",
        "threshold",
        "fired",
        "signal_source",
        "captured_at",
        "baseline_mean",
        "baseline_stddev",
        "baseline_window_days",
        "baseline_calculated_at",
    ]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"""
        INSERT INTO signal_snapshots ({", ".join(columns)})
        VALUES ({placeholders})
    """

    rows = [
        tuple(snapshot.to_db_row()[column] for column in columns)
        for snapshot in snapshots
    ]
    con = connect()
    try:
        cur = con.cursor()
        cur.executemany(sql, rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def snapshot_signals(
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: list[Any],
    all_evaluated: list[DetectorEvaluation],
    run_completed_at: datetime,
) -> None:
    """
    Writes temporal signal snapshots for all evaluated detectors.

    This function is non-blocking by contract: storage or telemetry failures
    are swallowed so discovery runs can complete even when temporal capture is
    unavailable.
    """
    try:
        snapshots = _build_signal_snapshots(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            all_evaluated=all_evaluated,
            run_completed_at=run_completed_at,
        )
        _insert_signal_snapshots(snapshots)

        payload = RunSignalSnapshotPayload(
            pack_id=pack_id,
            signal_count=len(snapshots),
            detector_count=len(
                {evaluation.detector_id for evaluation in all_evaluated}
            ),
            fired_count=len(
                {
                    evaluation.detector_id
                    for evaluation in all_evaluated
                    if evaluation.fired
                }
            ),
            below_threshold=len(
                {
                    evaluation.detector_id
                    for evaluation in all_evaluated
                    if not evaluation.fired
                }
            ),
        )
        try:
            record_event(
                org_id=org_id,
                event_type="run.signal_snapshot",
                source="temporal_engine",
                run_id=run_id,
                success=True,
                count=len(snapshots),
                payload=payload,
            )
        except Exception:
            return None
    except Exception:
        return None

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
    "RunSignalSnapshotPayload",
    "SignalSnapshot",
    "get_baseline",
    "get_run_signals",
    "get_signal_history",
    "snapshot_signals",
]
