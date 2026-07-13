"""
R16-A1 / AT-377 — Change-Based Ingestion Foundation: the checkpoint contract.

This module defines the three core data structures every 1.6+ connector is built
against. The single most important scaling rule: at enterprise scale a source
cannot be re-read in full on every discovery run, so each connector reports only
what changed since the last successful run and returns a new position marker.

Contract (per R16-A1 Section 1):
  * Checkpoint           — an OPAQUE per-source position marker. Each connector
                           defines its own internal ``value`` shape (an ISO
                           timestamp, a commit SHA, an updated-sequence id, ...).
                           The runner persists/returns it and NEVER interprets it.
  * DeltaBatch           — a page of changed records plus the new checkpoint
                           value after the batch, and whether more pages remain.
  * ChangeBasedIngestor  — the interface every connector implements. The runner
                           drives it via ``ingest_changes(org_id, since)``.

Checkpoint persistence (AT-378), runner integration (AT-379/AT-380), change-event
emission (AT-381) and the admin reset (AT-383) are separate stories. The
delete/tombstone vocabulary that change detection relies on — ``ChangeKind``,
the ``tombstone()`` helper, and the ``reports_deletes`` capability flag — lives
HERE as part of the contract (AT-382, §5: a record that disappears from a source
is a real change that must propagate). This file still takes no dependency on the
runner, the database, or the telemetry path.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 string — used to stamp ``captured_at``."""
    return datetime.now(timezone.utc).isoformat()


class ChangeKind:
    """The kind of change a delta record represents (R16-A1 §4 / §5).

    Carried as the ``change_kind`` field of each record dict in a
    :class:`DeltaBatch` and surfaced verbatim on the ``ingestion.artifact_changed``
    event (AT-381). ``DELETED`` is the tombstone signal — an artifact that
    disappeared from the source (AT-382, §5) — so downstream consumers (Release
    1.8 retrieval freshness) can stop treating it as live evidence.
    """

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


#: Every recognised change kind. A connector should set each record's
#: ``change_kind`` to one of these; the runner defaults a missing value to
#: ``UPDATED`` (the record is, after all, in a delta).
CHANGE_KINDS: frozenset[str] = frozenset(
    {ChangeKind.CREATED, ChangeKind.UPDATED, ChangeKind.DELETED}
)


def tombstone(artifact_id: str, **fields) -> dict:
    """Build a tombstone (delete) record for a removed source artifact (§5 / AT-382).

    A connector whose source NATIVELY reports deletions yields these in its
    :class:`DeltaBatch` so the deletion propagates downstream as a
    ``change_kind='deleted'`` event (e.g. a removed Confluence page stops
    contributing evidence). Any extra ``fields`` are merged in for connectors that
    attach per-record metadata; ``artifact_id`` and ``change_kind`` are always set.

    Connectors that CANNOT detect deletions must NOT fabricate tombstones. They
    declare the gap with ``ChangeBasedIngestor.reports_deletes = False`` and
    document why in their class docstring — the foundation never silently pretends
    deletes are caught when they are not.
    """
    if not artifact_id or not isinstance(artifact_id, str):
        raise ValueError("tombstone() requires a non-empty string artifact_id")
    return {**fields, "artifact_id": artifact_id, "change_kind": ChangeKind.DELETED}


@dataclass(frozen=True)
class Checkpoint:
    """Opaque per-source position marker.

    Each connector defines its own internal shape for ``value`` (a timestamp, a
    commit SHA, an updated-sequence id, etc.) but the runner treats it as opaque
    and just persists and returns it. One row per ``(org_id, connector_id)`` is
    persisted by the runner — never by the connector.

    Frozen so a checkpoint handed to a connector cannot be mutated in-flight;
    advancing a position means producing a NEW Checkpoint, never editing one.
    """

    connector_id: str
    org_id: str
    value: str        # connector-defined: ISO timestamp, SHA, seq, etc. (opaque)
    captured_at: str  # when this checkpoint was recorded (UTC ISO)

    def __post_init__(self) -> None:
        if not self.connector_id or not isinstance(self.connector_id, str):
            raise ValueError("Checkpoint.connector_id must be a non-empty string")
        if not self.org_id or not isinstance(self.org_id, str):
            raise ValueError("Checkpoint.org_id must be a non-empty string")
        # value is opaque but must be a string the runner can persist verbatim.
        if not isinstance(self.value, str):
            raise ValueError("Checkpoint.value must be a string (opaque to runner)")
        if not isinstance(self.captured_at, str) or not self.captured_at:
            raise ValueError("Checkpoint.captured_at must be a non-empty ISO string")

    @classmethod
    def create(cls, connector_id: str, org_id: str, value: str) -> "Checkpoint":
        """Build a checkpoint, stamping ``captured_at`` with the current UTC time.

        Convenience for connectors that have computed a new opaque ``value`` and
        just need the timestamp filled in.
        """
        return cls(
            connector_id=connector_id,
            org_id=org_id,
            value=value,
            captured_at=_utc_iso(),
        )


