"""Async refresh worker — re-embed only what changed, swap atomically (R18-B2 T3).

When a source artifact is created or updated, the freshness subscriber (T1) marks
its indexed chunks stale and ENQUEUES the artifact on the ``retrieval_refresh_queue``
(``refresh_queue.py``). This module is the worker that DRAINS that queue, off the
discovery-run path, and brings the artifact's chunks back up to date. Per Section 1
(``refresh_worker``) the sequence for one artifact is:

    re-extract  ->  re-chunk  ->  hash-compare  ->  re-embed only what changed
                ->  swap chunks atomically  ->  clear stale

Each step maps to a concrete guarantee in the story:

* **Re-extract** — the substrate is push-based (producers hand text to
  ``ingest_content``); it does not reach into connectors itself. So re-extraction
  goes through a producer-registered CONTENT RESOLVER (:func:`register_content_resolver`),
  the same callable-injection pattern ``evidence_source`` uses. A resolver takes
  ``(org_id, source_artifact)`` and returns the artifact's CURRENT content as a
  ``ContentArtifact`` (or ``None`` if it cannot be fetched right now). With no
  resolver for a source system, the artifact is left queued and its chunks stay
  stale — never served as current — until one is available (freshness is allowed
  to lag; it is never allowed to be wrong).
* **Re-chunk** — through the SAME ``ingest.build_records`` path producers use, so
  re-chunked content hashes are directly comparable to the stored ones.
* **Hash-compare (AC3)** — the chunk content hash (stamped by R18-B1) is the cheap
  test for real change. A chunk whose hash matches an already-embedded stored chunk
  is UNCHANGED: its existing vector is carried straight over and it is NOT
  re-embedded. An update event whose text did not actually change therefore costs
  zero embedding calls — refresh cost stays proportional to real change, not event
  volume (Section 4).
* **Re-embed only what changed** — only the genuinely new/changed chunks are sent
  to the model gateway (via ``embedder``, the gateway-only path — R16-D1).
* **Atomic swap (AC4)** — the full new chunk set replaces the old set in ONE
  transaction (``store.swap_artifact_chunks``), so retrieval never returns a
  half-old/half-new mix for one artifact.
* **Clear stale — only after replacement** — the inserted rows take the ``is_stale``
  default (FALSE) inside that same commit, so the stale flag is cleared as an
  inseparable part of installing the new content. Until the swap commits, the old
  chunks stay stale and are excluded from default retrieval (T4) — never treated as
  current evidence.

Operational posture matches the embedding worker (R18-B1 T3): every entry point is
defensive and never raises into anything. A resolver error, a gateway outage, or a
DB error leaves the artifact queued (or parked ``failed`` after repeated attempts)
with its chunks still stale — the safe direction. Work is bounded per tick so no one
tenant monopolises the worker, and every read/write is org-scoped.

Scope note: this task (T3) delivers the refresh worker itself and its content-
resolver seam. Wiring a specific connector's re-extraction (registering its
resolver) belongs to that connector/producer, exactly as calling ``ingest_content``
does. The model-version invalidation + managed backfill is T5; the freshness
metrics endpoint is T6.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Union

from app.retrieval import embedder, refresh_queue, store
from app.retrieval.ingest import ContentArtifact, build_records

logger = logging.getLogger(__name__)

# A content resolver re-extracts one artifact's CURRENT content on demand. It is
# the seam between the push-based ingestion producers and the pull-based refresh
# worker: given the org and the artifact id, return the artifact's content now (as
# a ``ContentArtifact`` or an equivalent dict), or ``None`` if it cannot be fetched
# this moment (transient source outage — the artifact stays queued for retry).
ContentResolver = Callable[[str, str], Optional[Union[ContentArtifact, dict]]]

_CONTENT_RESOLVERS: Dict[str, ContentResolver] = {}

# How many queued artifacts one org is refreshed per worker tick by default, when
# the caller does not specify. Bounds the work per tick; the remainder drains on
# subsequent ticks (freshness is eventually-consistent).
_DEFAULT_MAX_ARTIFACTS = 64


def _max_attempts() -> int:
    """Resolve the max refresh attempts before an artifact is parked ``failed``."""
    try:
        return max(1, int(os.getenv("RETRIEVAL_REFRESH_MAX_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        return 5


# ---------------------------------------------------------------------------
# Content resolver registry — the re-extraction seam
# ---------------------------------------------------------------------------


def register_content_resolver(source_system: str, resolver: ContentResolver) -> None:
    """Register how to re-extract a source system's artifacts (T3).

    Producers/connectors call this (typically at startup) to teach the refresh
    worker how to fetch the CURRENT content of one of their artifacts. Without a
    resolver for a source system, that system's stale artifacts cannot be refreshed
    — they stay queued and their chunks stay stale (excluded from default retrieval)
    rather than being served as current. Last registration for a source wins.
    """
    if not source_system or not str(source_system).strip():
        raise ValueError("source_system is required to register a content resolver")
    if not callable(resolver):
        raise ValueError("resolver must be callable")
    _CONTENT_RESOLVERS[source_system] = resolver


def unregister_content_resolver(source_system: str) -> None:
    """Remove a source system's content resolver (no-op if absent)."""
    _CONTENT_RESOLVERS.pop(source_system, None)


