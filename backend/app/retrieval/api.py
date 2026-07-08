"""Source-aware retrieval API — the ``retrieve()`` entry point (R18-B1 T4).

``retrieve(org_id, query_text, k, source_filter, min_score)`` is the platform API
the rest of AgentIQ calls to get evidence out of the vector store. It embeds the
query through the model gateway, searches ONLY the querying org's partition, and
returns ranked chunks with similarity scores and full provenance — the caller
never has to know pgvector exists (Section 1).

What T4 completes on top of the T1 skeleton:

* **Org-scoped** — the search is hard-partitioned by ``org_id`` at the SQL layer
  (``store.search``); a query can only ever match the caller's own partition (AC3).
* **Source-aware** — ``source_filter`` scopes results to named source systems
  ('only Confluence', 'only Slack', 'only this repo'); the filter is normalised
  (trimmed, de-duplicated) so callers get predictable behaviour, and an explicit
  scope that names nothing valid returns nothing rather than silently widening (AC4).
* **min-score** — ``min_score`` drops weak matches below a cosine-similarity floor
  so low-quality evidence never enters the downstream reasoning flow (AC4).
* **Ranked, reproducible** — results come back best-first (descending similarity)
  with a deterministic tie-break in the store, capped at ``k``.
* **EvidencePointer fields (AC5)** — each result carries ``chunk_id`` and
  ``retrieval_result_id``, the two extensible-detail fields the R16-B1 spine
  shipped present-but-null in 1.6; :meth:`RetrievedChunk.to_evidence_pointer`
  finally fills them, with no schema change.
* **Observable** — every call emits one ``retrieval.query_completed`` telemetry
  event (counts and filter shape only, never the query text or content).

Retrieval PROPOSES candidates; the context assembler DECIDES what enters context.
This module never feeds enrichment directly — the one path in is through
``assemble_context(evidence_source=...)`` (Section 2 / AC6), wired in T6.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional
from uuid import uuid4

from app.provenance import EvidencePointer
from app.retrieval import embedder, store

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    """A ranked retrieval result with similarity and full provenance.

    Field set matches Section 1 exactly. ``chunk_id`` and ``retrieval_result_id``
    are the EvidencePointer fields (R16-B1) the 1.6 spine left null;
    :meth:`to_evidence_pointer` carries them onto an observed pointer (AC5), and
    the assembler (T6) admits that pointer as evidence.
    """

    content: str
    similarity: float
    source_system: str          # 'confluence', 'slack', 'git', 'document'
    source_artifact: str        # page id / thread id / file path
    chunk_id: str               # -> EvidencePointer.chunk_id
    retrieval_result_id: str    # -> EvidencePointer.retrieval_result_id
    source_timestamp: str

    def to_evidence_pointer(self) -> EvidencePointer:
        """Build the R16-B1 provenance pointer for this retrieved chunk (AC5).

        Retrieved content was seen directly in a source, so the pointer is
        ``observed``. This is where ``chunk_id`` and ``retrieval_result_id`` — the
        two fields the spine has carried as null since 1.6 — are finally populated,
        with no schema change. The similarity is passed as the pointer confidence
        so downstream consumers (the assembler, T6) can weight the evidence.
        """
        return EvidencePointer.retrieved(
            source_system=self.source_system,
            source_artifact=self.source_artifact,
            chunk_id=self.chunk_id,
            retrieval_result_id=self.retrieval_result_id,
            source_timestamp=self.source_timestamp or None,
            confidence=self.similarity,
        )


def _normalize_source_filter(
    source_filter: Optional[List[str]],
) -> Optional[List[str]]:
    """Normalise a caller's source filter into a clean, ordered, unique list.

    Returns ``None`` when no filter was supplied (unscoped search). Otherwise trims
    whitespace, drops empty entries, and de-duplicates while sorting for
    determinism. A filter that was supplied but names nothing valid normalises to
    an empty list — the caller explicitly scoped to no source, which must return
    no results rather than silently widening to all sources.
    """
    if source_filter is None:
        return None
    cleaned = sorted({s.strip() for s in source_filter if s and s.strip()})
    return cleaned


def retrieve(
    org_id: str,
    query_text: str,
    k: int = 10,
    source_filter: Optional[List[str]] = None,
    min_score: Optional[float] = None,
) -> List[RetrievedChunk]:
    """Org-scoped, source-aware semantic retrieval — the platform retrieval API.

    Embeds the query via the gateway, searches ONLY ``org_id``'s partition, and
    returns up to ``k`` ranked chunks (best similarity first), each carrying:
    content, similarity, source_system, source_artifact, chunk_id,
    retrieval_result_id, source_timestamp.

    ``source_filter``  scopes to named source systems ('only Confluence', 'only
                       this repo'); normalised and de-duplicated. An explicit
                       filter naming nothing valid returns ``[]`` (AC4).
    ``min_score``      excludes matches below this cosine-similarity floor so weak
                       evidence never reaches the reasoning flow (AC4).

    The search is confined to the active embedding model's vectors so vectors from
    different models are never compared (AC8). Never raises: an empty/blank query,
    a non-positive ``k``, a scoped-to-nothing filter, or a gateway embedding miss
    all return ``[]`` — a retrieval miss is a normal outcome, never a crash.
    """
    if not org_id or not query_text or not query_text.strip():
        return []
    # k <= 0 is a request for no results; return early without touching the store.
    if k <= 0:
        return []

    sources = _normalize_source_filter(source_filter)
    if sources is not None and not sources:
        # Caller explicitly scoped to source systems but named none valid.
        _emit_query_telemetry(org_id, k, 0, sources, min_score, query_embedded=True)
        return []

    vectors = embedder.embed_texts([query_text])
    if not vectors:
        # Gateway degraded (no provider / failure). No candidates — never raise.
        logger.debug("retrieve: query embedding unavailable; returning no candidates")
        _emit_query_telemetry(org_id, k, 0, sources, min_score, query_embedded=False)
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
        source_filter=sources,
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

    _emit_query_telemetry(org_id, k, len(results), sources, min_score, query_embedded=True)
    return results


def _emit_query_telemetry(
    org_id: str,
    k: int,
    result_count: int,
    sources: Optional[List[str]],
    min_score: Optional[float],
    *,
    query_embedded: bool,
) -> None:
    """Emit one ``retrieval.query_completed`` event. Never raises.

    Counts and filter shape only — never the query text, the chunk content, or the
    vectors (PII guard). Imported lazily and fully guarded so a telemetry problem
    can never break a retrieval call.
    """
    try:
        from app.telemetry import record_event

        payload: dict = {
            "org_id": org_id,
            "k": k,
            "result_count": result_count,
            "query_embedded": query_embedded,
        }
        if sources is not None:
            payload["source_filter"] = sources
        if min_score is not None:
            payload["min_score"] = float(min_score)
        record_event("retrieval.query_completed", payload)
    except Exception:  # pragma: no cover - telemetry is best-effort
        logger.debug("retrieve: telemetry emit failed", exc_info=True)
