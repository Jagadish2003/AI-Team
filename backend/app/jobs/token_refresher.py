"""Proactive OAuth token refresher (background job).

OAuth access tokens are short-lived (ServiceNow ~30 min, Salesforce / Jira ~1 h)
but the refresh token stored in the credential vault is long-lived. ``get_token``
refreshes lazily — only when something reads a token inside the expiry window —
so a connected source that is not used for a while can sit with an expired access
token until the next read. This job closes that gap: on a fixed interval it
renews every vault token that is due to expire soon, using its refresh token,
so connectors stay live without the user ever re-running the OAuth flow.

It NEVER forces a re-auth: a connector with no refresh token, or whose refresh
genuinely fails, is simply left for the user to reconnect (``get_token`` marks
``refresh_failed`` and the token-status endpoint then reports it). Each connector
is refreshed in isolation so one failure never blocks the others, and the whole
job is gated by ``AGENTIQ_DISABLE_BACKGROUND_JOBS`` like the other periodic jobs.

Background job only — org_id/connector_id are read straight from the database;
request/tenancy context (get_current_org_id) is never touched here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import psycopg2
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from app.db import connect
    from app.auth.models import ConnectorNotAuthenticatedError
    from app.auth.vault import get_token
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.db import connect
    from backend.app.auth.models import ConnectorNotAuthenticatedError
    from backend.app.auth.vault import get_token

logger = logging.getLogger(__name__)

# How often the job runs. Must be comfortably smaller than TOKEN_REFRESH_AHEAD so
# a token entering the lookahead window is always caught before it expires.
TOKEN_REFRESH_JOB_INTERVAL_MINUTES = int(
    os.getenv("TOKEN_REFRESH_JOB_INTERVAL_MINUTES", "10")
)
# Refresh any token expiring within this many seconds (default 15 min > interval).
TOKEN_REFRESH_AHEAD_SECONDS = int(os.getenv("TOKEN_REFRESH_AHEAD_SECONDS", "900"))
TOKEN_REFRESH_JOB_ID = "oauth_token_refresher"

scheduler = BackgroundScheduler()
_sigterm_handler_registered = False


def _is_undefined_credentials_table(exc: Exception) -> bool:
    """True when *exc* is PostgreSQL's 'relation does not exist' for credentials."""
    msg = str(exc).lower()
    return "does not exist" in msg and "credentials" in msg


def get_refreshable_credentials(ahead_seconds: int) -> List[Tuple[str, str]]:
    """Return (org_id, connector_id) for tokens due to expire within ``ahead_seconds``.

    Only rows that can actually be refreshed without user action are returned:
    a refresh token is present and the last refresh did not fail. expires_at is an
    ISO-8601 string, which sorts chronologically, so a string comparison against
    the cutoff is correct regardless of the column's storage type (matches the
    baseline job's convention). Never raises for a missing table — returns [].
    """
    cutoff = (
        datetime.now(timezone.utc) + timedelta(seconds=ahead_seconds)
    ).isoformat()
    con = connect()
    try:
        cur = con.cursor()
        try:
            cur.execute(
                """
                SELECT org_id, connector_id
                FROM credentials
                WHERE is_deleted = FALSE
                  AND refresh_token IS NOT NULL
                  AND COALESCE(refresh_failed, 0) = 0
                  AND expires_at <= %s
                """,
                (cutoff,),
            )
        except psycopg2.Error as exc:
            con.rollback()
            if _is_undefined_credentials_table(exc):
                return []
            raise
        return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        con.close()


def run_token_refresh_job() -> None:
    """Renew every vault token due to expire within the lookahead window.

    Background entry point. Each connector is refreshed independently; a failure
    (or an unrefreshable token) is logged and skipped so it can never block the
    rest. ``get_token`` with the widened ``min_validity_seconds`` performs the
    actual refresh-and-store; it marks ``refresh_failed`` on a genuine failure so
    the connector surfaces as needing reconnect rather than being retried forever.
    """
    try:
        due = get_refreshable_credentials(TOKEN_REFRESH_AHEAD_SECONDS)
    except Exception:  # noqa: BLE001 — never let a scan error crash the scheduler.
        logger.exception("token-refresher: failed to scan credentials")
        return

    if not due:
        return

    refreshed = 0
    for org_id, connector_id in due:
        try:
            asyncio.run(
                get_token(
                    org_id,
                    connector_id,
                    min_validity_seconds=TOKEN_REFRESH_AHEAD_SECONDS,
                )
            )
            refreshed += 1
        except ConnectorNotAuthenticatedError:
            # No usable refresh, or the provider rejected it — get_token has marked
            # refresh_failed; leave it for the user to reconnect.
            logger.info(
                "token-refresher: %s/%s could not be refreshed (needs reconnect)",
                org_id,
                connector_id,
            )
        except Exception:  # noqa: BLE001 — isolate per-connector failures.
            logger.warning(
                "token-refresher: unexpected error refreshing %s/%s",
                org_id,
                connector_id,
                exc_info=True,
            )

    logger.info(
        "token-refresher: renewed %d/%d token(s) due within %ds",
        refreshed,
        len(due),
        TOKEN_REFRESH_AHEAD_SECONDS,
    )


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
        run_token_refresh_job,
        trigger="interval",
        minutes=TOKEN_REFRESH_JOB_INTERVAL_MINUTES,
        next_run_time=datetime.now(timezone.utc),
        id=TOKEN_REFRESH_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _register_sigterm_handler()

    return scheduler