def clear_content_resolvers() -> None:
    """Drop all registered content resolvers (test isolation helper)."""
    _CONTENT_RESOLVERS.clear()


def get_content_resolver(source_system: str) -> Optional[ContentResolver]:
    """Return the content resolver registered for a source system, or ``None``."""
    return _CONTENT_RESOLVERS.get(source_system)


# ---------------------------------------------------------------------------
# Refresh one artifact
# ---------------------------------------------------------------------------


@dataclass
class RefreshOutcome:
    """Result of refreshing one artifact.

    ``status`` is one of:
      * ``refreshed``      — content re-extracted and chunks swapped in atomically
                             (``chunks_total`` may be 0 if the source is now empty).
      * ``no_resolver``    — no content resolver for this source system; left queued.
      * ``no_content``     — the resolver could not provide content right now; the
                             artifact stays queued for retry.
      * ``resolver_error`` — the resolver raised; retried on a later tick.
      * ``error``          — unexpected failure; retried on a later tick.

    ``chunks_reused`` counts chunks whose hash was unchanged and whose stored vector
    was carried over WITHOUT re-embedding (AC3); ``chunks_reembedded`` counts chunks
    actually sent to the gateway this refresh.
    """

    org_id: str
    source_system: str
    source_artifact: str
    status: str
    chunks_total: int = 0
    chunks_reused: int = 0
    chunks_reembedded: int = 0
    chunks_removed: int = 0
    detail: Optional[str] = None


def refresh_artifact(
    org_id: str, source_system: str, source_artifact: str
) -> RefreshOutcome:
    """Re-extract, hash-compare, re-embed only changes, and swap atomically (T3).

    The whole Section-1 ``refresh_worker`` sequence for one artifact. Never raises:
    any failure returns a non-``refreshed`` :class:`RefreshOutcome` and leaves the
    artifact's chunks stale, so a problem can only ever DELAY freshness, never serve
    wrong evidence or break the worker.
    """
    try:
        resolver = get_content_resolver(source_system)
        if resolver is None:
            logger.debug(
                "refresh: no content resolver for source_system=%s; leaving "
                "artifact queued (org=%s artifact=%s)",
                source_system, org_id, source_artifact,
            )
            return RefreshOutcome(
                org_id, source_system, source_artifact, status="no_resolver"
            )

        try:
            raw = resolver(org_id, source_artifact)
        except Exception as exc:  # noqa: BLE001 — a resolver error must not crash the worker
            logger.warning(
                "refresh: content resolver failed (org=%s source_system=%s "
                "artifact=%s): %s",
                org_id, source_system, source_artifact, exc,
            )
            return RefreshOutcome(
                org_id, source_system, source_artifact,
                status="resolver_error", detail=str(exc),
            )

        if raw is None:
            return RefreshOutcome(
                org_id, source_system, source_artifact, status="no_content"
            )

        artifact = _resolved_artifact(raw, source_system, source_artifact)

        # Re-chunk through the SAME builder producers use, so hashes are comparable.
        new_records = build_records(org_id, artifact)

        # Hash-compare against what is currently indexed, carrying over the vectors
        # of unchanged chunks so they are never re-embedded (AC3).
        existing = store.get_chunks_for_artifact(org_id, source_system, source_artifact)
        identity, version = embedder.active_embedding_model()
        reusable = _reusable_by_hash(existing, identity, version)

        reused = 0
        changed_indexes: List[int] = []
        for idx, rec in enumerate(new_records):
            bucket = reusable.get(rec.content_hash)
            if bucket:
                match = bucket.pop(0)
                rec.chunk_id = match["chunk_id"]  # keep identity for unchanged content
                rec.embedding = match["embedding"]
                rec.embedding_model = match["embedding_model"]
                rec.embedding_model_version = match["embedding_model_version"]
                rec.embedded_at = match["embedded_at"]
                reused += 1
            else:
                changed_indexes.append(idx)

        # Re-embed ONLY the changed/new chunks, through the gateway (R16-D1). If the
        # gateway is unavailable or returns a mismatched count, the changed chunks
        # are left un-embedded and picked up by the embedding worker later — never a
        # failure of the refresh itself (AC7). Unchanged chunks are already carried.
        reembedded = _reembed_changed(
            new_records, changed_indexes, org_id, source_artifact, identity, version
        )

        # Atomic swap + clear stale, in one transaction (AC4).
        removed, written = store.swap_artifact_chunks(
            org_id, source_system, source_artifact, new_records
        )

        logger.info(
            "refresh: refreshed artifact (org=%s source_system=%s artifact=%s "
            "chunks=%d reused=%d reembedded=%d removed=%d)",
            org_id, source_system, source_artifact,
            written, reused, reembedded, removed,
        )
        return RefreshOutcome(
            org_id, source_system, source_artifact,
            status="refreshed",
            chunks_total=written,
            chunks_reused=reused,
            chunks_reembedded=reembedded,
            chunks_removed=removed,
        )
    except Exception as exc:  # noqa: BLE001 — the worker must never crash on one artifact
        logger.warning(
            "refresh: unexpected failure refreshing artifact (org=%s "
            "source_system=%s artifact=%s)",
            org_id, source_system, source_artifact, exc_info=True,
        )
        return RefreshOutcome(
            org_id, source_system, source_artifact, status="error", detail=str(exc)
        )


