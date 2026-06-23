from __future__ import annotations

import os
import signal
import statistics
from datetime import datetime, timedelta, timezone

import psycopg2

from apscheduler.schedulers.background import BackgroundScheduler

try:
    from app.db import connect
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.db import connect


def _is_undefined_table(exc: Exception) -> bool:
    """True when *exc* is PostgreSQL's 'relation does not exist' error."""
    return "does not exist" in str(exc).lower() and "signal_snapshots" in str(exc)


BASELINE_WINDOW_DAYS = int(os.getenv("BASELINE_WINDOW_DAYS", "90"))
BASELINE_MIN_RUNS = int(os.getenv("BASELINE_MIN_RUNS", "3"))
BASELINE_JOB_INTERVAL_HOURS = int(os.getenv("BASELINE_JOB_INTERVAL_HOURS", "6"))
BASELINE_JOB_ID = "signal_snapshot_baseline_calculator"

scheduler = BackgroundScheduler()
_sigterm_handler_registered = False


def get_distinct_org_ids() -> list[str]:
    """Return all org_ids that have signal snapshot data.

    Background job use only — never calls get_current_org_id().
    org_id is read directly from the database, not from request context.
    """
    con = connect()
    try:
        cur = con.cursor()
        try:
            cur.execute("SELECT DISTINCT org_id FROM signal_snapshots")
        except psycopg2.Error as exc:
            con.rollback()
            if _is_undefined_table(exc):
                return []
            raise
        return [row[0] for row in cur.fetchall()]
    finally:
        con.close()


def calculate_baselines_for_org(org_id: str) -> None:
    """Compute and persist baselines for all signal keys belonging to org_id.

    Background job use only. org_id is passed explicitly — never derived
    from request context (get_current_org_id() must not be called here).
    """
    con = connect()
    try:
        cur = con.cursor()
        # Compute the window cutoff in Python rather than via a SQL date
        # function. ISO-8601 strings sort chronologically, so this comparison is
        # correct whether captured_at is a TIMESTAMP or TEXT column, and avoids
        # SQLite's datetime('now', ...) which has no PostgreSQL equivalent.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW_DAYS)
        ).isoformat()
        try:
            cur.execute(
                """
                SELECT signal_key, metric_value
                FROM signal_snapshots
                WHERE org_id = %s
                  AND captured_at >= %s
                """,
                (org_id, cutoff),
            )
        except psycopg2.Error as exc:
            con.rollback()
            if _is_undefined_table(exc):
                return
            raise
        rows = cur.fetchall()

        groups: dict[str, list[float]] = {}
        for signal_key, value in rows:
            groups.setdefault(signal_key, []).append(float(value))

        calculated_at = datetime.now(timezone.utc).isoformat()

        for signal_key, values in groups.items():
            if len(values) >= BASELINE_MIN_RUNS:
                baseline_mean = statistics.mean(values)
                baseline_stddev = statistics.pstdev(values)

                cur.execute(
                    """
                    UPDATE signal_snapshots
                    SET
                        baseline_mean = %s,
                        baseline_stddev = %s,
                        baseline_window_days = %s,
                        baseline_calculated_at = %s
                    WHERE org_id = %s AND signal_key = %s
                    """,
                    (
                        baseline_mean,
                        baseline_stddev,
                        BASELINE_WINDOW_DAYS,
                        calculated_at,
                        org_id,
                        signal_key,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE signal_snapshots
                    SET
                        baseline_mean = NULL,
                        baseline_stddev = NULL,
                        baseline_window_days = NULL,
                        baseline_calculated_at = NULL
                    WHERE org_id = %s AND signal_key = %s
                    """,
                    (org_id, signal_key),
                )

        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def run_baseline_job() -> None:
    """Entry point for the scheduled baseline calculation job.

    Background job — no request context. org_id is never read from tenancy
    context. get_current_org_id() must not be called here or in any function
    called from here.

    Fetches all org_ids with signal data explicitly from the database and
    processes each org in isolation, passing org_id through every call.
    """
    org_ids = get_distinct_org_ids()
    for org_id in org_ids:
        calculate_baselines_for_org(org_id)


def _shutdown_scheduler(*args) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _register_sigterm_handler() -> None:
    global _sigterm_handler_registered

    if not _sigterm_handler_registered:
        try:
            signal.signal(signal.SIGTERM, _shutdown_scheduler)
            _sigterm_handler_registered = True
        except ValueError:
            # TestClient may run lifespan hooks outside the main interpreter thread.
            pass


def start_scheduler() -> BackgroundScheduler:
    if scheduler.running:
        _register_sigterm_handler()
        return scheduler

    scheduler.add_job(
        run_baseline_job,
        trigger="interval",
        hours=BASELINE_JOB_INTERVAL_HOURS,
        next_run_time=datetime.now(timezone.utc),
        id=BASELINE_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _register_sigterm_handler()

    return scheduler