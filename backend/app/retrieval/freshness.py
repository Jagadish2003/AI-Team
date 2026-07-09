"""Retrieval freshness — the ingestion.artifact_changed subscriber (R18-B2 T1).

The freshness listener for the retrieval substrate. It makes the retrieval layer
REACT when source content changes, instead of continuing to serve old chunks as if
they were still current.

The ingestion foundation has emitted ``ingestion.artifact_changed`` events since
1.6 (R16-A1) — org, connector, artifact id, change kind — with no consumer. This
module is the consumer they were emitted for. No connector changes are needed; the
signal has been flowing for two releases.

What T1 does, per Section 1 (The Freshness Contract):

  * ``deleted``            → remove the artifact's chunks from retrieval
                             IMMEDIATELY, before touching the refresh queue, via the
                             direct index-cleanup entry point :func:`remove_artifact`
                             (R18-B2 T2). The moment a source artifact is known gone,
                             its evidence must stop being retrievable (Section 1 /
                             AC2). Chunk removal and dropping any pending refresh for
                             the artifact happen in ONE atomic transaction
                             (``store.purge_artifact``) — deletion is a direct index
                             cleanup, never a refresh, so the system stays correct
                             even if the async refresh worker is delayed.
  * ``created`` | ``updated`` → mark the artifact's existing chunks STALE (so they
                             are no longer treated as fully-current evidence) and
                             QUEUE the artifact for asynchronous refresh. The heavy
                             work (re-extract, re-chunk, hash-compare, re-embed,
                             atomic swap) is done off the discovery-run path by the
                             refresh worker (T3) — enqueuing here is cheap and never
                             blocks a run.

Field mapping — the change event speaks the ingestion vocabulary, the store speaks
the retrieval vocabulary; T1 is where the two are bridged:

    event.connector_id  ->  store.source_system
    event.artifact_id   ->  store.source_artifact

Fire-and-forget and defensive by construction: this runs off the back of a
telemetry event and must NEVER break ingestion or a discovery run. A malformed
event is ignored; a store/queue error is logged and swallowed. The invalidation is
itself observable — one ``retrieval.artifact_invalidated`` telemetry event per
handled change — so 'staleness is never invisible' (Section 1) holds at the moment
of change, not just in the standing metrics (T6).

Scope note: T1 delivered the subscriber wiring and the invalidation actions (mark
stale + enqueue; remove on delete). T2 makes the deletion case first-class — a
public :func:`remove_artifact` entry point backed by the atomic
``store.purge_artifact`` — so deleted content leaves the index (and can no longer
influence recommendations, summaries, or LLM enrichment) with no window and no
orphaned refresh, independently of the async worker. The async refresh worker is
T3, the stale-exclusion policy in ``retrieve()`` is T4, and the standing freshness
metrics are T6. Each is its own task; these are the foundation they build on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from app.retrieval import store

logger = logging.getLogger(__name__)

# Change kinds carried on ingestion.artifact_changed (mirrors
# discovery.ingest.base.ChangeKind — duplicated as plain strings here so the
# consumer does not import the discovery package). A record in a delta with no
# recognised kind is treated as an update: it is, after all, in a delta.
_CREATED = "created"
_UPDATED = "updated"
_DELETED = "deleted"


@dataclass(frozen=True)
class ArtifactChangedEvent:
    """Normalised view of an ``ingestion.artifact_changed`` event.

    The raw event is a telemetry payload dict (org_id, connector_id, artifact_id,
    change_kind, observed_at). This dataclass gives the subscriber a validated,
    typed handle and is the single place the ingestion→retrieval field mapping is
    expressed: ``connector_id`` is the retrieval ``source_system`` and
    ``artifact_id`` is the retrieval ``source_artifact``.
    """

    org_id: str
    source_system: str      # <- event.connector_id
    source_artifact: str    # <- event.artifact_id
    change_kind: str
    observed_at: Optional[str] = None

    @classmethod
    def from_payload(
        cls, payload: Union[Mapping[str, Any], "ArtifactChangedEvent"]
    ) -> Optional["ArtifactChangedEvent"]:
        """Build a normalised event from a telemetry payload dict (or pass through).

        Returns ``None`` for anything unusable — a non-mapping, or a payload with
        no org / no artifact id — so the caller can safely ignore malformed events
        rather than raising into the ingestion path. ``change_kind`` is lower-cased
        and defaults to ``updated`` when absent or unrecognised.
        """
        if isinstance(payload, ArtifactChangedEvent):
            return payload
        if not isinstance(payload, Mapping):
            return None

        org_id = str(payload.get("org_id") or "").strip()
        # The emitter keys the changed item as artifact_id; accept a couple of
        # tolerant fallbacks so a slightly different producer shape still works.
        artifact_id = payload.get("artifact_id")
        if artifact_id is None:
            artifact_id = payload.get("source_artifact") or payload.get("id")
        connector_id = payload.get("connector_id")
        if connector_id is None:
            connector_id = payload.get("source_system")

        source_artifact = "" if artifact_id is None else str(artifact_id).strip()
        source_system = "" if connector_id is None else str(connector_id).strip()

        if not org_id or not source_artifact or not source_system:
            return None

        raw_kind = str(payload.get("change_kind") or _UPDATED).strip().lower()
        change_kind = raw_kind if raw_kind in {_CREATED, _UPDATED, _DELETED} else _UPDATED

        observed_at = payload.get("observed_at")
        return cls(
            org_id=org_id,
            source_system=source_system,
            source_artifact=source_artifact,
            change_kind=change_kind,
            observed_at=str(observed_at) if observed_at is not None else None,
        )


def on_artifact_changed(
    event: Union[Mapping[str, Any], ArtifactChangedEvent]
) -> None:
    """Subscriber for ``ingestion.artifact_changed`` (emitting since 1.6).

    Turns a source-change event into a retrieval-freshness action per Section 1:

        deleted            -> store.purge_artifact(...)           # immediate, atomic
        created | updated  -> store.mark_stale_and_enqueue(...)   # second-class +
                                                                  # async refresh,
                                                                  # one transaction

    Never raises: it runs off a telemetry event and must not break ingestion. A
    malformed event is ignored; any store/queue failure is logged and swallowed.
    """
    try:
        parsed = ArtifactChangedEvent.from_payload(event)
    except Exception:  # noqa: BLE001 — parsing must never break ingestion
        logger.warning("freshness: could not parse artifact_changed event", exc_info=True)
        return

    if parsed is None:
        logger.debug("freshness: ignoring malformed artifact_changed event: %r", event)
        return

    try:
        if parsed.change_kind == _DELETED:
            _handle_deleted(parsed)
        else:  # created | updated
            _handle_upserted(parsed)
    except Exception:  # noqa: BLE001 — invalidation must never break ingestion
        logger.warning(
            "freshness: failed to invalidate artifact (org=%s source_system=%s "
            "source_artifact=%s kind=%s)",
            parsed.org_id,
            parsed.source_system,
            parsed.source_artifact,
            parsed.change_kind,
            exc_info=True,
        )


def _handle_deleted(event: ArtifactChangedEvent) -> None:
    """Deletion path: hand off to the direct index-cleanup entry point (T2).

    The subscriber does no work of its own for a deletion beyond delegating to
    :func:`remove_artifact`, which performs the immediate, atomic cleanup. Keeping
    the actual removal in a public function means deletion can also be driven
    directly (e.g. a compliance-mandated purge) on exactly the same code path the
    change event uses.
    """
    remove_artifact(
        event.org_id,
        event.source_system,
        event.source_artifact,
        change_kind=event.change_kind,
    )


def remove_artifact(
    org_id: str,
    source_system: str,
    source_artifact: str,
    *,
    change_kind: str = _DELETED,
) -> int:
    """Immediately purge one artifact from the retrieval index (R18-B2 T2).

    This is the strongest freshness case handled as a DIRECT index-cleanup
    operation, NOT a refresh: unlike an update, a deletion never waits for the async
    refresh queue. Deleted content must stop appearing in retrieval — and stop
    being able to influence recommendations, summaries, or LLM enrichment — as soon
    as the deletion is known, because it may no longer be valid, visible, or allowed
    to be cited as current evidence (a deleted Confluence page, document, Git file,
    or message).

    Deletion is honoured BEFORE the re-embed queue, not after (Section 1 / AC2). One
    call to :func:`store.purge_artifact` removes the artifact's chunks and drops any
    pending refresh-queue row in a SINGLE transaction, so:

      * there is no window where the deleted artifact is still retrievable as
        current; and
      * the refresh worker (T3) never wakes to re-embed an artifact that is gone —
        the system stays correct even if that worker is delayed.

    Returns the number of chunks removed. Callable both by the ``deleted`` change
    handler and directly (compliance / manual purge). Org-scoped by construction.
    Any store failure propagates to :func:`on_artifact_changed`'s guard when driven
    by an event; a direct caller is expected to handle its own errors.
    """
    chunks_removed, queue_removed = store.purge_artifact(
        org_id, source_system, source_artifact
    )
    logger.info(
        "freshness: purged deleted artifact (org=%s source_system=%s "
        "source_artifact=%s chunks_removed=%d queue_rows_dropped=%d)",
        org_id,
        source_system,
        source_artifact,
        chunks_removed,
        queue_removed,
    )
    _emit_invalidated(
        ArtifactChangedEvent(org_id, source_system, source_artifact, change_kind),
        action="removed",
        chunks_affected=chunks_removed,
        queued=False,
    )
    return chunks_removed


def _handle_upserted(event: ArtifactChangedEvent) -> None:
    """Created/updated path: mark chunks stale AND queue an async refresh, atomically.

    Marking stale makes the existing chunks second-class immediately (T4 excludes
    them from default retrieval); enqueuing hands the heavy re-chunk/re-embed to the
    async worker (T3) so a discovery run is never blocked on it. An artifact with no
    indexed chunks yet (first-seen ``created``) still queues, so the refresh worker
    will index it.

    The two writes commit in ONE transaction (``store.mark_stale_and_enqueue``,
    mirroring the deletion path's atomic ``purge_artifact``): a chunk marked stale
    with no pending queue row would be excluded from retrieval with nothing ever
    scheduled to refresh it — and since the change event fires only on artifact
    change, there is no later signal to recover from that split. Atomicity here is
    what guarantees stale always implies queued.
    """
    marked, _queue_id = store.mark_stale_and_enqueue(
        event.org_id,
        event.source_system,
        event.source_artifact,
        change_kind=event.change_kind,
    )
    logger.info(
        "freshness: marked %d chunk(s) stale + queued refresh (org=%s "
        "source_system=%s source_artifact=%s kind=%s)",
        marked,
        event.org_id,
        event.source_system,
        event.source_artifact,
        event.change_kind,
    )
    _emit_invalidated(event, action="marked_stale", chunks_affected=marked, queued=True)


def _emit_invalidated(
    event: ArtifactChangedEvent,
    *,
    action: str,
    chunks_affected: int,
    queued: bool,
) -> None:
    """Emit one ``retrieval.artifact_invalidated`` telemetry event. Never raises.

    Identifiers, change kind, and counts only — never artifact content. Imported
    lazily and fully guarded so a telemetry problem can never break invalidation.
    """
    try:
        from app.telemetry import record_event

        record_event(
            "retrieval.artifact_invalidated",
            {
                "org_id": event.org_id,
                "source_system": event.source_system,
                "source_artifact": event.source_artifact,
                "change_kind": event.change_kind,
                "action": action,
                "chunks_affected": chunks_affected,
                "queued": queued,
            },
        )
    except Exception:  # pragma: no cover — telemetry is best-effort
        logger.debug("freshness: telemetry emit failed", exc_info=True)