def _resolved_artifact(
    raw: Union[ContentArtifact, dict], source_system: str, source_artifact: str
) -> ContentArtifact:
    """Coerce a resolver's return value to a validated ``ContentArtifact``.

    The artifact's identifiers are forced to the ones we were asked to refresh, so a
    resolver can never (accidentally) cause chunks to be written under a different
    artifact key than the one being refreshed.
    """
    if isinstance(raw, ContentArtifact):
        artifact = raw
    elif isinstance(raw, dict):
        artifact = ContentArtifact.from_dict(raw)
    else:
        raise ValueError(
            f"content resolver must return a ContentArtifact or dict, "
            f"got {type(raw).__name__}"
        )
    artifact.source_system = source_system
    artifact.source_artifact = source_artifact
    return artifact


def _reusable_by_hash(
    existing: List[dict], identity: str, version: str
) -> Dict[str, list]:
    """Index already-embedded stored chunks by content hash for reuse (AC3).

    Only chunks that ALREADY have a vector are reusable — an un-embedded stored
    chunk has nothing to carry over, so a new chunk matching it is embedded now
    (which is a first embed, not a re-embed). When the active embedding model
    identity is known, a stored vector produced by a DIFFERENT model/version is not
    reused: it must be re-embedded under the active model (model-generation mixing
    is forbidden — AC5, whose managed backfill is T5). Returns hash -> list of
    matching rows so duplicate-content chunks each consume a distinct stored vector
    (and a distinct ``chunk_id``), never colliding on one.
    """
    reusable: Dict[str, list] = {}
    for row in existing:
        if row.get("embedding") is None:
            continue
        if identity and (
            row.get("embedding_model") != identity
            or row.get("embedding_model_version") != version
        ):
            continue
        reusable.setdefault(row["content_hash"], []).append(row)
    return reusable


