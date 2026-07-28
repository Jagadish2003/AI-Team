"""Vector storage — pgvector index, HARD-PARTITIONED by org_id (R18-B1 T1).

The ``store`` layer owns the ``retrieval_chunks`` table (schema:
``database/models/retrieval.py``) and is the ONLY module that reads or writes
vectors. pgvector lives in the existing PostgreSQL — no separate vector database,
so on-prem installs add no infrastructure (Section 6).

Tenant isolation is the store's load-bearing invariant: **every** query in this
module binds ``org_id`` in its ``WHERE`` clause. There is no function that reads
or deletes chunks without an ``org_id`` argument, so one customer's indexed
content can never be returned to another even when the query text is similar
(AC3, R17-D3). Callers cannot opt out of the partition — it is applied at the SQL
layer here, not left to the caller to remember.

Scope note (T1): this task delivers the store and its org-partitioned read/write
primitives. The ranked ``retrieve()`` entry point and its telemetry are finalised
in T4 (``api.py``); the async batch embedding that stamps vectors is T3
(``embedder.py``). Both consume the primitives defined here.
"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from uuid import uuid4

from app import db
from database.models.retrieval import ALL_RETRIEVAL_DDL, RetrievalChunkRecord
from database.models.retrieval_freshness import ALL_FRESHNESS_DDL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def ensure_retrieval_store() -> None:
    """Create the pgvector extension, table, and indexes if absent.

    Runs the identical ``ALL_RETRIEVAL_DDL`` the migration
    (``0024_create_retrieval_chunks.py``) applies, so a runtime-created schema and
    a migration-applied schema can never drift. Every statement is idempotent
    (``IF NOT EXISTS``).

    The migration is the authoritative provisioning path; this helper exists for
    local/dev and tests. ``CREATE EXTENSION`` needs a privileged role — if the
    runtime role lacks it, the extension is expected to already exist (created by
    the migration) and the failure is logged, not raised, so startup is never
    blocked.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        for ddl in ALL_RETRIEVAL_DDL:
            try:
                cur.execute(ddl)
            except Exception:  # pragma: no cover - privilege/ordering edge
                con.rollback()
                if "EXTENSION" in ddl.upper():
                    logger.warning(
                        "ensure_retrieval_store: could not create pgvector "
                        "extension (expected if already provisioned by the "
                        "migration or role lacks privilege); continuing.",
                        exc_info=True,
                    )
                    continue
                raise
        con.commit()


def ensure_freshness_schema() -> None:
    """Create the R18-B2 freshness schema if absent (idempotent).

    Runs the identical ``ALL_FRESHNESS_DDL`` the migration
    (``0025_add_retrieval_freshness.py``) applies — the ``is_stale`` / ``stale_at``
    columns on ``retrieval_chunks`` and the ``retrieval_refresh_queue`` table — so
    the runtime-created schema and the migration-applied schema can never drift.
    Every statement is idempotent (``IF NOT EXISTS``). Local/dev/test convenience;
    the migration is the authoritative provisioning path in production.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        for ddl in ALL_FRESHNESS_DDL:
            cur.execute(ddl)
        con.commit()


# ---------------------------------------------------------------------------
# Vector <-> pgvector text literal
# ---------------------------------------------------------------------------


def _to_vector_literal(vector: Sequence[float]) -> str:
    """Format a Python float sequence as a pgvector text literal, e.g. ``[1,2,3]``.

    Used with an explicit ``%s::vector`` cast in every statement so the store
    needs no extra Python driver package — the ``vector`` type is provided by the
    pgvector DB extension, and its text input format is a bracketed, comma-joined
    list of floats.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _parse_vector_literal(text: Optional[str]) -> Optional[list[float]]:
    """Parse a pgvector text literal (``[1,2,3]``) back into a float list.

    The inverse of :func:`_to_vector_literal`, used by the freshness refresh path
    (T3) to read a stored vector out so it can be carried forward onto a chunk whose
    content did not change — re-embedding it would be wasted model spend (AC3).
    Returns ``None`` for a NULL/blank/malformed literal so an un-embedded or
    unreadable row is simply treated as "needs embedding", never an error.
    """
    if text is None:
        return None
    body = str(text).strip()
    if not body or body in ("[]", "[ ]"):
        return None
    body = body.strip("[]")
    if not body.strip():
        return None
    try:
        return [float(part) for part in body.split(",")]
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Writes  (producers -> substrate; embedding pipeline stamps vectors)
# ---------------------------------------------------------------------------


