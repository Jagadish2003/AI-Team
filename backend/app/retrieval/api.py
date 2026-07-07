"""Source-aware retrieval API — the ``retrieve()`` entry point (R18-B1).

``retrieve(org_id, query_text, k, source_filter, min_score)`` performs org-scoped
semantic retrieval: it embeds the query through the model gateway, searches ONLY
the querying org's partition, and returns ranked chunks with similarity scores and
full provenance (Section 1).

Source-aware means callers can scope to source systems ('only Confluence', 'only
this repo'). Each result carries the ``EvidencePointer`` fields (R16-B1) —
``chunk_id`` and ``retrieval_result_id`` — populating the pointer fields the 1.6
spine deliberately left null (AC5).

Retrieval PROPOSES candidates; the context assembler DECIDES what enters context.
This module never feeds enrichment directly — the one path in is through
``assemble_context(evidence_source=...)`` (Section 2 / AC6), which is wired in T6.

Scope (T1): this module defines the ``RetrievedChunk`` contract and the
``retrieve()`` signature/behaviour on top of the org-partitioned store. The full
ranking policy, min-score/telemetry refinements, and the assembly evidence-source
adapter are completed in T4/T6; the org-scoping and return contract are correct as
of T1.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional
from uuid import uuid4

from app.retrieval import embedder, store

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    """A ranked retrieval result with similarity and full provenance.

    Field set matches Section 1 exactly. ``chunk_id`` and ``retrieval_result_id``
    are the EvidencePointer fields (R16-B1) the 1.6 spine left null; the assembler
    (T6) carries them onto the evidence it admits.
    """

    content: str
    similarity: float
    source_system: str          # 'confluence', 'slack', 'git', 'document'
    source_artifact: str        # page id / thread id / file path
    chunk_id: str               # -> EvidencePointer.chunk_id
    retrieval_result_id: str    # -> EvidencePointer.retrieval_result_id
    source_timestamp: str


def retrieve(
    org_id: str,
    query_text: str,
    k: int = 10,
    source_filter: Optional[List[str]] = None,
    min_score: Optional[float] = None,
) -> List[RetrievedChunk]:
    """Org-scoped semantic retrieval.

    Embeds the query via the gateway, searches ONLY this org's partition, and
    returns ranked chunks each carrying: content, similarity, source_system,
    source_artifact, chunk_id, retrieval_result_id, source_timestamp.

    ``source_filter`` scopes to named source systems; ``min_score`` drops weak
    matches (AC4). The search is confined to vectors from the active embedding
    model so vectors from different models are never compared (AC8). If the query
    cannot be embedded (gateway graceful failure), an empty list is returned — a
    retrieval miss is never a crash.
    """
    if not query_text or not query_text.strip():
        return []

    vectors = embedder.embed_texts([query_text])
    if not vectors:
        # Gateway degraded (no provider / failure). No candidates — never raise.
        logger.debug("retrieve: query embedding unavailable; returning no candidates")
        return []
    query_vector = vectors[0]

    # AC8: restrict to the active embedding model's vectors. T3 stamps every stored
    # vector with the same (identity, version) pair resolved here, so filtering on
    # BOTH ensures a query never compares against vectors from a different model or
    # model version. Resolved once so the provider is not looked up twice.
    model_identity, model_version = embedder.active_embedding_model()
    rows = store.search(
        org_id=org_id,
        query_vector=query_vector,
        k=k,
        source_filter=source_filter,
        min_score=min_score,
        embedding_model=model_identity or None,
        embedding_model_version=model_version or None,
    )

    results: List[RetrievedChunk] = []
    for row in rows:
        ts = row.get("source_timestamp")
        results.append(
            RetrievedChunk(
                content=row["content"],
                similarity=float(row["similarity"]),
                source_system=row["source_system"],
                source_artifact=row["source_artifact"],
                chunk_id=row["chunk_id"],
                # One retrieval-result id per (query, chunk) hit — the pointer the
                # 1.6 spine left null (AC5). Minted per call, never stored.
                retrieval_result_id=str(uuid4()),
                source_timestamp=ts.isoformat() if hasattr(ts, "isoformat") else (ts or ""),
            )
        )
    return results
