"""Embedding pipeline — async, batched, gateway-only (R18-B1 T3).

Embeddings are model calls. The boundary rule applies in full: **every** embedding
is produced through the model gateway's embedding provider
(``get_embedding_provider()`` / the gateway's instrumented ``embed()``), so
in-boundary and customer-tenant deployments embed inside the customer's control
automatically (R16-D1). This module makes NO direct embedding-API call — any such
call anywhere outside ``app/model_gateway/`` is a build-breaking violation of the
R16-D1 no-bypass test (T7 / AC2), and for a sovereignty customer a data breach.

What T3 delivers on top of the T1 skeleton:

* **Batching** — pending chunks are embedded in bounded batches
  (``embed_pending_for_org``) rather than one call per chunk, so the pipeline is
  efficient and observable. Batch size is bounded so a batch never exceeds what a
  provider will accept.
* **Asynchronous with respect to discovery runs (AC7)** — content is indexed by
  producers WITHOUT a vector and embedded later, off the run path, by this
  pipeline (driven by ``jobs/embedding_worker.py``). Embedding lag never blocks a
  run; un-embedded content is simply not-yet-retrievable, never a run failure.
  Every entry point here is defensive: a gateway outage, a partial response, or a
  DB error degrades to "left pending, retried next cycle" — it never raises into a
  run.
* **Model identity + version stamped per vector (AC8)** — the active embedding
  model's ``(identity, version)`` is resolved ONCE per batch run from the gateway
  and stamped onto every vector it writes, so retrieval can refuse to compare
  vectors produced by different models.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.model_gateway import embed as _gateway_embed
from app.model_gateway import get_embedding_provider
from app.retrieval import store

logger = logging.getLogger(__name__)

# Default number of chunks embedded per gateway call. Bounded so a single batch
# stays within what an embedding provider will accept in one request; overridable
# per deployment without a code change. Clamped to >= 1.
_DEFAULT_BATCH_SIZE = 64


def _batch_size() -> int:
    """Resolve the embedding batch size live from config (min 1)."""
    try:
        return max(1, int(os.getenv("RETRIEVAL_EMBED_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))))
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_SIZE


# ---------------------------------------------------------------------------
# Active embedding model identity + version  (AC8)
# ---------------------------------------------------------------------------


def active_embedding_model() -> Tuple[str, str]:
    """Return the active embedding provider's ``(identity, version)`` (AC8).

    This is the pair stamped onto every vector the pipeline writes and the pair
    ``api.retrieve()`` filters the search by — so a query only ever compares
    against vectors produced by the *same* embedding model. Resolved live from the
    gateway so a provider/model repin is picked up without a restart.

    Never raises: an unexpected provider error degrades to ``("", "")`` so the
    embedding path (which must never block a run) stays alive.
    """
    try:
        return get_embedding_provider().embedding_identity()
    except Exception:  # noqa: BLE001 — identity lookup must never break the pipeline
        logger.warning("embedder: could not resolve embedding model identity", exc_info=True)
        return ("", "")


def embedding_model_identity() -> str:
    """Return the active embedding model identity string (stamped per vector, AC8).

    The identity component of :func:`active_embedding_model`. ``api.retrieve()``
    passes this to the store so a query is scoped to vectors from the active model.
    """
    return active_embedding_model()[0]


def embedding_model_version() -> str:
    """Return the active embedding model version string (stamped per vector, AC8)."""
    return active_embedding_model()[1]


# ---------------------------------------------------------------------------
# Batch embedding through the gateway (the ONLY embedding entry point)
# ---------------------------------------------------------------------------


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of strings through the gateway. One vector per input.

    Routes through the gateway's instrumented ``embed()`` so the call is observable
    and honours the active provider (hosted / in-boundary / customer-tenant). The
    gateway degrades gracefully — it returns an empty list on failure — so callers
    treat a short/empty result as "not embedded yet", never a crash: embedding lag
    must never block a run (AC7).
    """
    if not texts:
        return []
    return _gateway_embed(texts)


# ---------------------------------------------------------------------------
# Async batch pipeline: embed the pending backlog, stamp each vector (AC7/AC8)
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingRunResult:
    """Outcome of an embedding pass — for logging, the worker, and tests.

    ``embedded`` counts vectors successfully stamped; ``pending_seen`` counts
    chunks that were candidates this pass; ``batches`` counts gateway calls made.
    ``model_identity`` / ``model_version`` are the stamp applied this pass.
    """

    org_id: str
    pending_seen: int = 0
    embedded: int = 0
    batches: int = 0
    model_identity: str = ""
    model_version: str = ""