def _insert_chunk_row(cur: Any, rec: RetrievalChunkRecord) -> None:
    """Write one chunk record on an open cursor (INSERT ... ON CONFLICT DO UPDATE).

    Shared by :func:`upsert_chunks`, :func:`replace_artifact_chunks`, and
    :func:`swap_artifact_chunks` so the exact column set, the ``embedding`` ->
    pgvector-literal handling, and the conflict upsert are defined in ONE place.
    Does not commit — the caller owns the transaction boundary (that is what lets
    the re-ingest and freshness-refresh swaps delete + all inserts atomically).
    Rows are inserted with ``is_stale`` left to its column default (FALSE): a
    freshly written chunk is current, never stale.
    """
    row = rec.to_db_row()
    embedding = row.pop("embedding")
    columns = list(row.keys()) + ["embedding"]
    placeholders = ["%s"] * len(row) + (
        ["%s::vector"] if embedding is not None else ["NULL"]
    )
    values: list[Any] = list(row.values())
    if embedding is not None:
        values.append(_to_vector_literal(embedding))
    set_clause = ", ".join(
        f"{col}=EXCLUDED.{col}" for col in columns if col != "chunk_id"
    )
    cur.execute(
        f"INSERT INTO retrieval_chunks ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (chunk_id) DO UPDATE SET {set_clause}",
        values,
    )


def upsert_chunks(records: Sequence[RetrievalChunkRecord]) -> int:
    """Insert or replace chunk records by ``chunk_id``. Returns rows written.

    Records may be stored WITHOUT an embedding (``embedding is None``): content is
    indexed first and embedded asynchronously (T3), so un-embedded rows are
    provenance-complete but not yet retrievable (AC7). ``org_id`` is part of every
    row, so the partition is populated on write.
    """
    if not records:
        return 0

    written = 0
    with closing(db.connect()) as con:
        cur = con.cursor()
        for rec in records:
            _insert_chunk_row(cur, rec)
            written += 1
        con.commit()
    return written


def replace_artifact_chunks(
    org_id: str,
    source_system: str,
    source_artifact: str,
    records: Sequence[RetrievalChunkRecord],
) -> tuple[int, int]:
    """Atomically replace one artifact's chunks: DELETE + INSERT in one transaction.

    The re-ingest path (T5): a producer re-hands a changed artifact and its
    previous chunks must be swapped for the new set. Doing the delete and the
    inserts as ONE transaction on ONE connection closes the atomicity gap of
    calling ``delete_by_artifact`` and ``upsert_chunks`` separately — if any
    insert fails, the whole operation rolls back and the previously indexed
    chunks are left fully intact (never silently destroyed).

    Returns ``(deleted, written)``. Org-scoped: the DELETE binds ``org_id`` and
    every inserted row carries it.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        try:
            cur.execute(
                "DELETE FROM retrieval_chunks "
                "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
                (org_id, source_system, source_artifact),
            )
            deleted = cur.rowcount
            written = 0
            for rec in records:
                _insert_chunk_row(cur, rec)
                written += 1
            con.commit()
        except Exception:
            con.rollback()
            raise
    return deleted, written


def set_embedding(
    chunk_id: str,
    org_id: str,
    vector: Sequence[float],
    embedding_model: str,
    embedding_model_version: str,
) -> bool:
    """Stamp a chunk's embedding + model identity/version. Returns True if updated.

    Called by the async embedding pipeline (T3). The model identity and version
    are stored WITH the vector so retrieval can refuse to compare vectors produced
    by different models (AC8). Org-scoped: the ``WHERE`` binds ``org_id`` so a
    caller can only ever stamp a vector within its own partition.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE retrieval_chunks "
            "SET embedding = %s::vector, embedding_model = %s, "
            "    embedding_model_version = %s, embedded_at = %s, updated_at = %s "
            "WHERE chunk_id = %s AND org_id = %s",
            (
                _to_vector_literal(vector),
                embedding_model,
                embedding_model_version,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                chunk_id,
                org_id,
            ),
        )
        updated = cur.rowcount
        con.commit()
    return updated > 0