@dataclass
class DeltaBatch:
    """One page of changed records emitted by a connector.

    A connector yields these from ``ingest_changes``. ``next_checkpoint`` is the
    opaque position value AFTER this batch; the runner persists it only once the
    final batch (``is_complete=True``) has been fully processed (see AT-378).
    """

    records: list[dict] = field(default_factory=list)  # only the changed records
    next_checkpoint: str = ""   # the new opaque position value after this batch
    is_complete: bool = True    # False if more pages remain (pagination)

    def __post_init__(self) -> None:
        if not isinstance(self.records, list):
            raise ValueError("DeltaBatch.records must be a list of dicts")
        if any(not isinstance(r, dict) for r in self.records):
            raise ValueError("DeltaBatch.records must contain only dicts")
        # next_checkpoint mirrors Checkpoint.value: opaque, but a string.
        if not isinstance(self.next_checkpoint, str):
            raise ValueError("DeltaBatch.next_checkpoint must be a string (opaque)")
        if not isinstance(self.is_complete, bool):
            raise ValueError("DeltaBatch.is_complete must be a bool")

    @property
    def is_empty(self) -> bool:
        """True when the source reported no changes for this batch."""
        return len(self.records) == 0


class ChangeBasedIngestor(abc.ABC):
    """Interface every connector implements.

    The runner calls ``ingest_changes()`` with the last checkpoint and persists
    the returned ``next_checkpoint`` only on full success. A connector that
    re-reads its whole source each run does not satisfy this contract.

    Subclasses MUST set the ``connector_id`` class attribute (the stable id the
    runner keys checkpoints by) and implement ``ingest_changes``.

    Deletes / tombstones (R16-A1 §5 / AT-382)
    -----------------------------------------
    Change detection must handle deletions, not only creates and updates — a
    record that disappears from a source is a real change that must propagate.

      * If the source NATIVELY reports deletions, set ``reports_deletes = True``
        and yield a :func:`tombstone` record (``change_kind='deleted'``) for each
        removed artifact. The runner emits it as a ``change_kind='deleted'`` event.
      * If the source CANNOT report deletions, leave ``reports_deletes = False``
        (the default) and document WHY in the subclass docstring. The foundation
        does not silently pretend deletes are caught — the False flag is the
        explicit, inspectable declaration of that limitation.
    """

    connector_id: str = ""

    #: R16-A1 §5 / AT-382 — does this connector's source natively report
    #: deletions? DEFAULT False: a source is assumed UNABLE to report deletes
    #: unless a connector explicitly declares otherwise (and then yields
    #: ``tombstone()`` records). A connector that cannot detect deletes leaves
    #: this False and documents the limitation in its docstring — making the gap
    #: explicit rather than silently unhandled.
    reports_deletes: bool = False

    #: R18-A4 / AT-596 (T3) — does this connector drive retrieval freshness ITSELF?
    #: DEFAULT False: the change runner notifies the retrieval-freshness subscriber
    #: for each changed record, whose ``artifact_id`` maps 1:1 to a retrieval
    #: ``source_artifact`` (documents, git, Confluence, SharePoint). A conversation
    #: connector (Slack/Teams) indexes at the THREAD level while its change records
    #: are per-MESSAGE, so message-level freshness would be the wrong granularity —
    #: it sets this True and drives thread-level freshness from its deep-content path
    #: instead, and the runner then skips the generic per-record freshness
    #: notification for it (the ``ingestion.artifact_changed`` telemetry event is
    #: still emitted). See ``discovery.ingest.conversation_content``.
    manages_retrieval_freshness: bool = False

    @abc.abstractmethod
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed records since ``since``.

        If ``since`` is None (first run), perform an initial full load, but
        STREAM it as batches with checkpoints so a failure mid-load can resume
        rather than restart. When ``since`` is provided, ask the source only for
        what changed after ``since.value`` and yield those records.

        An unchanged source must yield no records (an empty delta) — either no
        batches at all, or a single ``DeltaBatch`` with empty ``records`` whose
        ``next_checkpoint`` echoes the incoming position.
        """
        raise NotImplementedError
