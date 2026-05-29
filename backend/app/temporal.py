"""Temporal signal storage API surface.

This module exposes temporal dataclasses and import-safe hooks used by runner
and future persistence code without creating database-to-app circular imports.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from database.models.signal_snapshots import DetectorEvaluationLike, SignalSnapshot
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.database.models.signal_snapshots import (
        DetectorEvaluationLike,
        SignalSnapshot,
    )

try:
    from app.telemetry import record_event
except ModuleNotFoundError:  # pragma: no cover - repo-root execution
    from backend.app.telemetry import record_event

logger = logging.getLogger(__name__)


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


def _db_path() -> Path:
    return Path(os.getenv("DB_PATH", "database/dev.db"))


def _isoformat(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _persist_snapshots(snapshots: list[SignalSnapshot]) -> int:
    """Insert snapshots into signal_snapshots. Returns count committed.

    Returns 0 if the table is absent or the DB is inaccessible so that
    the caller can still emit telemetry for the snapshots it built.
    """
    if not snapshots:
        return 0

    rows = []
    for s in snapshots:
        r = s.to_db_row()
        r["captured_at"] = _isoformat(r["captured_at"])
        r["baseline_calculated_at"] = _isoformat(r["baseline_calculated_at"])
        r["fired"] = int(r["fired"])
        rows.append(r)

    try:
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db), timeout=30.0, check_same_thread=False)
        try:
            con.executemany(
                """
                INSERT OR IGNORE INTO signal_snapshots (
                    id, org_id, run_id, pack_id, detector_id, signal_key,
                    metric_name, metric_value, threshold, fired, signal_source,
                    captured_at, baseline_mean, baseline_stddev,
                    baseline_window_days, baseline_calculated_at
                ) VALUES (
                    :id, :org_id, :run_id, :pack_id, :detector_id, :signal_key,
                    :metric_name, :metric_value, :threshold, :fired, :signal_source,
                    :captured_at, :baseline_mean, :baseline_stddev,
                    :baseline_window_days, :baseline_calculated_at
                )
                """,
                rows,
            )
            con.commit()
            return len(snapshots)
        finally:
            con.close()
    except Exception as exc:
        logger.warning("Signal snapshot persistence failed (non-blocking): %s", exc)
        return 0


def snapshot_signals(
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: list[Any],
    all_evaluated: list[DetectorEvaluation],
    run_completed_at: datetime,
) -> None:
    """Persist one SignalSnapshot row per metric per evaluation, then emit telemetry.

    Both persistence and the telemetry call are non-blocking: failures are
    logged as warnings and never propagate to the caller.
    """
    all_snapshots: list[SignalSnapshot] = []
    for evaluation in all_evaluated:
        try:
            primary = SignalSnapshot.from_primary_metric(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                evaluation=evaluation,
                run_completed_at=run_completed_at,
            )
            all_snapshots.append(primary)
            signal_metrics = SignalSnapshot.from_signal_metrics(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                evaluation=evaluation,
                run_completed_at=run_completed_at,
            )
            all_snapshots.extend(signal_metrics)
        except Exception as exc:
            logger.warning(
                "Signal snapshot build failed for %s (non-blocking): %s",
                getattr(evaluation, "detector_id", "unknown"),
                exc,
            )

    _persist_snapshots(all_snapshots)

    for snapshot in all_snapshots:
        try:
            record_event(
                "run.signal_snapshot",
                {
                    "metric_key": snapshot.signal_key,
                    "value": snapshot.metric_value,
                    "baseline": snapshot.baseline_mean,
                },
            )
        except Exception as exc:
            logger.warning("Signal snapshot telemetry failed (non-blocking): %s", exc)


__all__ = [
    "DetectorEvaluation",
    "DetectorEvaluationLike",
    "SignalSnapshot",
    "snapshot_signals",
]
