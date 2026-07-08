"""Retrieval refresh queue — the durable work list for freshness (R18-B2 T1).

When a source artifact changes, the freshness subscriber (``freshness.py``) marks
its chunks stale and ENQUEUES the artifact here for refresh. The heavy work —
re-extract, re-chunk, hash-compare, re-embed, atomic swap — is done later and off
the discovery-run path by the async refresh worker (T3), which drains this queue.
Splitting "notice the change" (fast, synchronous with the change event) from
"refresh the content" (slow, asynchronous) is what keeps normal discovery runs
from ever blocking on re-embedding (Section 1 / the task's async requirement).

The queue is PERSISTED, not in-memory: a change event is cheap to emit now and
unreconstructable later (the 1.6 design-ahead rationale), so a queued refresh must
survive a process restart. It is keyed per ``(org_id, source_system,
source_artifact)`` with a unique index, so repeated change events for the same
artifact collapse into ONE pending row — refresh cost stays proportional to real
change, not to event volume (Section 4: "cost proportional to change").

Every access is org-scoped, consistent with the store's hard tenant partition.

Scope note (T1): this task delivers ``enqueue`` (the subscriber's write) plus the
org-scoped read/count primitives the worker (T3) and metrics (T6) consume. The
worker that DRAINS the queue and the metrics endpoint are their own tasks.
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app import db

logger = logging.getLogger(__name__)


def enqueue(
    org_id: str,
    source_system: str,
    source_artifact: str,
    change_kind: str = "updated",
) -> str:
    """Queue one artifact for asynchronous refresh. Returns the queue row id.

    Called by the freshness subscriber on a ``created``/``updated`` change event,
    right after the artifact's chunks are marked stale. Idempotent per artifact:
    if the artifact is already queued (pending or mid-refresh), the existing row is
    updated in place (change_kind refreshed, ``updated_at`` bumped, reset to
    ``pending`` so a change that lands during a refresh is not lost) rather than
    duplicated — the unique ``(org_id, source_system, source_artifact)`` index
    backs the upsert. Returns the id of the pending row (existing or new).

    Org-scoped by construction. Never embeds or does any heavy work — enqueuing is
    intentionally cheap so it can run synchronously with the change event.
    """
    now = datetime.now(timezone.utc)
    new_id = str(uuid4())
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO retrieval_refresh_queue "
            "  (id, org_id, source_system, source_artifact, change_kind, "
            "   status, attempts, enqueued_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s, %s) "
            "ON CONFLICT (org_id, source_system, source_artifact) DO UPDATE SET "
            "   change_kind = EXCLUDED.change_kind, "
            "   status = 'pending', "
            "   updated_at = EXCLUDED.updated_at "
            "RETURNING id",
            (new_id, org_id, source_system, source_artifact, change_kind, now, now),
        )
        row = cur.fetchone()
        con.commit()
    # RETURNING id gives the surviving row's id (the existing one on conflict).
    return row[0] if row else new_id


def pending_count(org_id: str) -> int:
    """Return the number of artifacts pending refresh for an org (T6 metric seed).

    Org-scoped. Counts rows still awaiting work (``pending``) — the "events
    pending" component of the freshness-lag metric (Section 1: "Lag is visible").
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM retrieval_refresh_queue "
            "WHERE org_id = %s AND status = 'pending'",
            (org_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def fetch_pending(org_id: str, limit: int = 64) -> list[dict[str, Any]]:
    """Return up to ``limit`` of an org's pending refresh rows, oldest first.

    Feeds the async refresh worker (T3): it claims pending artifacts and does the
    re-chunk + re-embed. Org-scoped; ordered by ``enqueued_at`` so the backlog
    drains oldest-first and repeated calls make forward progress.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT id, org_id, source_system, source_artifact, change_kind, "
            "       status, attempts, enqueued_at, updated_at, last_error "
            "FROM retrieval_refresh_queue "
            "WHERE org_id = %s AND status = 'pending' "
            "ORDER BY enqueued_at ASC, id ASC "
            "LIMIT %s",
            (org_id, int(limit)),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def remove(org_id: str, source_system: str, source_artifact: str) -> int:
    """Drop an artifact's queue row. Returns rows removed.

    Used when a ``deleted`` change event supersedes a pending refresh (the
    artifact is gone — there is nothing left to refresh), and by the worker (T3)
    once a refresh completes. Org-scoped.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM retrieval_refresh_queue "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
            (org_id, source_system, source_artifact),
        )
        removed = cur.rowcount
        con.commit()
    return removed