def delete_by_artifact(org_id: str, source_system: str, source_artifact: str) -> int:
    """Delete all chunks for one source artifact within an org. Returns rows deleted.

    Supports the re-ingest path (T5): when a producer hands over a changed
    artifact, its previous chunks are removed before the new ones are written.
    Org-scoped by construction.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM retrieval_chunks "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
            (org_id, source_system, source_artifact),
        )
        deleted = cur.rowcount
        con.commit()
    return deleted


# ---------------------------------------------------------------------------
# Freshness invalidation  (R18-B2 T1) — always org-scoped
# ---------------------------------------------------------------------------


def mark_stale(org_id: str, source_system: str, source_artifact: str) -> int:
    """Mark all of one artifact's chunks stale within an org. Returns rows marked.

    The freshness subscriber (T1) calls this on a ``created``/``updated``
    ``ingestion.artifact_changed`` event: the artifact's indexed chunks may no
    longer reflect the source, so they are flagged second-class until the async
    refresh worker (T3) replaces them. T4 excludes ``is_stale`` chunks from default
    retrieval; ``stale_at`` records WHEN they went stale so freshness lag is
    measurable (T6).

    Idempotent: re-marking an already-stale artifact is a harmless no-op update
    (``stale_at`` is only stamped on rows that were not already stale, so the
    original staleness time is preserved). Org-scoped by construction.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE retrieval_chunks "
            "SET is_stale = TRUE, stale_at = %s, updated_at = %s "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s "
            "  AND is_stale = FALSE",
            (
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                org_id,
                source_system,
                source_artifact,
            ),
        )
        marked = cur.rowcount
        con.commit()
    return marked


def mark_stale_and_enqueue(
    org_id: str,
    source_system: str,
    source_artifact: str,
    change_kind: str = "updated",
) -> tuple[int, str]:
    """Atomically mark an artifact's chunks stale AND queue its refresh (T1).

    The created/updated invalidation is two facts that must never be observed
    apart: "these chunks are second-class" (``retrieval_chunks.is_stale``) and
    "a refresh is owed" (a ``pending`` ``retrieval_refresh_queue`` row). Committing
    them in separate transactions leaves a failure mode with no recovery path: if
    the stale-mark commits but the enqueue then fails, the chunks are excluded from
    default retrieval (T4) with no pending work item — and since the change event
    fires only on artifact change, nothing ever re-queues them. The artifact stays
    dark indefinitely.

    So this does both in ONE transaction — the same atomicity stance
    :func:`purge_artifact` takes for the deletion path. Either the chunks are stale
    AND the refresh is queued, or neither happened and the subscriber's guard logs
    the event as failed (a re-emitted/next change event can retry cleanly).

    The stale-mark half matches :func:`mark_stale` (only not-yet-stale rows are
    stamped, preserving the original ``stale_at``); the enqueue half matches
    ``refresh_queue.enqueue`` (idempotent upsert on the unique
    ``(org_id, source_system, source_artifact)`` index, resetting the row to
    ``pending``). Returns ``(chunks_marked, queue_row_id)``. Org-scoped by
    construction.
    """
    now = datetime.now(timezone.utc)
    new_id = str(uuid4())
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE retrieval_chunks "
            "SET is_stale = TRUE, stale_at = %s, updated_at = %s "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s "
            "  AND is_stale = FALSE",
            (now, now, org_id, source_system, source_artifact),
        )
        marked = cur.rowcount
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
    return marked, (row[0] if row else new_id)


def remove_chunks(org_id: str, source_system: str, source_artifact: str) -> int:
    """Immediately remove one artifact's chunks from retrieval. Returns rows removed.

    The freshness subscriber calls this on a ``deleted``
    ``ingestion.artifact_changed`` event. Deletion is honoured BEFORE the re-embed
    queue, never after (Section 1 / AC2): the moment a source artifact is known to
    be gone, its evidence must stop being retrievable — there is no window where
    deleted content is still served as current. This is a hard delete of the rows,
    identical in effect to :func:`delete_by_artifact`; the distinct name keeps the
    freshness contract's vocabulary (``store.remove_chunks``) explicit at the call
    site. Org-scoped by construction.

    Removes only the chunk rows. The deletion path (R18-B2 T2) instead uses
    :func:`purge_artifact`, which also drops the artifact's pending refresh-queue
    row in the SAME transaction so deletion never leaves an orphaned refresh
    scheduled against content that is already gone.
    """
    return delete_by_artifact(org_id, source_system, source_artifact)


