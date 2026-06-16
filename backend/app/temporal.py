"""Temporal signal storage API surface.

This module exposes temporal dataclasses, import-safe hooks, and lightweight
query helpers used by runner and future persistence code without creating
database-to-app circular imports.
"""

from __future__ import annotations

import logging
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

try:
    from app.telemetry import RunSignalSnapshotPayload, record_event
except ModuleNotFoundError:  # pragma: no cover - repo-root execution
    from backend.app.telemetry import RunSignalSnapshotPayload, record_event


logger = logging.getLogger(__name__)

BASELINE_MIN_RUNS = int(os.getenv("BASELINE_MIN_RUNS", "3"))
PRIMARY_METRIC_NAME = "metric_value"


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
        try:
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
        except Exception as exc:
            logger.warning(
                "Signal snapshot build failed for %s (non-blocking): %s",
                getattr(evaluation, "detector_id", "unknown"),
                exc,
            )
    return snapshots


def _insert_signal_snapshots(snapshots: list[SignalSnapshot]) -> int:
    if not snapshots:
        return 0

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
    placeholders = ", ".join("%s" for _ in columns)
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
        return len(snapshots)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _build_signal_snapshot_telemetry(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    signal_count: int,
    all_evaluated: list[DetectorEvaluation],
) -> RunSignalSnapshotPayload:
    return RunSignalSnapshotPayload(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        signal_count=signal_count,
        detector_count=len(all_evaluated),
        fired_count=sum(1 for evaluation in all_evaluated if evaluation.fired),
        below_threshold=sum(1 for evaluation in all_evaluated if not evaluation.fired),
    )


def ensure_signal_snapshots_table() -> None:
    """Create the signal_snapshots table + indexes if they do not exist.

    Schema mirrors migrations/versions/0002_create_signal_snapshots.py exactly.
    All statements are IF NOT EXISTS, so this is a no-op when the alembic
    migration already created the table (contract tests, production). It exists
    so a fresh dev.db — created by seed_loader.py, which does not run alembic —
    still has the table the temporal/baseline enrichment reads from. Without it,
    every signal-history query raises "no such table" and the broad try/except
    in temporal_enrichment swallows it, hiding the Baseline Context panel.
    """
    con = connect()
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_snapshots (
                id                   VARCHAR(36)  NOT NULL PRIMARY KEY,
                org_id               VARCHAR(64)  NOT NULL,
                run_id               VARCHAR(64)  NOT NULL,
                pack_id              VARCHAR(64)  NOT NULL,
                detector_id          VARCHAR(128) NOT NULL,
                signal_key           VARCHAR(256) NOT NULL,
                metric_name          VARCHAR(128) NOT NULL,
                metric_value         DOUBLE PRECISION NOT NULL,
                threshold            DOUBLE PRECISION,
                fired                BOOLEAN      NOT NULL,
                signal_source        VARCHAR(64)  NOT NULL,
                captured_at          TIMESTAMP    NOT NULL,
                baseline_mean        DOUBLE PRECISION,
                baseline_stddev      DOUBLE PRECISION,
                baseline_window_days INTEGER,
                baseline_calculated_at TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ss_org_signal_time
                ON signal_snapshots (org_id, signal_key, captured_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ss_org_run
                ON signal_snapshots (org_id, run_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ss_org_detector
                ON signal_snapshots (org_id, detector_id, captured_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ss_baseline_stale
                ON signal_snapshots (baseline_calculated_at)
        """)
        con.commit()
    except Exception as exc:
        con.rollback()
        logger.warning("ensure_signal_snapshots_table failed (non-blocking): %s", exc)
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
        signal_count = _insert_signal_snapshots(snapshots)
    except Exception as exc:
        logger.warning("Signal snapshot persistence failed (non-blocking): %s", exc)
        return None

    payload = _build_signal_snapshot_telemetry(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        signal_count=signal_count,
        all_evaluated=all_evaluated,
    )
    try:
        record_event("run.signal_snapshot", payload)
    except Exception as exc:
        logger.warning("Signal snapshot telemetry failed (non-blocking): %s", exc)

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
    signal_filter = (signal_key or "").strip() or PRIMARY_METRIC_NAME
    if "::" in signal_filter:
        signal_clause = "signal_key = %s"
        signal_value = signal_filter
    else:
        signal_clause = "metric_name = %s"
        signal_value = signal_filter

    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            _signal_snapshot_select()
            + f"""
            WHERE org_id = %s AND detector_id = %s AND {signal_clause}
            ORDER BY captured_at DESC
            LIMIT %s
            """,
            (org_id, detector_id, signal_value, limit),
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
                signal_key,
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
                      AND counted.signal_key = signal_snapshots.signal_key
                ) AS run_count
            FROM signal_snapshots
            WHERE org_id = %s AND detector_id = %s AND metric_name = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (org_id, detector_id, PRIMARY_METRIC_NAME),
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
            WHERE org_id = %s AND run_id = %s
            ORDER BY captured_at DESC
            """,
            (org_id, run_id),
        )
        results = _fetch_dicts(cur)
    finally:
        con.close()

    if not results:
        raise HTTPException(status_code=404, detail="run not found")

    return results


__all__ = [
    "DetectorEvaluation",
    "DetectorEvaluationLike",
    "RunSignalSnapshotPayload",
    "SignalSnapshot",
    "ensure_signal_snapshots_table",
    "get_baseline",
    "get_run_signals",
    "get_signal_history",
    "snapshot_signals",
]
