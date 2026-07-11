"""Retrieval-freshness storage model (R18-B2 — Retrieval Freshness, T1).

Adds the freshness machinery on top of the R18-B1 ``retrieval_chunks`` store:

  * a ``is_stale`` flag on ``retrieval_chunks`` — set when a source artifact
    changes (``ingestion.artifact_changed`` / created|updated), cleared when the
    artifact is refreshed. Stale chunks are second-class: excluded from default
    retrieval until refreshed (the exclusion policy itself is T4). A ``stale_at``
    timestamp records WHEN the chunk went stale so freshness lag is measurable
    (T6 metrics).
  * a ``retrieval_refresh_queue`` table — the durable work list of artifacts that
    changed and need re-chunk + re-embed. The subscriber (T1) enqueues; the async
    refresh worker (T3) drains it. Persisted (not in-memory) so a queued refresh
    survives a restart — a change event is unreconstructable once consumed.

This module is the SINGLE SOURCE OF TRUTH for the freshness schema, imported by
both ``migrations/versions/0025_add_retrieval_freshness.py`` (the CI gate) and the
runtime ``app.retrieval.store.ensure_freshness_schema()`` helper, so the
migration-applied schema and the runtime-created schema can never drift — the same
no-drift pattern R18-B1 used for ``retrieval.py`` / ``0024``.

Section 1 (The Freshness Contract) of the story fixes the behaviour these objects
support:

  * "Change invalidates" — updated marks chunks stale + queues refresh; deleted
    removes chunks immediately (before the queue). T1 wires the subscriber that
    performs these actions.
  * "Stale is second-class" — the ``is_stale`` flag is what T4 reads to exclude
    stale chunks from default retrieval.
  * "Lag is visible" — ``stale_at`` and the queue's ``enqueued_at`` /
    ``status`` are what T6 turns into pending-events / stale-count / backfill
    metrics.
"""
from __future__ import annotations


# Status values for a queued refresh. Kept as a frozenset so the DDL CHECK
# constraint and any Python-side validation can never disagree (same derivation
# the R18-B1 store uses for its content_type enum column).
REFRESH_STATUSES = frozenset({"pending", "in_progress", "done", "failed"})

_REFRESH_STATUS_CHECK = ", ".join(f"'{s}'" for s in sorted(REFRESH_STATUSES))


# ---------------------------------------------------------------------------
# 1) is_stale flag on the existing retrieval_chunks table
# ---------------------------------------------------------------------------

# ADD COLUMN ... IF NOT EXISTS is idempotent, so this runs safely both as a
# migration and via the runtime ensure helper. Existing rows default to NOT stale
# (they were current when written); the subscriber flips the flag on change.
ADD_IS_STALE_COLUMN = """
    ALTER TABLE retrieval_chunks
        ADD COLUMN IF NOT EXISTS is_stale BOOLEAN NOT NULL DEFAULT FALSE
"""

ADD_STALE_AT_COLUMN = """
    ALTER TABLE retrieval_chunks
        ADD COLUMN IF NOT EXISTS stale_at TIMESTAMP
"""

# T4 excludes stale chunks by default; T6 counts them per org. Both read
# (org_id, is_stale), so index it. Leads with org_id to stay consistent with the
# store's org-first partition (every predicate binds org_id first).
CREATE_IDX_ORG_STALE = """
    CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_org_stale
        ON retrieval_chunks (org_id, is_stale)
"""


# ---------------------------------------------------------------------------
# 2) retrieval_refresh_queue — durable work list for the async refresh worker
# ---------------------------------------------------------------------------

# One row per (org, artifact) awaiting refresh. Re-queuing an artifact that is
# already pending must not create a duplicate — the unique index on
# (org_id, source_system, source_artifact) lets the enqueue upsert collapse
# repeat change events for the same artifact into one pending row (Section 1:
# "cost proportional to change", not to event volume).
CREATE_REFRESH_QUEUE_TABLE = f"""
    CREATE TABLE IF NOT EXISTS retrieval_refresh_queue (
        id               VARCHAR(36)   NOT NULL PRIMARY KEY,
        org_id           VARCHAR(64)   NOT NULL,
        source_system    VARCHAR(64)   NOT NULL,
        source_artifact  VARCHAR(512)  NOT NULL,
        change_kind      VARCHAR(16)   NOT NULL,
        status           VARCHAR(16)   NOT NULL DEFAULT 'pending'
                             CHECK (status IN ({_REFRESH_STATUS_CHECK})),
        attempts         INTEGER       NOT NULL DEFAULT 0,
        enqueued_at      TIMESTAMP     NOT NULL,
        updated_at       TIMESTAMP     NOT NULL,
        last_error       TEXT
    )
"""

# Collapse repeat change events for one artifact into a single pending row.
CREATE_REFRESH_QUEUE_UNIQUE = """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_refresh_queue_org_artifact
        ON retrieval_refresh_queue (org_id, source_system, source_artifact)
"""

# The worker (T3) drains oldest-pending-first per org; T6 counts pending per org.
# Both read (org_id, status), so index it.
CREATE_REFRESH_QUEUE_IDX_ORG_STATUS = """
    CREATE INDEX IF NOT EXISTS idx_refresh_queue_org_status
        ON retrieval_refresh_queue (org_id, status)
"""


ALL_FRESHNESS_DDL: "tuple[str, ...]" = (
    ADD_IS_STALE_COLUMN,
    ADD_STALE_AT_COLUMN,
    CREATE_IDX_ORG_STALE,
    CREATE_REFRESH_QUEUE_TABLE,
    CREATE_REFRESH_QUEUE_UNIQUE,
    CREATE_REFRESH_QUEUE_IDX_ORG_STATUS,
)

# DROP order for downgrade() — queue indexes + table, then the chunk index and
# columns. The retrieval_chunks table itself is owned by 0024 and is NOT dropped
# here.
DROP_FRESHNESS_DDL: "tuple[str, ...]" = (
    "DROP INDEX IF EXISTS idx_refresh_queue_org_status",
    "DROP INDEX IF EXISTS uq_refresh_queue_org_artifact",
    "DROP TABLE IF EXISTS retrieval_refresh_queue",
    "DROP INDEX IF EXISTS idx_retrieval_chunks_org_stale",
    "ALTER TABLE retrieval_chunks DROP COLUMN IF EXISTS stale_at",
    "ALTER TABLE retrieval_chunks DROP COLUMN IF EXISTS is_stale",
)