def purge_artifact(
    org_id: str, source_system: str, source_artifact: str
) -> tuple[int, int]:
    """Atomically remove a deleted artifact from the retrieval index (R18-B2 T2).

    Deletion is the strongest freshness case: the moment a source artifact is known
    gone, it must stop being retrievable AND must stop being scheduled for refresh —
    there is no valid content left to re-embed. This is a DIRECT index-cleanup
    operation, not a refresh: it does the whole cleanup in ONE transaction so the
    two facts (chunks gone, no pending refresh) commit together and are never
    observed apart.

    In a single transaction it:

      1. deletes every ``retrieval_chunks`` row for ``(org_id, source_system,
         source_artifact)`` — the artifact stops being retrievable immediately, with
         no window where deleted content is still served as current (AC2); and
      2. deletes the artifact's ``retrieval_refresh_queue`` row (if any) — a delete
         supersedes any pending refresh, so a delayed refresh worker (T3) never
         wakes to re-embed an artifact that no longer exists.

    Doing both in one transaction is what keeps the system correct even if the
    refresh worker is delayed or the process dies mid-cleanup: the substrate never
    ends up with a queue row pointing at chunks that are already gone (the two
    separate statements can never partially commit). Returns
    ``(chunks_removed, queue_rows_removed)``.

    The store touches ``retrieval_refresh_queue`` (owned by ``refresh_queue.py``)
    only where the freshness contract demands chunk-state and queue-state commit
    together — here and :func:`mark_stale_and_enqueue`: the atomicity AC2 demands
    can only be met by committing both statements together, and the queue row is
    index bookkeeping for the very chunks being changed. Org-scoped by
    construction — every predicate binds
    ``org_id`` first, so one org's delete can never remove another's rows even when
    the artifact id collides.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM retrieval_chunks "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
            (org_id, source_system, source_artifact),
        )
        chunks_removed = cur.rowcount
        cur.execute(
            "DELETE FROM retrieval_refresh_queue "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
            (org_id, source_system, source_artifact),
        )
        queue_removed = cur.rowcount
        con.commit()
    return chunks_removed, queue_removed


def count_stale(org_id: str) -> int:
    """Return the number of stale chunks for an org (freshness metrics, T6 seed).

    Org-scoped like every read. Exposed now so T1's tests can assert the
    invalidation actually flipped the flag; T6 builds the run-health metric on it.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM retrieval_chunks WHERE org_id = %s AND is_stale = TRUE",
            (org_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Freshness refresh  (R18-B2 T3) — hash-compare inputs + atomic chunk swap
# ---------------------------------------------------------------------------


def get_chunks_for_artifact(
    org_id: str, source_system: str, source_artifact: str
) -> list[dict[str, Any]]:
    """Return one artifact's stored chunks with hash + vector (refresh input, T3).

    The refresh worker (``app.retrieval.refresh``) reads the CURRENTLY-indexed
    chunks so it can hash-compare them against the freshly re-extracted content and
    decide, per chunk, whether anything actually changed. For an UNCHANGED chunk
    (same ``content_hash``) the stored ``embedding`` is carried straight over to the
    replacement row, so unchanged content is never re-embedded (AC3) — that is why
    the vector is read back here (as a pgvector text literal, parsed to a float
    list) alongside its model identity/version.

    Ordered by ``chunk_position`` for readable diffs. Org-scoped: the ``WHERE``
    leads with ``org_id = %s`` so a caller only ever sees its own partition (AC3).
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content_hash, content_type, chunk_position, "
            "       is_stale, embedding::text AS embedding_text, "
            "       embedding_model, embedding_model_version, embedded_at "
            "FROM retrieval_chunks "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s "
            "ORDER BY chunk_position ASC, chunk_id ASC",
            (org_id, source_system, source_artifact),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["embedding"] = _parse_vector_literal(d.pop("embedding_text", None))
        out.append(d)
    return out


def swap_artifact_chunks(
    org_id: str,
    source_system: str,
    source_artifact: str,
    records: Sequence[RetrievalChunkRecord],
) -> tuple[int, int]:
    """Atomically replace one artifact's chunks with ``records`` (T3 / AC4).

    The refresh worker rebuilds the full chunk set for an artifact (reusing carried-
    over vectors for unchanged chunks, freshly-embedded vectors for changed ones)
    and hands it here to be swapped in. In a SINGLE transaction this:

      1. deletes every existing ``retrieval_chunks`` row for ``(org_id,
         source_system, source_artifact)``; then
      2. inserts every record in ``records``.

    Because both happen in one transaction, retrieval NEVER sees a mix of old and
    new chunks for the artifact — a concurrent reader observes either the complete
    old set or the complete new set, never a half-and-half blend (AC4). The
    inserted rows take the ``is_stale`` column default (FALSE), so the artifact's
    stale flag is cleared as an inseparable part of the same commit that installs
    the new content — never before the replacement is durable. Returns
    ``(chunks_removed, chunks_written)``. Org-scoped by construction.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM retrieval_chunks "
            "WHERE org_id = %s AND source_system = %s AND source_artifact = %s",
            (org_id, source_system, source_artifact),
        )
        removed = cur.rowcount
        written = 0
        for rec in records:
            _insert_chunk_row(cur, rec)
            written += 1
        con.commit()
    return removed, written


