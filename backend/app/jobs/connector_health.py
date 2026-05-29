"""
Connector health check job for AgentIQ 2.0.

Runs every CONNECTOR_HEALTH_CHECK_INTERVAL_SECONDS (default: 900).
Iterates all connected connectors across all workspaces via
WorkspaceRepository and ConnectorRepository.
Writes one connector.health_check telemetry event per connector per run
via record_event() — the only approved telemetry write path.

Token status is evaluated via get_token_status(connector) which returns an
object with:
    .is_connected      bool   — True if credentials are present and valid
    .needs_refresh     bool   — True if token expires within the refresh window
    .expires_in_seconds int | None

Status mapping:
    needs_refresh=True              → "needs_refresh"
    is_connected=True               → "connected"
    otherwise                       → "needs_auth"

APScheduler lifecycle:
    start_health_check_job()  — call once from app startup (main.py lifespan)
    stop_health_check_job()   — call from app shutdown; uses wait=False to
                                 avoid blocking SIGTERM handling

See T1-S10-C / AT-90.
"""

from __future__ import annotations

import logging
import os
import signal
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.telemetry import record_event

logger = logging.getLogger("telemetry.jobs.connector_health")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HEALTH_CHECK_INTERVAL: int = int(
    os.environ.get("CONNECTOR_HEALTH_CHECK_INTERVAL_SECONDS", "900")
)

# ---------------------------------------------------------------------------
# Repository / service stubs
#
# These are the module-level names that contract tests patch via:
#   patch("app.jobs.connector_health.WorkspaceRepository")
#   patch("app.jobs.connector_health.ConnectorRepository")
#   patch("app.jobs.connector_health.get_token_status")
#
# Real implementations will be injected by future sprints (T1-S11+).
# The stubs allow the scheduler to run safely in environments where the
# repository layer is not yet wired (e.g. local dev without a full DB).
# ---------------------------------------------------------------------------

class WorkspaceRepository:
    """Stub workspace repository.  Returns empty list until wired in S11."""

    @staticmethod
    def get_all() -> list[Any]:
        """Return all active workspace records."""
        return []


class ConnectorRepository:
    """Stub connector repository.  Returns empty list until wired in S11."""

    @staticmethod
    def get_connected(workspace: Any = None) -> list[Any]:
        """Return connectors that are in a connected state for the workspace."""
        return []


def get_token_status(connector: Any) -> Any:
    """Return a token-status object for the given connector.

    Expected attributes on the returned object:
        .is_connected       bool
        .needs_refresh      bool
        .expires_in_seconds int | None

    Stub implementation always raises NotImplementedError — real
    implementation is injected by the connector-auth sprint.
    """
    raise NotImplementedError(
        "get_token_status is not yet wired; stub will be replaced in S11"
    )


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def _map_status(token_status: Any) -> str:
    """Map a token-status object to a ConnectorHealthPayload status string.

    Priority: needs_refresh > connected > needs_auth
    """
    if token_status.needs_refresh:
        return "needs_refresh"
    if token_status.is_connected:
        return "connected"
    return "needs_auth"


# ---------------------------------------------------------------------------
# Per-connector check
# ---------------------------------------------------------------------------

def _check_connector(connector: Any, org_id: str) -> None:
    """Evaluate one connector and write a telemetry event.

    Any exception from get_token_status or record_event is caught and
    logged at ERROR level — one failing connector must never abort checks
    for the remaining connectors in the same run.

    Args:
        connector: Connector ORM object with a .id attribute.
        org_id:    Workspace org_id that owns this connector.
    """
    try:
        t0 = time.perf_counter()
        token_status = get_token_status(connector)
        duration_ms = int((time.perf_counter() - t0) * 1000)

        status = _map_status(token_status)
        expiry: Optional[int] = getattr(token_status, "expires_in_seconds", None)
        connector_id = str(connector.id)

        record_event("connector.health_check", {
            "status":               status,
            "connector_id":         connector_id,
            "token_expiry_seconds": expiry,
            "check_duration_ms":    duration_ms,
            "org_id":               org_id,
            "source":               "connector_health_job",
            "success":              True,
        })

        logger.debug(
            "health_check org=%s connector=%s status=%s duration_ms=%d",
            org_id, connector_id, status, duration_ms,
        )

    except Exception:
        logger.error(
            "connector health check failed — connector=%s org=%s\n%s",
            getattr(connector, "id", "<unknown>"),
            org_id,
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Main job function
# ---------------------------------------------------------------------------

def run_connector_health_checks() -> None:
    """Iterate all workspaces × connected connectors and write telemetry.

    Called by APScheduler on the configured interval.  Also safe to invoke
    directly in tests or for a one-shot manual run.

    Job-level exceptions are caught and logged so APScheduler never sees an
    unhandled exception (which suppresses future runs in some configurations).
    """
    try:
        logger.info(
            "connector health check starting — %s",
            datetime.now(timezone.utc).isoformat(),
        )

        workspaces = WorkspaceRepository.get_all()
        if not workspaces:
            logger.warning("connector health check: no workspaces found — skipping")
            return

        for workspace in workspaces:
            connectors = ConnectorRepository.get_connected(workspace)
            for connector in connectors:
                _check_connector(connector=connector, org_id=workspace.org_id)

        logger.info(
            "connector health check complete — %s",
            datetime.now(timezone.utc).isoformat(),
        )

    except Exception:
        logger.error(
            "connector health check job error\n%s",
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# APScheduler lifecycle
# ---------------------------------------------------------------------------

_scheduler: Optional[BackgroundScheduler] = None


def start_health_check_job() -> None:
    """Start the APScheduler background job.  Call once from app startup.

    Registers the job to fire immediately (next_run_time=now) then every
    HEALTH_CHECK_INTERVAL seconds.  Idempotent: a second call while the
    scheduler is already running logs a warning and returns.

    Also installs a SIGTERM handler that calls stop_health_check_job() so
    the process shuts down cleanly under container orchestrators.

    Example (main.py lifespan)::

        from app.jobs.connector_health import start_health_check_job
        start_health_check_job()
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.warning(
            "connector health check scheduler already running — skipping start"
        )
        return

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=run_connector_health_checks,
        trigger="interval",
        seconds=HEALTH_CHECK_INTERVAL,
        id="connector_health_check",
        replace_existing=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc),
    )
    _scheduler.start()

    # signal.signal() is only permitted in the main thread.
    # Skip in test/worker-thread contexts (e.g. pytest via TestClient).
    import threading
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _sigterm_handler)

    logger.info(
        "connector health check scheduler started (interval=%ds)",
        HEALTH_CHECK_INTERVAL,
    )


def stop_health_check_job() -> None:
    """Stop the scheduler gracefully.  Safe to call if not running.

    Uses wait=False so the call returns immediately and does not block
    in-flight SIGTERM handling.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("connector health check scheduler stopped")
    _scheduler = None


def _sigterm_handler(signum: int, frame: object) -> None:
    logger.info("SIGTERM received — stopping connector health check scheduler")
    stop_health_check_job()
