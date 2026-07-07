"""Retrieval-substrate storage model (R18-B1 — Retrieval Substrate, T1).

The pgvector-backed vector store that the whole of Release 1.8 retrieval builds
on. This module is the SINGLE SOURCE OF TRUTH for the ``retrieval_chunks`` schema:

  * ``migrations/versions/0024_create_retrieval_chunks.py`` (the CI gate) imports
    ``ALL_RETRIEVAL_DDL`` from here.
  * the runtime ``app.retrieval.store.ensure_retrieval_store()`` helper executes
    the exact same statements.

so the migration-applied schema and the runtime-created schema can never drift —
the same no-drift pattern used by ``database/models/entities.py`` /
``0003_create_entities.py``.

Section 1 (Architecture) of the story fixes the store's shape:

  * pgvector in the EXISTING PostgreSQL — no new infrastructure component for
    on-prem installs. The vector column uses the ``vector`` type from the
    pgvector extension.
  * ONE logical index, HARD-PARTITIONED by ``org_id``: every query is org-scoped
    at the SQL layer, consistent with R17-D3 tenant isolation. ``org_id`` is a
    mandatory column and every read/write in ``store.py`` binds it — one
    customer's indexed content is never retrievable by another, even when the
    query text is similar (AC3).
  * each record stores the vector PLUS the chunk metadata and provenance-pointer
    fields needed to explain where the content came from: source system, source
    artifact, source timestamp, content type, chunk position, and a content hash
    (the hash is what freshness — R18-B2 — uses to detect change).
  * the embedding model identity + version are stamped PER VECTOR. A model change
    invalidates compatibility — vectors from different models are never compared
    (AC8). These columns are nullable because content is indexed before it is
    embedded: embedding is asynchronous with respect to discovery runs and
    un-embedded content is simply not-yet-retrievable, never a run failure (AC7).

The chunk primary key ``chunk_id`` and the per-retrieval ``retrieval_result_id``
populate the ``EvidencePointer`` fields the 1.6 spine deliberately left null
(AC5) — ``retrieval_result_id`` is minted per retrieval call and is NOT stored
here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


# Content-type chunk policies are owned by T2 (chunking.py); the store only needs
# to persist the resolved content_type string. Kept as a frozenset here so the DDL
# CHECK constraint and the Python-side validation can never disagree — the same
# derivation entities.py uses for its enum columns.
CONTENT_TYPES = frozenset({"prose", "conversation", "code"})

# Source systems that feed the substrate this release (Section 3). Not enforced by
# a CHECK constraint — new producers (SharePoint, Teams, …) must be able to write
# without a migration — but centralised here for callers and tests.
KNOWN_SOURCE_SYSTEMS = frozenset(
    {"document", "git", "confluence", "sharepoint", "slack", "teams"}
)

# sha256 hex digest length — the fixed width of the content_hash column.
CONTENT_HASH_LEN = 64

# Max stored length for the free-text source_artifact (page id / thread id / file
# path). File paths and Confluence page ids stay well within this; bounding it
# keeps the (org_id, source_artifact) index inside PostgreSQL's btree row limit.
SOURCE_ARTIFACT_MAX_LEN = 1024

_CONTENT_TYPE_CHECK = ", ".join(f"'{t}'" for t in sorted(CONTENT_TYPES))


def compute_content_hash(content: str) -> str:
    """Return the sha256 hex digest of ``content`` (the freshness key, R18-B2).

    Defined alongside the schema so producers, the store, and freshness detection
    all hash content identically. UTF-8 encoded; a change in the source text
    yields a different hash, which is exactly how R18-B2 detects staleness.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DDL — single source of truth (imported by the migration and the ensure helper)
# ---------------------------------------------------------------------------

# pgvector lives in the EXISTING PostgreSQL (Section 6: "pgvector, not a new
# component"). The extension must exist before a ``vector`` column can be created.
# ``IF NOT EXISTS`` makes this idempotent for both the migration and the runtime
# ensure helper.
CREATE_VECTOR_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector"

# The embedding column is declared as an unqualified ``vector`` (no fixed
# dimension) on purpose: the embedding model — and therefore the vector
# dimension — is a per-deployment decision made through the model gateway
# (R16-D1), and vectors from different models are never compared (AC8), so the
# store must hold whatever dimension the active model emits. A dimensioned ANN
# index (HNSW / IVFFLAT) is deliberately deferred to the retrieval-API task (T4),
# which pins the dimension for a deployment; org-scoped correctness does not
# depend on it — every query is bounded by ``WHERE org_id = %s`` first.
CREATE_RETRIEVAL_CHUNKS_TABLE = f"""
    CREATE TABLE IF NOT EXISTS retrieval_chunks (
        chunk_id                VARCHAR(36)    NOT NULL PRIMARY KEY,
        org_id                  VARCHAR(64)    NOT NULL,
        content                 TEXT           NOT NULL,
        content_hash            VARCHAR({CONTENT_HASH_LEN})  NOT NULL,
        content_type            VARCHAR(32)    NOT NULL CHECK (content_type IN ({_CONTENT_TYPE_CHECK})),
        source_system           VARCHAR(64)    NOT NULL,
        source_artifact         VARCHAR({SOURCE_ARTIFACT_MAX_LEN}) NOT NULL,
        source_timestamp        TIMESTAMP,
        chunk_position          INTEGER        NOT NULL DEFAULT 0,
        embedding               vector,
        embedding_model         VARCHAR(128),
        embedding_model_version VARCHAR(64),
        embedded_at             TIMESTAMP,
        provenance              TEXT,
        created_at              TIMESTAMP      NOT NULL,
        updated_at              TIMESTAMP      NOT NULL
    )
"""