# ---------------------------------------------------------------------------
# Reads  (always org-scoped)
# ---------------------------------------------------------------------------


def search(
    org_id: str,
    query_vector: Sequence[float],
    k: int = 10,
    source_filter: Optional[Sequence[str]] = None,
    artifact_filter: Optional[Sequence[str]] = None,
    min_score: Optional[float] = None,
    embedding_model: Optional[str] = None,
    embedding_model_version: Optional[str] = None,
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Return this org's most-similar chunks to ``query_vector`` (cosine similarity).

    HARD org partition: the ``WHERE`` clause always leads with ``org_id = %s`` — a
    query can only ever match the caller's own partition (AC3).

    ``source_filter``    scopes results to named source systems ('only Confluence',
                         'only this repo') via ``source_system = ANY(...)`` (AC4).
    ``artifact_filter``  scopes results to an EXACT set of ``source_artifact`` ids
                         (``source_artifact = ANY(...)``) — never a LIKE/substring
                         match. R18-A6 T5's component-scoped retrieval builds this
                         allowlist from the structure map (component -> declaring
                         file), so a query can be confined to one component's REAL
                         files rather than any file whose path/name coincidentally
                         resembles it.
    ``min_score``        excludes weak matches below the cosine-similarity floor
                         (AC4).
    ``embedding_model`` / ``embedding_model_version`` restrict the search to
                       vectors produced by a specific model, so vectors from
                       different models are never compared (AC8). ``retrieve()``
                       (T4) passes the active model here.
    ``include_stale``  when ``False`` (default) stale chunks are excluded at the
                       SQL layer (``is_stale = FALSE``) so a source artifact that
                       changed is not served as current evidence until refreshed
                       (R18-B2 T4 / AC1). Set ``True`` to include stale rows for a
                       caller that explicitly wants them (or that surfaces the
                       staleness itself, as the context-assembly evidence source
                       does so the exclusion stays visible on the selection log —
                       AC6). The served-first-class-only-when-fresh index
                       ``idx_retrieval_chunks_org_stale`` covers this predicate.

    Only embedded rows participate (``embedding IS NOT NULL``): un-embedded
    content is absent from retrieval, never an error (AC7). Returns raw row dicts
    each carrying a ``similarity`` score and its ``is_stale`` flag; ``api.retrieve()``
    maps these to ``RetrievedChunk`` and mints the per-result
    ``retrieval_result_id``.
    """
    literal = _to_vector_literal(query_vector)

    where = ["org_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [org_id]
    if not include_stale:
        # Freshness contract (R18-B2 / AC1): stale chunks are second-class and
        # excluded from default retrieval until the async refresh worker replaces
        # them. A no-parameter literal, so it never shifts the vector param order.
        where.append("is_stale = FALSE")
    if source_filter:
        where.append("source_system = ANY(%s)")
        params.append(list(source_filter))
    if artifact_filter:
        where.append("source_artifact = ANY(%s)")
        params.append(list(artifact_filter))
    if embedding_model is not None:
        where.append("embedding_model = %s")
        params.append(embedding_model)
    if embedding_model_version is not None:
        where.append("embedding_model_version = %s")
        params.append(embedding_model_version)
    if min_score is not None:
        # cosine similarity = 1 - cosine distance (<=>). Filter before ranking.
        where.append("(1 - (embedding <=> %s::vector)) >= %s")
        params.extend([literal, min_score])

    sql = (
        "SELECT chunk_id, org_id, content, content_hash, content_type, "
        "       source_system, source_artifact, source_timestamp, chunk_position, "
        "       embedding_model, embedding_model_version, is_stale, "
        "       (1 - (embedding <=> %s::vector)) AS similarity "
        "FROM retrieval_chunks "
        f"WHERE {' AND '.join(where)} "
        # Rank by ascending cosine distance (= descending similarity). chunk_id is a
        # deterministic tie-break so two chunks at the same distance always order
        # identically across calls — retrieval ranking must be reproducible (T4).
        "ORDER BY embedding <=> %s::vector ASC, chunk_id ASC "
        "LIMIT %s"
    )
    # Vector params bind in statement order: SELECT similarity, [min_score filter],
    # ORDER BY, then LIMIT.
    exec_params = [literal] + params + [literal, int(k)]

    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, exec_params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_chunks_by_artifacts(
    org_id: str,
    artifacts: Sequence[str],
    limit: int = 10,
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Return this org's embedded chunks for an EXACT set of ``source_artifact`` ids.

    The no-query counterpart to :func:`search` — no vector to rank against, so
    this is a direct read rather than a similarity search. Used by R18-A6 T5's
    component-scoped retrieval when a caller wants a component's currently-indexed
    code directly, rather than an answer to a semantic question about it.

    ``artifacts`` is matched EXACTLY (``source_artifact = ANY(%s)``) — never a
    LIKE/substring match — so a file that merely mentions a component's name in a
    comment, or an unrelated ``*Test`` file, is never mistaken for that
    component's real code (AC5).

    HARD org partition (``org_id = %s`` first, AC3). Only embedded rows
    participate (``embedding IS NOT NULL``), matching :func:`search`'s retrieval
    invariant — un-embedded content is not-yet-retrievable, never an error.
    ``include_stale=False`` (default) excludes stale chunks, matching the
    freshness contract (R18-B2 / AC1). Ordered by ``source_artifact`` then
    ``chunk_position`` for a stable, readable listing; ``chunk_id`` breaks ties
    deterministically.
    """
    if not artifacts:
        return []
    where = ["org_id = %s", "embedding IS NOT NULL", "source_artifact = ANY(%s)"]
    params: list[Any] = [org_id, list(artifacts)]
    if not include_stale:
        where.append("is_stale = FALSE")
    sql = (
        "SELECT chunk_id, org_id, content, content_hash, content_type, "
        "       source_system, source_artifact, source_timestamp, chunk_position, "
        "       embedding_model, embedding_model_version, is_stale "
        "FROM retrieval_chunks "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY source_artifact ASC, chunk_position ASC, chunk_id ASC "
        "LIMIT %s"
    )
    params.append(int(limit))
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def count_chunks(org_id: str, embedded_only: bool = False) -> int:
    """Return the number of chunks stored for an org (diagnostics/tests).

    Org-scoped like every read. ``embedded_only`` counts just the retrievable
    (embedded) rows — useful for asserting the async embedding invariant (AC7).
    """
    sql = "SELECT COUNT(*) FROM retrieval_chunks WHERE org_id = %s"
    if embedded_only:
        sql += " AND embedding IS NOT NULL"
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, (org_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def iter_artifact_provenance(
    org_id: str, source_systems: Optional[Sequence[str]] = None
) -> list[dict[str, Any]]:
    """Return this org's distinct indexed artifacts with their provenance.

    A DETERMINISTIC, exact read (no vectors, no similarity) that lets a caller
    resolve a stable identifier against the provenance of ingested content — e.g.
    MSP-B5's explicit-citation runbook matcher, which joins a cited runbook id to
    the runbook page that declares it. One row per ``(source_system,
    source_artifact)`` (the substrate's stable-id key), carrying the parsed
    ``provenance`` dict, the representative ``chunk_id``, and ``source_timestamp``.

    HARD org partition: the ``WHERE`` leads with ``org_id = %s`` so a caller only
    ever sees its own partition (AC3). ``source_systems`` optionally scopes to named
    systems (e.g. the runbook library's ``document``/``confluence``/``sharepoint``)
    via ``source_system = ANY(...)``. Ordered deterministically so repeated reads
    return an identical, reproducible sequence.
    """
    sql = (
        "SELECT DISTINCT ON (source_system, source_artifact) "
        "       source_system, source_artifact, source_timestamp, "
        "       provenance, chunk_id "
        "FROM retrieval_chunks "
        "WHERE org_id = %s "
    )
    params: list[Any] = [org_id]
    systems = [str(s) for s in (source_systems or []) if s]
    if systems:
        sql += "AND source_system = ANY(%s) "
        params.append(systems)
    sql += (
        "ORDER BY source_system ASC, source_artifact ASC, "
        "         chunk_position ASC, chunk_id ASC"
    )
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("provenance")
        if isinstance(raw, str) and raw:
            try:
                d["provenance"] = json.loads(raw)
            except (ValueError, TypeError):
                d["provenance"] = {}
        elif not isinstance(raw, dict):
            d["provenance"] = {}
        out.append(d)
    return out


def count_chunks_by_source(org_id: str) -> list[dict[str, Any]]:
    """Return the org's indexed-content volume broken down by source system.

    R18-C2 T1 (Run-Health Dashboard, content panel): a read-only aggregation of
    the chunks already stored — assembling existing truth, not new instrumentation.
    Org-scoped like every read. Each row reports the total chunk count and the
    embedded (retrievable) subset for one ``source_system``, ordered by source so
    the panel renders deterministically.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT source_system, COUNT(*) AS chunk_count, "
            "COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedded_count "
            "FROM retrieval_chunks WHERE org_id = %s "
            "GROUP BY source_system ORDER BY source_system ASC",
            (org_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "source_system": r[0],
            "chunk_count": int(r[1]),
            "embedded_count": int(r[2]),
        }
        for r in rows
    ]


def pending_embedding_backlog(org_id: str) -> dict[str, Any]:
    """Return the current unembedded backlog and its observed time span.

    The run-health attention rules use this org-scoped record summary to
    distinguish a transient batch from work that is accumulating: a backlog is
    only considered growing when enough pending rows span a meaningful period.
    ``oldest_created_at`` and ``newest_created_at`` are timestamps from the
    existing chunk records, not dashboard observation timestamps, so repeated
    reads over unchanged data remain deterministic.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) "
            "FROM retrieval_chunks "
            "WHERE org_id = %s AND embedding IS NULL",
            (org_id,),
        )
        row = cur.fetchone()

    count = int(row[0]) if row else 0
    oldest = row[1] if row and row[1] is not None else None
    newest = row[2] if row and row[2] is not None else None
    return {
        "count": count,
        "oldest_created_at": (
            oldest.isoformat() if hasattr(oldest, "isoformat") else oldest
        ),
        "newest_created_at": (
            newest.isoformat() if hasattr(newest, "isoformat") else newest
        ),
    }


