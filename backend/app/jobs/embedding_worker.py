"""Async retrieval embedding worker (background job) — R18-B1 T3.

The retrieval substrate indexes content BEFORE it is embedded: producers hand
extracted text + provenance to ``ingest_content()`` (T5), which writes chunks with
``embedding IS NULL``. Those chunks are embedded asynchronously — off the discovery
run path — so embedding lag never blocks a run and un-embedded content is simply
not-yet-retrievable, never a run failure (AC7). This job is that asynchronous
driver: on a fixed interval it drains the pending backlog for every org through
``embedder.embed_pending_all_orgs``, which embeds in bounded batches via the model
gateway ONLY and stamps each vector with the active model's identity + version
(AC8).

It never re-embeds already-embedded content (it only ever selects ``embedding IS
NULL`` rows) and never raises into a run: ``embed_pending_all_orgs`` /
``embed_pending_for_org`` isolate and log every failure, leaving un-embeddable
content pending for the next tick. Gated by ``AGENTIQ_DISABLE_BACKGROUND_JOBS``
like the other periodic jobs, and it reads org ids straight from the store —
request/tenancy context is never touched here.
"""
from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

try:
    from app.retrieval import embedder
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.retrieval import embedder

logger = logging.getLogger(__name__)

# How often the worker drains the pending backlog.
EMBEDDING_WORKER_INTERVAL_SECONDS = int(
    os.getenv("RETRIEVAL_EMBED_JOB_INTERVAL_SECONDS", "60")
)
# Bound the work done for a single org in one tick so no tenant with a large
# backlog can monopolise a tick; None (0 → None) drains the whole backlog.
_MAX_PER_ORG = int(os.getenv("RETRIEVAL_EMBED_MAX_CHUNKS_PER_ORG", "512"))
EMBEDDING_WORKER_MAX_CHUNKS_PER_ORG = _MAX_PER_ORG if _MAX_PER_ORG > 0 else None
EMBEDDING_WORKER_JOB_ID = "retrieval_embedding_worker"

scheduler = BackgroundScheduler()
_sigterm_handler_registered = False


def run_embedding_worker() -> None:
    """Embed the pending backlog for every org (background entry point).

    Delegates to ``embed_pending_all_orgs``, which is org-scoped per read and never
    raises. The extra guard here means even an unexpected error can never crash the
    scheduler thread — embedding lag is only ever a delay, never a failure (AC7).
    """
    try:
        results = embedder.embed_pending_all_orgs(
            max_chunks_per_org=EMBEDDING_WORKER_MAX_CHUNKS_PER_ORG
        )
    except Exception:  # noqa: BLE001 — never let a worker error crash the scheduler.
        logger.exception("embedding-worker: pass failed")
        return

    embedded = sum(r.embedded for r in results)
    if embedded:
        logger.info(
            "embedding-worker: embedded %d chunk(s) across %d org(s)",
            embedded,
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
        run_embedding_worker,
        trigger="interval",
        seconds=EMBEDDING_WORKER_INTERVAL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id=EMBEDDING_WORKER_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _register_sigterm_handler()

    return scheduler
