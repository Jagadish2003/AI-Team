"""MSP-B8 / T4 — the Event-History Bridge ingestor.

The bridge is the transport that makes AgentIQ's cloud-event findings identical
whether they arrived through a native connector (MSP-B1/B2) or through an exported
event history loaded into staging (MSP-B8 T2/T3). It reads staged rows on
AgentIQ's existing change-based ingestion path and normalises each one through the
MSP-B0 mappers, emitting the SAME :class:`OperationalEvent` shape a native
connector emits — so a detector cannot tell the two apart, except for the
intentional ``source_system='bridge:<provider>'`` prefix.

What this ingestor does (per staged row)
----------------------------------------
1. **Reads incrementally on the read-only DB path.** A :class:`StagingReader`
   pages forward by staging ``row_id`` from the last checkpoint. The default
   :class:`~discovery.ingest.ops_event_staging_store.DbStagingReader` opens each
   read with ``SET TRANSACTION READ ONLY`` and fails closed — the bridge is a
   consumer of the staging store, never a privileged write path (AC6).
2. **Maps through the appropriate MSP-B0 mapper** for the row's
   ``(provider, source_format)`` — ``map_cloudwatch`` / ``map_eventbridge`` /
   ``map_cloudtrail`` / ``map_azure_monitor`` / ``map_azure_activity_log``.
3. **Re-stamps ``source_system`` to ``bridge:<provider>``** while PRESERVING the
   mapper's ``event_signature`` — so a bridged event and its native twin share the
   same recurrence identity and differ only in ``source_system`` (the bridge
   equivalence proof; AC2 is finalised in T6).
4. **Attaches evidence that resolves back to the raw staged payload and batch
   identity.** The event's OBSERVED evidence pointer keys the raw payload in a
   :class:`RawEventStore`; the emitted record also carries ``batch_id`` and
   ``staging_row_id`` so a finding traces to the exact export batch (AC1/AC4).
5. **Preserves ``provider_event_id`` and dedupes on it** — re-loading an export
   batch never yields duplicate operational events (idempotent loads; AC3).

Checkpoint (opaque to the runner)
---------------------------------
The opaque checkpoint value is the last processed staging ``row_id`` as a string.
The ingestor pages ``row_id > checkpoint`` and advances only as batches are
emitted; the runner persists ``next_checkpoint`` after a batch is processed and
the final ``is_complete=True`` batch lands (R16-A1 / AT-378). A DB read failure
propagates (fail-closed), so the checkpoint never advances over unread rows.

Deletes / tombstones (R16-A1 §5): ``reports_deletes = False`` — the staging table
is append-only export history; an exported event is never retracted, so there is
no upstream deletion to propagate. The limitation is declared, not faked.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

from discovery.signals.evidence_store import RawEventStore
from discovery.signals.operational_event import OperationalEvent
from discovery.signals.reference_mappers import (
    map_azure_activity_log,
    map_azure_monitor,
    map_cloudtrail,
    map_cloudwatch,
    map_eventbridge,
)

from .aws_export_loaders import (
    PROVIDER_AWS,
    SOURCE_FORMAT_CLOUDTRAIL,
    SOURCE_FORMAT_CLOUDWATCH,
    SOURCE_FORMAT_EVENTBRIDGE,
)
from .azure_export_loaders import (
    PROVIDER_AZURE,
    SOURCE_FORMAT_AZURE_ACTIVITY_LOG,
    SOURCE_FORMAT_AZURE_MONITOR,
)
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .ops_event_staging_store import DbStagingReader, StagingReader

logger = logging.getLogger(__name__)

#: Rows read from staging per page. Modest so a large backlog streams as many
#: small, individually-checkpointed batches (resumable) rather than one huge read.
DEFAULT_BATCH_SIZE = 500

#: (provider, source_format) → the MSP-B0 mapper that normalises that surface.
#: The bridge routes each staged row through exactly the mapper a native connector
#: for that surface would use, so bridge and native events are the same shape.
_MAPPER_REGISTRY = {
    (PROVIDER_AWS, SOURCE_FORMAT_CLOUDWATCH): map_cloudwatch,
    (PROVIDER_AWS, SOURCE_FORMAT_EVENTBRIDGE): map_eventbridge,
    (PROVIDER_AWS, SOURCE_FORMAT_CLOUDTRAIL): map_cloudtrail,
    (PROVIDER_AZURE, SOURCE_FORMAT_AZURE_MONITOR): map_azure_monitor,
    (PROVIDER_AZURE, SOURCE_FORMAT_AZURE_ACTIVITY_LOG): map_azure_activity_log,
}


def bridge_source_system(provider: str) -> str:
    """The intentional bridge prefix stamped on every bridge-produced event."""
    return f"bridge:{provider}"


class OpsEventBridgeIngestor(ChangeBasedIngestor):
    """ChangeBasedIngestor over the ops-event staging table (MSP-B8 T4).

    Construct with an injectable :class:`StagingReader` (defaults to the read-only
    :class:`DbStagingReader`) and an optional :class:`RawEventStore` so a bridged
    event's evidence pointer resolves back to its raw staged payload. Both are
    injectable so the whole ingestor is unit-testable with no database.
    """

    connector_id = "ops_event_bridge"
    reports_deletes = False
    #: A bridged cloud event is an observation, not an indexed retrieval artifact —
    #: the change runner must not emit per-event artifact_changed/freshness work for
    #: it (see ChangeBasedIngestor.produces_retrieval_content).
    produces_retrieval_content = False

    def __init__(
        self,
        reader: Optional[StagingReader] = None,
        *,
        raw_store: Optional[RawEventStore] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.reader = reader if reader is not None else DbStagingReader()
        self.raw_store = raw_store
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ─────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of normalised operational events since ``since``.

        Pages ``row_id > checkpoint`` from the reader. Each row is mapped through
        its MSP-B0 mapper, re-stamped ``source_system='bridge:<provider>'``, and
        emitted as a record carrying the normalised event, its evidence pointer,
        ``provider_event_id`` (dedupe) and ``batch_id`` (batch identity). An empty
        staging table (or a fully-consumed one) yields a single empty
        :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming position.
        """
        if not org_id or not str(org_id).strip():
            raise ValueError("org_id is required")

        after = _decode_checkpoint(since)
        seen: set[tuple] = set()          # (provider, provider_event_id) — dedupe
        emitted_any = False

        while True:
            # Fail-closed: a reader error propagates and the checkpoint is never
            # advanced over rows that were not actually read.
            rows = list(self.reader.fetch_after(org_id, after_row_id=after, limit=self.batch_size))
            if not rows:
                if not emitted_any:
                    # Nothing new: a single empty delta that echoes the position.
                    yield DeltaBatch(records=[], next_checkpoint=str(after), is_complete=True)
                return

            records: List[Dict[str, Any]] = []
            for row in rows:
                # Advance past every row we consume. Tested against None, not
                # truthiness: row_id 0 is a legitimate position, and treating it as
                # "no id" would leave the checkpoint pinned and reprocess the same
                # staging row on every run.
                if row.row_id is not None:
                    after = row.row_id
                dedupe_key = (row.provider, row.provider_event_id)
                if dedupe_key in seen:
                    logger.debug(
                        "ops_event_bridge: duplicate provider_event_id %s (%s) skipped",
                        row.provider_event_id, row.provider,
                    )
                    continue
                event = self._map_row(org_id, row)
                if event is None:
                    continue  # unroutable/failed row was loud-skipped in _map_row
                seen.add(dedupe_key)
                records.append(self._to_record(event, row))

            more = len(rows) == self.batch_size
            emitted_any = True
            yield DeltaBatch(
                records=records,
                next_checkpoint=str(after),
                is_complete=not more,
            )
            if not more:
                return

    # ── Mapping + emission ───────────────────────────────────────────────────
    def _map_row(self, org_id: str, row: Any) -> Optional[OperationalEvent]:
        """Normalise one staged row into a ``bridge:<provider>`` OperationalEvent.

        Returns ``None`` (loud-skip) when no mapper is registered for the row's
        ``(provider, source_format)`` or the mapper raises — a single unroutable
        row must never wedge the whole bridge, mirroring the loaders' loud-skip
        discipline. The checkpoint still advances past a skipped row.
        """
        mapper = _MAPPER_REGISTRY.get((row.provider, row.source_format))
        if mapper is None:
            logger.warning(
                "ops_event_bridge: no B0 mapper for (provider=%s, source_format=%s) "
                "— row_id=%s skipped", row.provider, row.source_format, row.row_id,
            )
            return None
        try:
            event = mapper(row.raw, org_id=org_id)
        except Exception:  # mappers are meant to be tolerant; stay robust anyway
            logger.warning(
                "ops_event_bridge: mapper %s failed on row_id=%s (provider=%s) — skipped",
                getattr(mapper, "__name__", mapper), row.row_id, row.provider,
                exc_info=True,
            )
            return None

        # Re-stamp the transport WITHOUT recomputing the recurrence signature:
        # the mapper already derived event_signature from the native provider
        # family, so preserving it keeps a bridged event identical to its native
        # twin except for source_system (the equivalence guarantee).
        event.source_system = bridge_source_system(row.provider)
        # Repoint provenance at the raw STAGED payload (not the live API artifact),
        # keyed so it resolves through the raw-event store.
        event.provenance = self._bridge_pointer(row, event).to_dict()

        # Persist the raw staged payload so the evidence pointer resolves back to
        # it (AC1). Optional: without a raw_store the event still carries a valid
        # pointer, it just has nothing to resolve against.
        if self.raw_store is not None:
            self.raw_store.put(
                org_id,
                event.source_system,
                row.provider_event_id,
                row.raw,
            )
        return event

    def _bridge_pointer(self, row: Any, event: OperationalEvent) -> EvidencePointer:
        """OBSERVED evidence pointer keyed to the raw staged payload."""
        return EvidencePointer.observed(
            source_system=bridge_source_system(row.provider),
            source_artifact=row.provider_event_id,
            source_timestamp=event.observed_at,
            source_artifact_type="staged_event",
        )

    def _to_record(self, event: OperationalEvent, row: Any) -> Dict[str, Any]:
        """Shape one normalised bridge event into a DeltaBatch record.

        ``event`` is the detector-visible, provider-agnostic OperationalEvent (the
        SAME shape a native connector emits). The record wraps it with the change
        vocabulary and the bridge trace-back metadata — ``provider_event_id`` for
        dedupe, ``batch_id`` / ``staging_row_id`` for batch identity, and the
        ``evidence_pointer`` that resolves to the raw staged payload.
        """
        return {
            "artifact_id": f"{event.source_system}:{row.provider_event_id}",
            "change_kind": ChangeKind.CREATED,
            "source_system": event.source_system,
            "provider": row.provider,
            "source_format": row.source_format,
            "provider_event_id": row.provider_event_id,
            "batch_id": row.batch_id,
            "staging_row_id": row.row_id,
            "event": event.to_dict(),
            "evidence_pointer": event.provenance,
        }


def _decode_checkpoint(since: Optional[Checkpoint]) -> int:
    """Decode the opaque checkpoint value (last processed row_id) to an int.

    Tolerant: a missing / blank / non-numeric value means "start from the
    beginning" (row_id 0), never a crash — a degenerate checkpoint degrades to a
    safe full re-read.
    """
    if since is None or not since.value:
        return 0
    try:
        return int(since.value)
    except (TypeError, ValueError):
        logger.warning(
            "ops_event_bridge: unreadable checkpoint value %r; starting from 0",
            since.value,
        )
        return 0


def resolve_raw_payload(
    raw_store: RawEventStore, org_id: str, record: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Resolve a bridge record back to its raw staged payload (evidence trace-back).

    Walks the record's evidence pointer ``(source_system, source_artifact)`` into
    the raw-event store within ``org_id`` — the same key the ingestor stored the
    raw payload under. Returns ``None`` when nothing is stored (e.g. the ingestor
    ran without a raw_store).
    """
    pointer = record.get("evidence_pointer") or {}
    source_system = pointer.get("source_system")
    source_artifact = pointer.get("source_artifact")
    if not source_system or not source_artifact:
        return None
    return raw_store.get(org_id, source_system, source_artifact)
