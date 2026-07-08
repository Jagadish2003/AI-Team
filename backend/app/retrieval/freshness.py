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
                             IMMEDIATELY, before touching the refresh queue. The
                             moment a source artifact is known gone, its evidence
                             must stop being retrievable (Section 1 / AC2). A
                             pending refresh for it is dropped — there is nothing
                             left to refresh.
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

Scope note (T1): this delivers the subscriber wiring and the invalidation actions
(mark stale + enqueue; immediate remove on delete). The async refresh worker is
T3, the stale-exclusion policy in ``retrieve()`` is T4, and the standing freshness
metrics are T6. Each is its own task; this one is the foundation they build on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from app.retrieval import refresh_queue, store

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

        deleted            -> store.remove_chunks(...)          # immediate
        created | updated  -> store.mark_stale(...)             # second-class
                              refresh_queue.enqueue(...)        # async refresh

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
    """Deletion path: remove chunks immediately, BEFORE the queue (Section 1/AC2).

    Deletion is honoured before the re-embed queue, not after — the moment a source
    artifact is known gone, its evidence must stop being retrievable, with no
    window where deleted content is still served as current. Any pending refresh
    for the artifact is dropped: there is nothing left to refresh.
    """
    removed = store.remove_chunks(
        event.org_id, event.source_system, event.source_artifact
    )
    # Drop a stale pending refresh for the now-deleted artifact (best-effort).
    try:
        refresh_queue.remove(
            event.org_id, event.source_system, event.source_artifact
        )
    except Exception:  # noqa: BLE001 — queue cleanup is best-effort
        logger.debug(
            "freshness: could not drop queue row for deleted artifact "
            "(org=%s artifact=%s)",
            event.org_id,
            event.source_artifact,
            exc_info=True,
        )
    logger.info(
        "freshness: removed %d chunk(s) for deleted artifact (org=%s "
        "source_system=%s source_artifact=%s)",
        removed,
        event.org_id,
        event.source_system,
        event.source_artifact,
    )
    _emit_invalidated(event, action="removed", chunks_affected=removed, queued=False)


def _handle_upserted(event: ArtifactChangedEvent) -> None:
    """Created/updated path: mark chunks stale, then queue an async refresh.

    Marking stale makes the existing chunks second-class immediately (T4 excludes
    them from default retrieval); enqueuing hands the heavy re-chunk/re-embed to the
    async worker (T3) so a discovery run is never blocked on it. An artifact with no
    indexed chunks yet (first-seen ``created``) still queues, so the refresh worker
    will index it.
    """
    marked = store.mark_stale(
        event.org_id, event.source_system, event.source_artifact
    )
    refresh_queue.enqueue(
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