# Hard org partition + source scoping: leads with org_id so both the tenant
# isolation predicate (WHERE org_id = %s) and source_filter (AND source_system =
# ANY(...)) are index-served.
CREATE_RETRIEVAL_IDX_ORG_SOURCE = """
    CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_org_source
        ON retrieval_chunks (org_id, source_system)
"""

# Freshness (R18-B2) looks content up by (org, hash) to detect change / dedupe
# re-ingested artifacts.
CREATE_RETRIEVAL_IDX_ORG_HASH = """
    CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_org_hash
        ON retrieval_chunks (org_id, content_hash)
"""

# Re-ingesting an artifact replaces its chunks; this index serves the delete /
# lookup-by-artifact path the producer contract (T5) uses.
CREATE_RETRIEVAL_IDX_ORG_ARTIFACT = """
    CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_org_artifact
        ON retrieval_chunks (org_id, source_artifact)
"""

ALL_RETRIEVAL_DDL: tuple[str, ...] = (
    CREATE_VECTOR_EXTENSION,
    CREATE_RETRIEVAL_CHUNKS_TABLE,
    CREATE_RETRIEVAL_IDX_ORG_SOURCE,
    CREATE_RETRIEVAL_IDX_ORG_HASH,
    CREATE_RETRIEVAL_IDX_ORG_ARTIFACT,
)

# DROP order for downgrade() — indexes, then table. The extension is intentionally
# NOT dropped: other objects may come to depend on it and dropping a shared
# extension on a store rollback is unsafe.
DROP_RETRIEVAL_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_retrieval_chunks_org_artifact",
    "DROP INDEX IF EXISTS idx_retrieval_chunks_org_hash",
    "DROP INDEX IF EXISTS idx_retrieval_chunks_org_source",
    "DROP TABLE IF EXISTS retrieval_chunks",
)


@dataclass
class RetrievalChunkRecord:
    """One stored, indexable chunk — the vector plus its full provenance.

    This is the write-side record persisted by ``store.upsert_chunks()``. The
    read-side, ranked-with-similarity shape returned to callers is
    ``app.retrieval.api.RetrievedChunk``.

    ``embedding`` / ``embedding_model`` / ``embedding_model_version`` /
    ``embedded_at`` are ``None`` until the embedding pipeline (T3) processes the
    chunk. Un-embedded records are stored and provenance-complete but are absent
    from retrieval until embedded (AC7).
    """

    org_id: str
    content: str
    content_type: str
    source_system: str
    source_artifact: str
    chunk_position: int = 0
    source_timestamp: Optional[datetime] = None
    chunk_id: str = field(default_factory=lambda: str(uuid4()))
    content_hash: Optional[str] = None
    embedding: Optional[list[float]] = None
    embedding_model: Optional[str] = None
    embedding_model_version: Optional[str] = None
    embedded_at: Optional[datetime] = None
    provenance: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for fname in ("org_id", "content", "content_type", "source_system", "source_artifact"):
            val = getattr(self, fname)
            if val is None or not str(val).strip():
                raise ValueError(f"{fname} is required")
        if self.content_type not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {sorted(CONTENT_TYPES)}")
        if len(self.source_artifact) > SOURCE_ARTIFACT_MAX_LEN:
            raise ValueError(
                f"source_artifact exceeds {SOURCE_ARTIFACT_MAX_LEN} characters"
            )
        # The content hash is derived from content, never supplied by the caller,
        # so freshness (R18-B2) always compares like for like.
        self.content_hash = compute_content_hash(self.content)

    def to_db_row(self) -> dict[str, Any]:
        """Return a column->value mapping for persistence.

        ``embedding`` is left as a Python list; ``store.py`` formats it into the
        pgvector literal at insert time so this module carries no DB-driver
        concern.
        """
        return {
            "chunk_id": self.chunk_id,
            "org_id": self.org_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "content_type": self.content_type,
            "source_system": self.source_system,
            "source_artifact": self.source_artifact,
            "source_timestamp": self.source_timestamp,
            "chunk_position": self.chunk_position,
            "embedding": self.embedding,
            "embedding_model": self.embedding_model,
            "embedding_model_version": self.embedding_model_version,
            "embedded_at": self.embedded_at,
            "provenance": json.dumps(self.provenance) if self.provenance is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
