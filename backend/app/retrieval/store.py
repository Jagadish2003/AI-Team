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

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app import db
from database.models.retrieval import ALL_RETRIEVAL_DDL, RetrievalChunkRecord

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


# ---------------------------------------------------------------------------
# Writes  (producers -> substrate; embedding pipeline stamps vectors)
# ---------------------------------------------------------------------------


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
            written += 1
        con.commit()
    return written


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
# Reads  (always org-scoped)
# ---------------------------------------------------------------------------


def search(
    org_id: str,
    query_vector: Sequence[float],
    k: int = 10,
    source_filter: Optional[Sequence[str]] = None,
    min_score: Optional[float] = None,
    embedding_model: Optional[str] = None,
    embedding_model_version: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return this org's most-similar chunks to ``query_vector`` (cosine similarity).

    HARD org partition: the ``WHERE`` clause always leads with ``org_id = %s`` — a
    query can only ever match the caller's own partition (AC3).

    ``source_filter``  scopes results to named source systems ('only Confluence',
                       'only this repo') via ``source_system = ANY(...)`` (AC4).
    ``min_score``      excludes weak matches below the cosine-similarity floor
                       (AC4).
    ``embedding_model`` / ``embedding_model_version`` restrict the search to
                       vectors produced by a specific model, so vectors from
                       different models are never compared (AC8). ``retrieve()``
                       (T4) passes the active model here.

    Only embedded rows participate (``embedding IS NOT NULL``): un-embedded
    content is absent from retrieval, never an error (AC7). Returns raw row dicts
    each carrying a ``similarity`` score; ``api.retrieve()`` maps these to
    ``RetrievedChunk`` and mints the per-result ``retrieval_result_id``.
    """
    literal = _to_vector_literal(query_vector)

    where = ["org_id = %s", "embedding IS NOT NULL"]
    params: list[Any] = [org_id]
    if source_filter:
        where.append("source_system = ANY(%s)")
        params.append(list(source_filter))
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
        "       embedding_model, embedding_model_version, "
        "       (1 - (embedding <=> %s::vector)) AS similarity "
        "FROM retrieval_chunks "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY embedding <=> %s::vector "
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