# ---------------------------------------------------------------------------
# Async embedding pipeline support  (T3) — reads are ALWAYS org-scoped
# ---------------------------------------------------------------------------


def fetch_unembedded(org_id: str, limit: int = 128) -> list[dict[str, Any]]:
    """Return up to ``limit`` of this org's not-yet-embedded chunks (oldest first).

    Feeds the async batch embedding pipeline (``embedder.embed_pending_for_org``):
    it selects the rows written by producers with ``embedding IS NULL`` — content
    is indexed first and embedded asynchronously (AC7) — and returns just the
    ``chunk_id`` + ``content`` the pipeline needs to embed and then stamp via
    ``set_embedding``.

    HARD org partition: the ``WHERE`` leads with ``org_id = %s`` so the pipeline
    only ever embeds a caller org's own content (AC3). Ordered by ``created_at``
    so the backlog drains oldest-first and repeated calls make forward progress.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content "
            "FROM retrieval_chunks "
            "WHERE org_id = %s AND embedding IS NULL "
            "ORDER BY created_at ASC, chunk_id ASC "
            "LIMIT %s",
            (org_id, int(limit)),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def orgs_with_unembedded(limit: int = 100) -> list[str]:
    """Return distinct org_ids that currently have un-embedded chunks.

    Lets the background embedding worker enumerate which tenants have a backlog
    and then drain each with the org-scoped ``fetch_unembedded`` — so the worker
    covers every org while every content-returning read stays partitioned by
    ``org_id`` (AC3). Returns only org identifiers here, never chunk content.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT org_id "
            "FROM retrieval_chunks "
            "WHERE embedding IS NULL "
            "ORDER BY org_id "
            "LIMIT %s",
            (int(limit),),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Embedding-model-version invalidation + managed backfill  (R18-B2 T5) — AC5.
