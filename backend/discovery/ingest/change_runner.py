"""R16-A1 / AT-379 — runner integration for change-based ingestion.

Drives every :class:`~discovery.ingest.base.ChangeBasedIngestor` through its
``ingest_changes()`` stream and owns the **full checkpoint lifecycle** so that
individual connectors never touch persistence:

    1. read the checkpoint for ``(org_id, connector_id)`` (may be None — first run)
    2. stream ``ingest_changes(org_id, since=checkpoint)``
    3. process each batch fully (via an optional ``process_batch`` callback)
    4. ONLY after the final batch (``is_complete=True``) is processed, write the
       new ``next_checkpoint`` atomically
    5. on ANY failure, do NOT write the checkpoint — the next run re-reads from
       the last known-good position

This is the single most important correctness rule of the foundation
(R16-A1 §2, §8): **the checkpoint advances only on full success.** A failed or
partial run must never advance it, or data is silently skipped forever (AC2).

Connector-agnostic by construction (R16-A1 §8, AC5): the runner treats the
checkpoint ``value`` as opaque. It is read, threaded back into ``ingest_changes``
as ``since``, and persisted verbatim — never parsed, compared, or branched on.
That is what lets a timestamp source, a commit-SHA source, and a change-sequence
source all share this one driver unchanged.

Out of scope here (deliberately deferred to the stories this one blocks):
  * per-batch resumable checkpointing of a first-run full load — AT-380.
  * ``ingestion.artifact_changed`` event emission — AT-381.
This driver establishes the write-only-on-full-success baseline they build on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from .base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from . import checkpoint_repository as _repo

logger = logging.getLogger(__name__)

# Injectable persistence seam — defaults to the real AT-378 repository, but tests
# (and alternative stores) can pass their own read/save callables. The runner
# never reaches past these two functions, keeping storage and orchestration
# cleanly separable.
ReadCheckpoint = Callable[[str, str], Optional[Checkpoint]]
SaveCheckpoint = Callable[[Checkpoint], None]
ProcessBatch = Callable[[DeltaBatch], None]


@dataclass
class IngestionResult:
    """Outcome of driving a single ingestor through one run.

    ``checkpoint_advanced`` is the load-bearing field: it is True only when the
    delta was fully consumed and the new position was persisted. On any failure
    it stays False and ``error`` carries the cause — the prior checkpoint is
    left untouched so the next run re-reads from it.
    """

    connector_id: str
    org_id: str
    batches: int = 0
    records: int = 0
    checkpoint_advanced: bool = False
    new_checkpoint: Optional[Checkpoint] = None
    started_from: Optional[Checkpoint] = None  # the checkpoint read before the run
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def ingest_with_checkpoint(
    ingestor: ChangeBasedIngestor,
    org_id: str,
    *,
    process_batch: Optional[ProcessBatch] = None,
    read_checkpoint: ReadCheckpoint = _repo.read_checkpoint,
    save_checkpoint: SaveCheckpoint = _repo.save_checkpoint,
) -> IngestionResult:
    """Drive one ingestor through its full checkpoint lifecycle.

    Reads the prior checkpoint, streams the delta, hands each batch to
    ``process_batch`` (if given), and persists the new checkpoint **only** after
    the stream drains successfully with a terminal ``is_complete=True`` batch.

    Never raises for a runtime failure of the connector stream, ``process_batch``,
    or the persistence layer: the failure is recorded on ``result.error`` and the
    run stops WITHOUT advancing the checkpoint, so the prior position is preserved
    and the next run re-reads from it (AC2). (A misconfigured ingestor with no
    ``connector_id`` is a programming error and is raised eagerly.)

    The checkpoint ``value`` is never inspected here — it is read, passed back as
    ``since``, and saved verbatim (AC5).
    """
    connector_id = ingestor.connector_id
    if not connector_id:
        raise ValueError(
            "ChangeBasedIngestor.connector_id must be set before driving it "
            "(the runner keys checkpoints by it)."
        )

    result = IngestionResult(connector_id=connector_id, org_id=org_id)

    try:
        # 1. read checkpoint before the run — opaque; may be None on first run.
        since = read_checkpoint(org_id, connector_id)
        result.started_from = since
        logger.info(
            "change-ingest: connector=%s org=%s start (checkpoint=%s)",
            connector_id,
            org_id,
            "none (first run)" if since is None else "present",
        )

        terminal_value: Optional[str] = None
        saw_complete = False

        # 2. stream the delta; 3. process each batch fully before the next.
        for batch in ingestor.ingest_changes(org_id, since):
            if not isinstance(batch, DeltaBatch):  # defensive: contract violation
                raise TypeError(
                    f"{connector_id}.ingest_changes yielded {type(batch).__name__}, "
                    "expected DeltaBatch"
                )
            if process_batch is not None:
                # Any exception here propagates → no checkpoint write (AC2).
                process_batch(batch)
            result.batches += 1
            result.records += len(batch.records)
            if batch.is_complete:
                # Remember the terminal position but do NOT write yet — the write
                # happens only after the whole stream drains without error, so a
                # later batch raising still leaves the prior checkpoint intact.
                terminal_value = batch.next_checkpoint
                saw_complete = True

        # 4. write only on full success: the stream fully drained AND reported a
        # terminal batch. An unchanged source that yields no batches (or no
        # complete batch) leaves the prior checkpoint untouched — never a
        # regression.
        if saw_complete:
            new_cp = Checkpoint.create(connector_id, org_id, terminal_value or "")
            save_checkpoint(new_cp)
            result.checkpoint_advanced = True
            result.new_checkpoint = new_cp
            logger.info(
                "change-ingest: connector=%s org=%s OK — %d record(s) across %d "
                "batch(es); checkpoint advanced.",
                connector_id,
                org_id,
                result.records,
                result.batches,
            )
        else:
            logger.info(
                "change-ingest: connector=%s org=%s — no terminal batch "
                "(empty/partial delta); checkpoint left unchanged.",
                connector_id,
                org_id,
            )
    except Exception as exc:  # noqa: BLE001 — capture, do not advance, do not abort caller.
        result.error = exc
        result.checkpoint_advanced = False
        result.new_checkpoint = None
        logger.warning(
            "change-ingest: connector=%s org=%s FAILED after %d batch(es) — "
            "checkpoint NOT advanced; next run re-reads from last known-good. (%s: %s)",
            connector_id,
            org_id,
            result.batches,
            type(exc).__name__,
            exc,
        )

    return result


def run_ingestors(
    ingestors: Iterable[ChangeBasedIngestor],
    org_id: str,
    *,
    process_batch: Optional[ProcessBatch] = None,
    read_checkpoint: ReadCheckpoint = _repo.read_checkpoint,
    save_checkpoint: SaveCheckpoint = _repo.save_checkpoint,
) -> List[IngestionResult]:
    """Drive every ingestor independently through :func:`ingest_with_checkpoint`.

    Each connector is isolated: a failure in one is recorded on its result and
    never advances its checkpoint, but does not abort the others (one bad source
    must not block the rest of the run). Returns one :class:`IngestionResult` per
    ingestor, in input order.
    """
    return [
        ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=process_batch,
            read_checkpoint=read_checkpoint,
            save_checkpoint=save_checkpoint,
        )
        for ingestor in ingestors
    ]