def _reembed_changed(
    records: List,
    changed_indexes: List[int],
    org_id: str,
    source_artifact: str,
    identity: str,
    version: str,
) -> int:
    """Embed only the changed/new chunks through the gateway. Returns count embedded.

    Positional alignment: the gateway returns one vector per input text in order. A
    short/empty result (gateway degraded) or a count mismatch leaves those chunks
    un-embedded — the embedding worker finishes them later; a mis-aligned vector
    would attach the wrong content to a chunk, far worse than a short delay (AC7).
    With no active embedding model, nothing is embedded here and the changed chunks
    are left pending for the embedding worker.
    """
    if not changed_indexes or not identity:
        return 0
    texts = [records[i].content for i in changed_indexes]
    vectors = embedder.embed_texts(texts)
    if len(vectors) != len(texts):
        logger.info(
            "refresh: gateway returned %d vectors for %d changed chunk(s) "
            "(org=%s artifact=%s); leaving them for the embedding worker",
            len(vectors), len(texts), org_id, source_artifact,
        )
        return 0
    embedded_at = datetime.now(timezone.utc)
    embedded = 0
    for pos, idx in enumerate(changed_indexes):
        records[idx].embedding = vectors[pos]
        records[idx].embedding_model = identity
        records[idx].embedding_model_version = version
        records[idx].embedded_at = embedded_at
        embedded += 1
    return embedded


# ---------------------------------------------------------------------------
# Drain the queue — per org, then all orgs (the background worker entry point)
# ---------------------------------------------------------------------------


@dataclass
class RefreshRunResult:
    """Outcome of one refresh pass over an org's queued artifacts (for the worker)."""

    org_id: str
    processed: int = 0
    refreshed: int = 0
    reused_chunks: int = 0
    reembedded_chunks: int = 0
    failed: int = 0
    skipped: int = 0  # left queued (no resolver / no content this tick)


def refresh_pending_for_org(
    org_id: str, *, max_artifacts: Optional[int] = None
) -> RefreshRunResult:
    """Refresh one page of an org's queued artifacts (oldest first). Never raises.

    Fetches up to ``max_artifacts`` (default :data:`_DEFAULT_MAX_ARTIFACTS`) pending
    rows and refreshes each. A successful refresh removes the queue row
    (``mark_done``); a transient failure is recorded and retried next tick, parking
    the row ``failed`` only after ``RETRIEVAL_REFRESH_MAX_ATTEMPTS`` attempts
    (``mark_failed``); a missing resolver / unavailable content leaves the row
    queued. Only ONE page is processed per call — the worker's interval drains the
    rest, so a large backlog never blocks a tick and skipped rows never loop within
    a single pass.
    """
    result = RefreshRunResult(org_id=org_id)
    try:
        limit = max_artifacts if (max_artifacts and max_artifacts > 0) else _DEFAULT_MAX_ARTIFACTS
        pending = refresh_queue.fetch_pending(org_id, limit=limit)
        max_attempts = _max_attempts()
        for row in pending:
            outcome = refresh_artifact(org_id, row["source_system"], row["source_artifact"])
            result.processed += 1
            if outcome.status == "refreshed":
                refresh_queue.mark_done(org_id, row["id"])
                result.refreshed += 1
                result.reused_chunks += outcome.chunks_reused
                result.reembedded_chunks += outcome.chunks_reembedded
            elif outcome.status == "no_resolver":
                # No way to re-extract this source yet — leave it queued (chunks stay
                # stale, never served as current) until a resolver is available.
                result.skipped += 1
            else:  # no_content | resolver_error | error
                refresh_queue.mark_failed(
                    org_id, row["id"], outcome.detail or outcome.status,
                    max_attempts=max_attempts,
                )
                result.failed += 1
    except Exception:  # noqa: BLE001 — a pass error must never crash the worker
        logger.warning(
            "refresh: pass for org %s failed; work stays queued", org_id, exc_info=True
        )
    return result


def refresh_pending_all_orgs(
    *, max_artifacts_per_org: Optional[int] = None, max_orgs: int = 100
) -> List[RefreshRunResult]:
    """Drain a page of queued refreshes for every org that has one (worker driver).

    Enumerates orgs with pending refresh rows (``refresh_queue.orgs_with_pending``)
    and runs :func:`refresh_pending_for_org` for each — every read stays org-scoped.
    Per-org failures are isolated (``refresh_pending_for_org`` never raises), so one
    tenant's backlog can never stall another's. Returns one result per org processed.
    """
    results: List[RefreshRunResult] = []
    try:
        orgs = refresh_queue.orgs_with_pending(limit=max_orgs)
    except Exception:  # noqa: BLE001 — a scan error must not crash the worker
        logger.warning("refresh: could not enumerate orgs with pending refreshes", exc_info=True)
        return results

    for org_id in orgs:
        results.append(
            refresh_pending_for_org(org_id, max_artifacts=max_artifacts_per_org)
        )
    return results
