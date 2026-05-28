import os
import signal
import statistics
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import connect

# Configuration via env vars
BASELINE_WINDOW_DAYS = int(os.getenv("BASELINE_WINDOW_DAYS", "90"))
BASELINE_MIN_RUNS = int(os.getenv("BASELINE_MIN_RUNS", "3"))
BASELINE_JOB_INTERVAL_HOURS = int(os.getenv("BASELINE_JOB_INTERVAL_HOURS", "6"))


def calculate_baselines():
    con = connect()
    cur = con.cursor()

    # Get all org_id + signal_key combinations
    cur.execute("""
        SELECT org_id, signal_key, value, captured_at
        FROM signal_snapshots
        WHERE captured_at >= datetime('now', ? || ' days')
    """, (f"-{BASELINE_WINDOW_DAYS}",))
    rows = cur.fetchall()

    # Group by org_id + signal_key
    groups = {}
    for org_id, signal_key, value, captured_at in rows:
        key = (org_id, signal_key)
        if key not in groups:
            groups[key] = []
        groups[key].append(value)

    # Calculate and update baselines
    now = datetime.now(timezone.utc).isoformat()

    for (org_id, signal_key), values in groups.items():
        if len(values) >= BASELINE_MIN_RUNS:
            mean = statistics.mean(values)
            stddev = statistics.pstdev(values)  # population stddev NOT sample

            cur.execute("""
                UPDATE signal_snapshots
                SET
                    baseline_mean = ?,
                    baseline_stddev = ?,
                    baseline_window_days = ?,
                    baseline_calculated_at = ?
                WHERE org_id = ? AND signal_key = ?
            """, (mean, stddev, BASELINE_WINDOW_DAYS, now, org_id, signal_key))
        else:
            # Fewer than BASELINE_MIN_RUNS — leave baseline columns null
            cur.execute("""
                UPDATE signal_snapshots
                SET
                    baseline_mean = NULL,
                    baseline_stddev = NULL,
                    baseline_window_days = NULL,
                    baseline_calculated_at = NULL
                WHERE org_id = ? AND signal_key = ?
            """, (org_id, signal_key))

    con.commit()
    con.close()


# Scheduler setup
scheduler = BackgroundScheduler()


def start_scheduler():
    scheduler.add_job(
        calculate_baselines,
        trigger="interval",
        hours=BASELINE_JOB_INTERVAL_HOURS,
        next_run_time=datetime.now(timezone.utc)  # runs at startup too
    )
    scheduler.start()

    # Graceful SIGTERM shutdown
    def handle_sigterm(*args):
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, handle_sigterm)