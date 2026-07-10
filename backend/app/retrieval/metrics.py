"""Freshness metrics — retrieval lag made queryable, per org (R18-B2 T6).

Section 1's last rule is "**Lag is visible**": freshness lag — events pending,
chunks stale, backfill progress — is exposed as metrics, surfaced on the
run-health dashboard in Sprint 2. Staleness is allowed to exist (it happens
naturally while the async workers are running); it is never allowed to be
invisible. This module computes that visibility per org (AC7):

* ``pending_change_events``  — artifacts sitting in the refresh queue awaiting
  the async refresh worker (T3). The "events pending" component of lag.
* ``failed_refreshes``       — queue rows parked as ``failed`` after exhausting
  their retry budget. Their chunks stay stale until an operator investigates,
  so they must be counted, not hidden.
* ``stale_chunks``           — chunks marked stale by change events (T1) and not
  yet swapped by the refresh worker. Excluded from default retrieval (T4), so
  this is content currently invisible to findings.
* ``pending_embeddings``     — chunks indexed but not yet embedded (R18-B1 AC7's
  "not yet retrievable" set — the embedding worker's backlog).
* ``backfill``               — model-repin convergence (T5): how many embedded
  vectors are stamped by the ACTIVE model vs still awaiting re-embedding, with a
  progress ratio the dashboard can render directly.

Everything is computed from the same org-partitioned primitives the workers run
on (``refresh_queue``, ``store``, the gateway's active-model identity), so the
numbers are the workers' actual view, not a parallel bookkeeping that can drift.

Failure posture — deliberately DIFFERENT from the run-path modules: this module
does NOT swallow errors into zeros. A metrics read that degrades to "0 stale,
0 pending" while the database is down would report perfect health at the exact
moment nothing can be trusted — the one lie a freshness metric must never tell.
A failing read raises, and the route surfaces it as an error the operator can
see.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

try:  # Repo-root import style (tests add both roots to sys.path).
    from backend.app.retrieval import embedder, refresh_queue, store
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.retrieval import embedder, refresh_queue, store

__all__ = ["freshness_metrics", "backfill_progress"]


def backfill_progress(org_id: str) -> Dict[str, Any]:
    """Model-backfill convergence for one org (the T5 repin, measured).

    ``embedded_total``    embedded vectors in the org's partition.
    ``on_active_model``   vectors stamped by the ACTIVE (identity, version) pair —
                          the only ones retrieval will return (AC5/AC8).
    ``awaiting_backfill`` vectors stamped by any other model — invalidated by the
                          repin, invisible to retrieval, queued for the managed
                          backfill to re-embed.
    ``progress``          on_active_model / embedded_total, rounded to 4 places.
                          1.0 when nothing is embedded (an empty partition is
                          converged, not broken). When no active model is
                          resolvable (gateway degraded), nothing counts as
                          current — progress honestly reads 0.0, matching the
                          backfill worker, which parks in the same situation.
    ``complete``          True when no embedded vector awaits backfill.

    The active model pair is resolved live from the gateway — the same pair the
    backfill stamps and ``retrieve()`` filters by — so this metric and the
    worker can never disagree about which generation is current.
    """
    identity, version = embedder.active_embedding_model()
    embedded_total = store.count_chunks(org_id, embedded_only=True)
    awaiting = (
        store.count_stale_model(org_id, identity, version) if embedded_total else 0
    )
    on_active = embedded_total - awaiting
    progress = 1.0 if embedded_total == 0 else round(on_active / embedded_total, 4)
    return {
        "active_model": identity,
        "active_model_version": version,
        "embedded_total": embedded_total,
        "on_active_model": on_active,
        "awaiting_backfill": awaiting,
        "progress": progress,
        "complete": awaiting == 0,
    }


def freshness_metrics(org_id: str) -> Dict[str, Any]:
    """Compute the org's full freshness-lag picture (AC7). Raises on read failure.

    Returns the queryable shape the ``/api/retrieval/freshness`` route serves and
    the Sprint-2 run-health dashboard consumes: pending change events, failed
    refreshes, stale chunk count, embedding backlog, and backfill progress —
    every count scoped to ``org_id`` and nothing else (the store/queue
    primitives bind the org in SQL).

    ``generated_at`` is the wall-clock read time: metrics describe "now", unlike
    run artifacts, so this is one of the few places the wall clock is correct.
    """
    total_chunks = store.count_chunks(org_id)
    embedded = store.count_chunks(org_id, embedded_only=True)
    return {
        "org_id": org_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pending_change_events": refresh_queue.pending_count(org_id),
        "failed_refreshes": refresh_queue.failed_count(org_id),
        "stale_chunks": store.count_stale(org_id),
        "chunks_total": total_chunks,
        "chunks_embedded": embedded,
        "pending_embeddings": total_chunks - embedded,
        "backfill": backfill_progress(org_id),
    }