# The per-vector (embedding_model, embedding_model_version) stamp IS the
# compatibility marker: a vector stamped with anything other than the ACTIVE
# model is incompatible with the active vector space. ``search()`` already refuses
# to compare across model generations by filtering on the active pair (AC8), so an
# old-model vector is invalidated the instant the provider/version repins — it can
# no longer be returned. These reads let the backfill converge every embedded
# vector back onto the active model without re-embedding compatible ones. Reads are
# ALWAYS org-scoped.
# ---------------------------------------------------------------------------


# A row's stamp is incompatible with the active model when EITHER the identity or
# the version differs — ``IS DISTINCT FROM`` so a NULL stamp (never safely
# comparable) also counts as incompatible. Applied only to embedded rows, so it
# never overlaps the ``embedding IS NULL`` pending set the normal pipeline drains.
_STALE_MODEL_PREDICATE = (
    "embedding IS NOT NULL "
    "AND (embedding_model IS DISTINCT FROM %s "
    "     OR embedding_model_version IS DISTINCT FROM %s)"
)


def fetch_stale_model_embedded(
    org_id: str,
    embedding_model: str,
    embedding_model_version: str,
    limit: int = 128,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` of this org's vectors stamped by a NON-active model.

    Feeds the managed backfill (``embedder.backfill_stale_model_for_org``): it
    selects EMBEDDED rows whose ``(embedding_model, embedding_model_version)`` stamp
    differs from the active pair — i.e. vectors that a provider/version switch has
    invalidated (AC5) — and returns just the ``chunk_id`` + ``content`` the backfill
    needs to re-embed and re-stamp via :func:`set_embedding`.

    HARD org partition: the ``WHERE`` leads with ``org_id = %s`` so the backfill
    only ever touches a caller org's own content (AC3). Ordered by ``embedded_at``
    (oldest stamp first) so a repin drains the most-stale vectors first and repeated
    calls make forward progress.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content "
            "FROM retrieval_chunks "
            f"WHERE org_id = %s AND {_STALE_MODEL_PREDICATE} "
            "ORDER BY embedded_at ASC NULLS FIRST, chunk_id ASC "
            "LIMIT %s",
            (org_id, embedding_model, embedding_model_version, int(limit)),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def orgs_with_stale_model(
    embedding_model: str,
    embedding_model_version: str,
    limit: int = 100,
) -> list[str]:
    """Return distinct org_ids holding vectors stamped by a NON-active model.

    Lets the background worker enumerate which tenants still have old-model vectors
    after a provider/version repin and drain each with the org-scoped
    :func:`fetch_stale_model_embedded` — covering every org while every
    content-returning read stays partitioned by ``org_id`` (AC3). Returns only org
    identifiers, never chunk content.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT org_id "
            "FROM retrieval_chunks "
            f"WHERE {_STALE_MODEL_PREDICATE} "
            "ORDER BY org_id "
            "LIMIT %s",
            (embedding_model, embedding_model_version, int(limit)),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


def count_stale_model(
    org_id: str,
    embedding_model: str,
    embedding_model_version: str,
) -> int:
    """Return how many of this org's vectors are stamped by a NON-active model.

    The backfill-progress signal for the active model (R18-B2 T5): it is the count
    of old-model vectors still awaiting re-embedding, so it falls to 0 when a repin
    has fully backfilled. Org-scoped like every read; T6 builds the standing
    backfill-progress metric on it.
    """
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM retrieval_chunks "
            f"WHERE org_id = %s AND {_STALE_MODEL_PREDICATE}",
            (org_id, embedding_model, embedding_model_version),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0
