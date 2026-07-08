"""R16-A1 / AT-379 + AT-380 — runner integration for change-based ingestion.

Drives every :class:`~discovery.ingest.base.ChangeBasedIngestor` through its
``ingest_changes()`` stream and owns the **full checkpoint lifecycle** so that
individual connectors never touch persistence:

    1. read the checkpoint for ``(org_id, connector_id)`` (may be None — first run)
    2. stream ``ingest_changes(org_id, since=checkpoint)``
    3. process each batch fully (via an optional ``process_batch`` callback)
    4. write the new ``next_checkpoint`` — see the two-mode rule below
    5. on ANY failure, do NOT advance past the last fully-processed batch — the
       next run re-reads from the last known-good position

Two checkpoint-write modes, selected purely by whether a prior checkpoint exists
(``since is None``). This is read-before-run existence logic from R16-A1 §2 step 1
— it never inspects the checkpoint *value*, so the runner stays connector-agnostic
(AC5):

  * Incremental run (``since`` present) — R16-A1 §2 (AC2): the steady-state path.
    The delta since the last run is small, so the checkpoint is written ONCE,
    only after the final batch (``is_complete=True``) is processed. A failure
    mid-batch leaves the prior checkpoint unchanged; the next run re-reads from it.

  * First run (``since is None``) — R16-A1 §3 (AC3): the initial full load is
    STREAMED as checkpointed batches rather than one monolithic read. Each batch
    writes its own checkpoint immediately after it is fully processed, so a
    failure at batch N resumes from batch N-1 on the next run instead of
    restarting the whole load. This makes even a 100GB initial load resumable.

    Resume boundary: once the first run has written any checkpoint, the next run
    finds a checkpoint and is therefore driven in incremental (all-or-nothing)
    mode. The connector still resumes from the last saved batch (it reads the
    checkpoint back as ``since``), so no data is skipped and the load completes
    across runs; what incremental mode does not do is persist a *second* run's
    partial progress mid-resume. This is a deliberate consequence of selecting the
    mode by checkpoint existence alone (never by inspecting the value — AC5) on the
    fixed AT-378 schema, which carries no full-load-in-progress flag. Correctness
    (no skipped records, eventual completion) holds regardless.

Both modes honour the load-bearing rule (R16-A1 §8): **the checkpoint never
advances past data that was not fully processed.** A written checkpoint always
marks the end of a batch whose ``process_batch`` returned successfully.

Connector-agnostic by construction (R16-A1 §8, AC5): the checkpoint ``value`` is
read, threaded back into ``ingest_changes`` as ``since``, and persisted verbatim —
never parsed, compared, or branched on. That is what lets a timestamp source, a
commit-SHA source, and a change-sequence source all share this one driver.

Change events (R16-A1 §4, AT-381): for each record in every fully-processed
batch this runner emits one ``ingestion.artifact_changed`` telemetry event. 1.6
only EMITS — the Release 1.8 retrieval-freshness layer will consume them. Emission
is fire-and-forget: it never affects the checkpoint lifecycle or the run outcome.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from . import checkpoint_repository as _repo

logger = logging.getLogger(__name__)

# Injectable persistence seam — defaults to the real AT-378 repository, but tests
# (and alternative stores) can pass their own read/save callables. The runner
# never reaches past these two functions, keeping storage and orchestration
# cleanly separable.
ReadCheckpoint = Callable[[str, str], Optional[Checkpoint]]
SaveCheckpoint = Callable[[Checkpoint], None]
ProcessBatch = Callable[[DeltaBatch], None]


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _is_persistable_checkpoint(value: str, connector_id: str, org_id: str) -> bool:
    """Reject an empty opaque checkpoint value before it is persisted (PR #239).

    ``value`` is opaque to the runner, but an empty string is degenerate: on the
    next run :func:`read_checkpoint` would return a ``Checkpoint(value="")`` and a
    connector handed an empty ``since.value`` typically treats it as "beginning of
    time" and silently performs a full re-read — indistinguishable from an
    intentional first run. The runner is non-blocking, so we do NOT raise; we log
    a warning and skip the write, leaving the prior known-good position intact and
    surfacing the connector contract violation rather than masking it.
    """
    if value != "":
        return True
    logger.warning(
        "change-ingest: connector=%s org=%s yielded an EMPTY next_checkpoint for a "
        "completed batch; not persisting it (an empty checkpoint would force a "
        "silent full re-read next run). This is a connector contract violation.",
        connector_id,
        org_id,
    )
    return False


def _emit_artifact_changed(org_id: str, connector_id: str, records: list) -> None:
    """Emit one ``ingestion.artifact_changed`` telemetry event per changed
    artifact in a fully-processed batch (R16-A1 §4 / AT-381, AC4).

    Each record carries its own ``artifact_id`` and ``change_kind``
    ('created' | 'updated' | 'deleted'); ``observed_at`` is stamped here. 1.6 only
    EMITS — there is no consumer yet (the 1.8 retrieval-freshness layer subscribes
    later). Fire-and-forget: a telemetry import/write failure must NEVER break
    ingestion, so the whole path is guarded and any error is only logged.

    ``record_event`` is imported lazily (like the repository's app.db import) to
    avoid an import cycle with the app package at module load.
    """
    if not records:
        return
    try:
        from app.telemetry import record_event
    except Exception:  # pragma: no cover — telemetry must not break ingestion
        logger.warning(
            "change-ingest: telemetry unavailable; skipping artifact_changed events "
            "for connector=%s",
            connector_id,
        )
        return

    observed_at = _utc_iso()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        artifact_id = rec.get("artifact_id", rec.get("id"))
        # change_kind passes through verbatim (created/updated/deleted, §5/AT-382);
        # a record in a delta with no kind is treated as an update.
        change_kind = rec.get("change_kind", ChangeKind.UPDATED)
        event = {
            "org_id": org_id,
            "connector_id": connector_id,
            "artifact_id": "" if artifact_id is None else str(artifact_id),
            "change_kind": change_kind,
            "observed_at": observed_at,
        }
        try:
            record_event("ingestion.artifact_changed", event)
        except Exception:  # pragma: no cover — one bad event must not stop the run
            logger.warning(
                "change-ingest: failed to emit artifact_changed (connector=%s "
                "artifact_id=%s)",
                connector_id,
                artifact_id,
            )
        # R18-B2 T1: notify the retrieval-freshness subscriber off the SAME change
        # stream. Fully guarded and lazily imported (like record_event above) so a
        # freshness or import failure can NEVER break ingestion — the subscriber is
        # itself fire-and-forget, this guard is the belt-and-braces boundary.
        _notify_freshness(event)


def _notify_freshness(event: dict) -> None:
    """Hand one artifact_changed event to the retrieval-freshness subscriber.

    Lazily imported to avoid an app-package import cycle at module load, and fully
    guarded: a freshness failure must never break the ingestion run. R18-B2 T1.
    """
    try:
        from app.retrieval.freshness import on_artifact_changed

        on_artifact_changed(event)
    except Exception:  # pragma: no cover — freshness must not break ingestion
        logger.warning(
            "change-ingest: freshness subscriber failed (connector=%s artifact_id=%s)",
            event.get("connector_id"),
            event.get("artifact_id"),
        )


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
    first_run: bool = False              # True when no prior checkpoint existed
    batches_checkpointed: int = 0        # per-batch writes (first-run resumable load)
    complete: bool = False               # the stream reported a terminal batch
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

    Reads the prior checkpoint, streams the delta, and hands each batch to
    ``process_batch`` (if given). The checkpoint write policy depends on whether a
    prior checkpoint existed:

      * Incremental run (``since`` present): persist the new checkpoint ONCE, only
        after the stream drains successfully with a terminal ``is_complete=True``
        batch. A mid-batch failure leaves the prior checkpoint unchanged (AC2).
      * First run (``since is None``): stream the initial full load as checkpointed
        batches — persist each batch's checkpoint immediately after it is fully
        processed, so a failure at batch N resumes from batch N-1 next run (AC3).

    Never raises for a runtime failure of the connector stream, ``process_batch``,
    or the persistence layer: the failure is recorded on ``result.error`` and the
    run stops without advancing past the last fully-processed batch, so the next
    run re-reads/resumes from the last known-good position. (A misconfigured
    ingestor with no ``connector_id`` is a programming error and is raised eagerly.)

    The checkpoint ``value`` is never inspected here — it is read, passed back as
    ``since``, and saved verbatim (AC5). Choosing the mode tests only whether a
    checkpoint *exists*, never its contents.
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
        is_first_run = since is None  # existence only — never inspects the value (AC5)
        result.first_run = is_first_run
        logger.info(
            "change-ingest: connector=%s org=%s start (%s)",
            connector_id,
            org_id,
            "first run — streamed resumable full load" if is_first_run
            else "incremental — write-only-on-full-success",
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
                # Any exception here propagates → this batch's checkpoint is never
                # written, so the run never advances past unprocessed data (AC2/AC3).
                process_batch(batch)
            result.batches += 1
            result.records += len(batch.records)

            # AT-381 (§4, AC4): emit one ingestion.artifact_changed per changed
            # artifact in this fully-processed batch. Reached only after
            # process_batch succeeded; fire-and-forget — never affects the
            # checkpoint lifecycle or the run outcome.
            _emit_artifact_changed(org_id, connector_id, batch.records)

            if is_first_run and _is_persistable_checkpoint(
                batch.next_checkpoint, connector_id, org_id
            ):
                # AC3: resumable full load — checkpoint each fully-processed batch
                # so an interruption resumes from the last saved batch, not the
                # start. The value is opaque; persisted verbatim.
                new_cp = Checkpoint.create(connector_id, org_id, batch.next_checkpoint)
                save_checkpoint(new_cp)
                result.checkpoint_advanced = True
                result.new_checkpoint = new_cp
                result.batches_checkpointed += 1

            if batch.is_complete:
                terminal_value = batch.next_checkpoint
                saw_complete = True

        result.complete = saw_complete

        # 4. Incremental write-only-on-full-success: persist once, after the whole
        # stream drained AND reported a terminal batch. (First-run writes already
        # happened per-batch above.) An unchanged/partial incremental source that
        # yields no terminal batch leaves the prior checkpoint untouched (AC2).
        if not is_first_run and saw_complete:
            terminal = terminal_value or ""
            if _is_persistable_checkpoint(terminal, connector_id, org_id):
                new_cp = Checkpoint.create(connector_id, org_id, terminal)
                save_checkpoint(new_cp)
                result.checkpoint_advanced = True
                result.new_checkpoint = new_cp

        if result.checkpoint_advanced:
            logger.info(
                "change-ingest: connector=%s org=%s OK — %d record(s) across %d "
                "batch(es)%s; checkpoint %s.",
                connector_id,
                org_id,
                result.records,
                result.batches,
                "" if not is_first_run else f" ({result.batches_checkpointed} checkpointed)",
                "advanced" if saw_complete else "advanced (load incomplete — resumes next run)",
            )
        else:
            logger.info(
                "change-ingest: connector=%s org=%s — no checkpoint written "
                "(empty/partial delta); position left unchanged.",
                connector_id,
                org_id,
            )
    except Exception as exc:  # noqa: BLE001 — capture, do not advance, do not abort caller.
        result.error = exc
        result.complete = False
        # Do NOT reset checkpoint_advanced / new_checkpoint: on a first-run failure
        # the per-batch writes for the batches that DID fully process are valid
        # resume points (AC3) and must be reported. The current failing batch was
        # never checkpointed, so the stored position still marks the last
        # known-good batch. On an incremental failure nothing was written, so these
        # fields are already at their defaults (no advance — AC2).
        if result.checkpoint_advanced:
            logger.warning(
                "change-ingest: connector=%s org=%s FAILED after %d batch(es) — "
                "first-run load interrupted; %d batch(es) checkpointed, resumes "
                "from last known-good next run. (%s: %s)",
                connector_id,
                org_id,
                result.batches,
                result.batches_checkpointed,
                type(exc).__name__,
                exc,
            )
        else:
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
