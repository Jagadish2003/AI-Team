"""AgentIQ retrieval substrate (R18-B1 — Release 1.8, Track B).

The layer that finds the few most relevant pieces of evidence on demand —
chunking, embedding, indexing, and source-aware retrieval — so the system can
reason over large unstructured content without being overwhelmed. Keystone of
Release 1.8: every deep-content story consumes this.

Package layout (Section 1):

    chunking.py         - per-content-type chunk policies (+ content hash / provenance)
    embedder.py         - batch embedding via the model gateway ONLY
    store.py            - pgvector index, org-partitioned
    api.py              - retrieve() entry point + RetrievedChunk
    ingest.py           - ingest_content() producer contract (T5)
    evidence_source.py  - retrieval-backed evidence source for assemble_context (T6)
    freshness.py        - ingestion.artifact_changed subscriber (R18-B2 T1)
    refresh_queue.py    - durable refresh work list for the async worker (R18-B2)

Two load-bearing invariants hold across the package:

  * Embeddings are model calls — produced ONLY through the model gateway
    (R16-D1 no-bypass). No direct embedding-API call lives here.
  * Every store access is hard-partitioned by org_id at the SQL layer (R17-D3):
    one org's indexed content is never retrievable by another.

Retrieval PROPOSES candidates; the context assembler DECIDES what enters context.
The substrate never feeds enrichment directly — the one path into opportunity
context is ``assemble_context(evidence_source=retrieval_evidence_source(org_id))``
(T6), where the assembler's confidence floor, observed-first ordering, hard caps,
and selection log all apply to retrieved chunks.

Content enters through ONE producer contract: ``ingest.ingest_content(org_id,
artifacts)``. Producers hand extracted text + provenance; the substrate owns
chunking, hashing, embedding, indexing, and metadata storage. Producers never
write vectors directly.
"""
from __future__ import annotations

# Kept import-light on purpose: submodules (store/api) pull in the DB and model
# gateway, so importing the package must not force those in. Callers import the
# submodule they need, e.g. ``from app.retrieval.api import retrieve``.

__all__ = [
    "chunking",
    "embedder",
    "store",
    "api",
    "ingest",
    "freshness",
    "refresh_queue",
]
