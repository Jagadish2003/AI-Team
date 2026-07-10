"""Async retrieval refresh worker (background job) — R18-B2 T3.

When a source artifact changes, the freshness subscriber (T1) marks its chunks
stale and queues the artifact on ``retrieval_refresh_queue``. This job is the
asynchronous driver that DRAINS that queue off the discovery-run path: on a fixed
interval it processes a bounded page of queued artifacts for every org through
``refresh.refresh_pending_all_orgs``, which for each artifact re-extracts the
current content, re-chunks it, hash-compares against the stored chunks, re-embeds
ONLY what actually changed (via the model gateway), swaps the chunk set in
atomically, and clears the stale flag as part of that same commit.

Doing the refresh here — never inline with a discovery run — is what keeps
re-embedding from ever blocking a run (Section 1's async requirement). It never
raises into anything: ``refresh_pending_all_orgs`` / ``refresh_pending_for_org``
isolate and log every failure, leaving un-refreshable artifacts queued (and their
chunks stale, so nothing outdated is served) for the next tick. Gated by
``AGENTIQ_DISABLE_BACKGROUND_JOBS`` like the other periodic jobs, and it reads org
ids straight from the queue — request/tenancy context is never touched here.
"""
from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

try:
    from app.retrieval import refresh
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.retrieval import refresh

logger = logging.getLogger(__name__)

# How often the worker drains a page of the refresh queue.
REFRESH_WORKER_INTERVAL_SECONDS = int(
    os.getenv("RETRIEVAL_REFRESH_JOB_INTERVAL_SECONDS", "120")
)
# Bound the artifacts refreshed for a single org in one tick so no tenant with a
# large backlog can monopolise a tick; 0 → None uses the module default page size.
_MAX_PER_ORG = int(os.getenv("RETRIEVAL_REFRESH_MAX_ARTIFACTS_PER_ORG", "128"))
REFRESH_WORKER_MAX_ARTIFACTS_PER_ORG = _MAX_PER_ORG if _MAX_PER_ORG > 0 else None
REFRESH_WORKER_JOB_ID = "retrieval_refresh_worker"

scheduler = BackgroundScheduler()
_sigterm_handler_registered = False


def run_refresh_worker() -> None:
    """Refresh a page of the queued backlog for every org (background entry point).

    Delegates to ``refresh_pending_all_orgs``, which is org-scoped per read and
    never raises. The extra guard here means even an unexpected error can never
    crash the scheduler thread — a refresh delay is only ever a delay, never a
    failure, and stale content is simply not served until refreshed.
    """
    try:
        results = refresh.refresh_pending_all_orgs(
            max_artifacts_per_org=REFRESH_WORKER_MAX_ARTIFACTS_PER_ORG
        )
    except Exception:  # noqa: BLE001 — never let a worker error crash the scheduler.
        logger.exception("refresh-worker: pass failed")
        return

    refreshed = sum(r.refreshed for r in results)
    reembedded = sum(r.reembedded_chunks for r in results)
    if refreshed:
        logger.info(
            "refresh-worker: refreshed %d artifact(s) (%d chunk(s) re-embedded) "
            "across %d org(s)",
            refreshed,
            reembedded,
            len(results),
        )


def _shutdown_scheduler(*_args) -> None:
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
        run_refresh_worker,
        trigger="interval",
        seconds=REFRESH_WORKER_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id=REFRESH_WORKER_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _register_sigterm_handler()

    return scheduler