def embed_pending_for_org(
    org_id: str,
    *,
    batch_size: Optional[int] = None,
    max_chunks: Optional[int] = None,
) -> EmbeddingRunResult:
    """Embed this org's not-yet-embedded chunks in batches, stamping each vector.

    The core of T3. Selects ``embedding IS NULL`` rows for ``org_id``
    (``store.fetch_unembedded``), embeds them in bounded batches through the
    gateway, and stamps every resulting vector with the active model's identity +
    version (``store.set_embedding``) so vectors from different models are never
    compared (AC8). Content that cannot be embedded this pass is simply left
    pending and picked up on the next pass.

    Asynchronous-with-runs contract (AC7): this NEVER raises. A gateway outage
    (empty result), a partial batch, or a per-row DB error is logged and the
    affected chunks stay pending — embedding lag is never a run failure. Returns an
    :class:`EmbeddingRunResult` describing what happened.

    ``batch_size``  overrides the configured gateway batch size for this pass.
    ``max_chunks``  caps how many chunks this pass processes (the worker uses it to
                    bound one tick); ``None`` drains the whole backlog for the org.
    """
    result = EmbeddingRunResult(org_id=org_id)
    try:
        size = batch_size if batch_size and batch_size > 0 else _batch_size()
        identity, version = active_embedding_model()
        result.model_identity, result.model_version = identity, version
        if not identity:
            # No usable embedding provider/model — leave everything pending. A
            # vector with no model stamp could never be safely compared (AC8).
            logger.info(
                "embedder: no embedding model identity available; leaving org %s "
                "content pending", org_id,
            )
            return result

        remaining = max_chunks
        while remaining is None or remaining > 0:
            fetch_n = size if remaining is None else min(size, remaining)
            pending = store.fetch_unembedded(org_id, limit=fetch_n)
            if not pending:
                break
            result.pending_seen += len(pending)
            stamped = _embed_and_stamp_batch(org_id, pending, identity, version)
            result.embedded += stamped
            result.batches += 1
            if remaining is not None:
                remaining -= len(pending)
            # Stop unless the whole page was fully stamped. A short page means the
            # backlog is drained; a page that was NOT fully stamped (gateway
            # outage/partial batch/per-row error) has left those rows pending — the
            # next fetch would return the SAME rows, so we must not loop on them in
            # this pass. Leave them for the next worker tick. Only a full page that
            # was fully embedded implies more may remain, so continue.
            if stamped < len(pending) or len(pending) < fetch_n:
                break
    except Exception:  # noqa: BLE001 — embedding must never block a run (AC7)
        logger.warning(
            "embedder: embedding pass for org %s failed; content stays pending",
            org_id,
            exc_info=True,
        )
    return result


def _embed_and_stamp_batch(
    org_id: str,
    pending: List[dict],
    identity: str,
    version: str,
) -> int:
    """Embed one batch and stamp each returned vector. Returns vectors stamped.

    Alignment is positional: the gateway returns one vector per input text in
    order. If the gateway degrades (empty list) or returns a different count than
    it was given, the whole batch is treated as "not embedded yet" and left
    pending — a mis-aligned vector would attach the wrong content to a chunk, which
    is far worse for retrieval than a short delay (AC7 favours correctness over
    latency here). Per-row stamp failures are isolated so one bad row cannot strand
    the rest of the batch.
    """
    texts = [row["content"] for row in pending]
    vectors = embed_texts(texts)
    if len(vectors) != len(texts):
        logger.info(
            "embedder: gateway returned %d vectors for %d texts (org %s); "
            "leaving this batch pending",
            len(vectors), len(texts), org_id,
        )
        return 0

    stamped = 0
    for row, vector in zip(pending, vectors):
        try:
            if store.set_embedding(
                chunk_id=row["chunk_id"],
                org_id=org_id,
                vector=vector,
                embedding_model=identity,
                embedding_model_version=version,
            ):
                stamped += 1
        except Exception:  # noqa: BLE001 — isolate per-row failures
            logger.warning(
                "embedder: failed to stamp chunk %s (org %s); left pending",
                row.get("chunk_id"), org_id, exc_info=True,
            )
    return stamped


def embed_pending_all_orgs(
    *,
    batch_size: Optional[int] = None,
    max_chunks_per_org: Optional[int] = None,
    max_orgs: int = 100,
) -> List[EmbeddingRunResult]:
    """Drain the pending backlog for every org that has one (background worker).

    Enumerates the orgs with un-embedded content (``store.orgs_with_unembedded``)
    and runs :func:`embed_pending_for_org` for each — every content read stays
    org-scoped (AC3). Per-org failures are isolated by ``embed_pending_for_org``
    (which never raises), so one tenant's backlog can never stall another's.
    Returns one :class:`EmbeddingRunResult` per org processed.
    """
    results: List[EmbeddingRunResult] = []
    try:
        orgs = store.orgs_with_unembedded(limit=max_orgs)
    except Exception:  # noqa: BLE001 — a scan error must not crash the worker
        logger.warning("embedder: could not enumerate orgs with pending chunks", exc_info=True)
        return results

    for org_id in orgs:
        results.append(
            embed_pending_for_org(
                org_id, batch_size=batch_size, max_chunks=max_chunks_per_org
            )
        )
    return results
