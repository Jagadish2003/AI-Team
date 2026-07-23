"""MSP-B1 / AT-641 (T1) — the shared cloud-connector skeleton.

The native cloud connectors (AWS/MSP-B1, Azure/MSP-B2) do the same four things;
only the provider edge differs. This module builds that common shape ONCE — the
poll loop, the per-scope checkpoints, the MSP-B0 mapper invocation, and the B7
admission hand-off — and both native connectors consume it. It is the direct
application of the R17-A3/A4 Java/.NET "share the extraction, not just the idea"
discipline to clouds: :mod:`cloud_event_connector` is to AWS/Azure what
:mod:`operational_ingest` is to Java/.NET.

The skeleton IS the contract with MSP-B2 (AT-641): if B2 finds it must fork this
to ingest Azure, that is a design defect to surface early — an Azure connector is
this skeleton with ``provider='azure'`` and Azure scopes, nothing more. The
connector adds NO detector-visible fields of its own; it is transport plus the
MSP-B0 contract.

The four responsibilities (per staged concern)
-----------------------------------------------
1. **Poll loop.** For each configured *scope* (a managed account/subscription ×
   provider surface, e.g. ``cloudwatch`` in ``us-east-1``) the connector pages
   forward from that scope's last position via an injectable
   :class:`CloudPollSource`. The source is the ONLY provider-specific edge — a
   live client (boto3 / Azure SDK) in production, an in-memory
   :class:`StaticCloudPollSource` offline/in tests.
2. **Per-scope checkpoints.** One ``(org_id, connector_id)`` checkpoint row is
   persisted by the runner, but a deployment polls many scopes each with its own
   position. The opaque checkpoint value encodes a per-scope position MAP
   (``{"v":1,"scopes":{scope_key: position}}``) — a scope absent from the map is
   read from the beginning, so a first load is resumable (R16-A1 §3). The runner
   persists/returns the value verbatim and never interprets it (R16-A1 AC5).
3. **Mapper invocation.** Each raw provider payload is normalised through the
   MSP-B0 reference mapper named on its scope (``discovery.signals.reference_mappers``),
   so every event is the identical detector-visible :class:`OperationalEvent`
   shape a detector reasons over — a detector never branches on provider.
4. **Admission hand-off (B7).** Every mapped event is handed to an
   :class:`~discovery.signals.ops_stream.OpsEventStream`, so re-firing events fold
   into one active signal with an occurrence count at the door (MSP-B7 dedup) and
   the per-run event budget is enforced. :meth:`active_signals` exposes the folded,
   deduplicated view; :meth:`budget_report` exposes the deferral proof.

Transport equivalence with the bridge (AT-641 AC4)
--------------------------------------------------
The connector re-stamps each event's ``source_system`` to the provider family
(``'aws'`` / ``'azure'``) while PRESERVING the mapper's ``event_signature`` —
exactly mirroring the way :mod:`ops_event_bridge` re-stamps to
``'bridge:<provider>'``. So a natively-ingested event and its bridged twin are
detector-identical except for that one field (``'aws'`` vs ``'bridge:aws'``). The
raw provider payload is stored against the event's OBSERVED evidence pointer so a
finding still traces back to the original event, and is never embedded in the
detector-visible model (MSP-B0 / AT-638).

Deletes / tombstones (R16-A1 §5): ``reports_deletes = False`` — a cloud event
stream is append-only observation history; a fired alarm or a logged API call is
never retracted upstream, so there is no deletion to propagate. The limitation is
declared, not faked.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

from discovery.signals.evidence_store import RawEventStore
from discovery.signals.operational_event import OperationalEvent
from discovery.signals.ops_stream import (
    DEFAULT_ACTIVE_PERIOD_SECONDS,
    Admission,
    OpsEventStream,
)
from discovery.signals.reference_mappers import MAPPERS

from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch

logger = logging.getLogger(__name__)

#: Opaque-checkpoint schema version, so a future shape change is detectable.
CHECKPOINT_VERSION = 1

#: ``source_artifact_type`` stamped on every event's OBSERVED evidence pointer.
CLOUD_EVENT_ARTIFACT_TYPE = "cloud_event"


# ─────────────────────────────────────────────────────────────────────────────
# Scope + poll-page value objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CloudScope:
    """One unit of polling with its own checkpoint position.

    A scope is a managed account/subscription × provider surface (× region),
    e.g. CloudWatch alarm history in account ``111122223333`` / ``us-east-1``.
    ``mapper`` is the MSP-B0 reference-mapper NAME (a key of
    :data:`discovery.signals.reference_mappers.MAPPERS`) that normalises this
    surface's raw payloads — keeping scopes pure data so a poll source can be
    seeded from config/fixtures without importing the mappers.
    """

    provider: str                     # 'aws' | 'azure'
    account: str                      # managed account / subscription id
    surface: str                      # 'cloudwatch' | 'eventbridge' | 'cloudtrail' | ...
    mapper: str                       # MSP-B0 mapper name (resolved via MAPPERS)
    region: Optional[str] = None
    label: Optional[str] = None       # optional human display label

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("CloudScope.provider is required")
        if not self.account:
            raise ValueError("CloudScope.account is required")
        if not self.surface:
            raise ValueError("CloudScope.surface is required")
        if not self.mapper:
            raise ValueError("CloudScope.mapper is required")

    @property
    def scope_key(self) -> str:
        """Stable per-scope key used to index this scope's checkpoint position."""
        return f"{self.provider}:{self.account}:{self.region or '*'}:{self.surface}"


@dataclass
class PollPage:
    """One page of raw provider payloads returned by a :class:`CloudPollSource`.

    ``events`` are raw provider payloads (mapped by the connector, never here).
    ``next_position`` is the opaque per-scope position AFTER this page; the
    connector stores it in the checkpoint map. ``has_more`` drives the poll loop:
    while ``True`` the connector keeps paging this scope.
    """

    events: List[Dict[str, Any]] = field(default_factory=list)
    next_position: str = ""
    has_more: bool = False


class CloudPollSource(ABC):
    """The provider edge — the ONLY provider-specific part of a native connector.

    Production implementations wrap a live client (boto3 CloudWatch/EventBridge/
    CloudTrail, the Azure Monitor/Activity Log SDK); offline/tests use
    :class:`StaticCloudPollSource`. Kept deliberately tiny so a native connector
    is "this skeleton + a poll source", never a fork of the poll loop.
    """

    @abstractmethod
    def list_scopes(self, org_id: str) -> List[CloudScope]:
        """Return the scopes to poll for ``org_id`` (from config, never scanning)."""
        raise NotImplementedError

    @abstractmethod
    def poll(self, org_id: str, scope: CloudScope, position: str) -> PollPage:
        """Return the next page of raw payloads for ``scope`` after ``position``."""
        raise NotImplementedError


class StaticCloudPollSource(CloudPollSource):
    """In-memory :class:`CloudPollSource` — the offline/test implementation.

    Seeded with ``(scope, [raw_event, ...])`` pairs and pages deterministically by
    integer offset into each scope's event list (the opaque position is the
    consumed-count as a string). Newest-position semantics are the caller's: seed
    events oldest-first and an incremental run resumes past the stored offset.
    """

    def __init__(
        self,
        scope_events: Optional[List[Any]] = None,
        *,
        page_size: int = 500,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        self.page_size = page_size
        # scope_key -> (scope, [events])
        self._scopes: Dict[str, Any] = {}
        for scope, events in (scope_events or []):
            self.add_scope(scope, events)

    def add_scope(self, scope: CloudScope, events: List[Dict[str, Any]]) -> None:
        """Register (or extend) a scope's seeded event list."""
        existing = self._scopes.get(scope.scope_key)
        if existing is None:
            self._scopes[scope.scope_key] = (scope, list(events))
        else:
            existing[1].extend(events)

    def list_scopes(self, org_id: str) -> List[CloudScope]:
        return [scope for scope, _ in self._scopes.values()]

    def poll(self, org_id: str, scope: CloudScope, position: str) -> PollPage:
        entry = self._scopes.get(scope.scope_key)
        if entry is None:
            return PollPage(events=[], next_position=position or "0", has_more=False)
        _, events = entry
        start = _int_position(position)
        end = min(start + self.page_size, len(events))
        return PollPage(
            events=list(events[start:end]),
            next_position=str(end),
            has_more=end < len(events),
        )


def _int_position(position: str) -> int:
    """Decode a :class:`StaticCloudPollSource` offset position (tolerant)."""
    try:
        return int(position or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# The shared connector skeleton
# ─────────────────────────────────────────────────────────────────────────────

class CloudEventConnector(ChangeBasedIngestor):
    """Shared change-based ingestor for native cloud-event sources (AT-641 T1).

    Owns the poll loop, per-scope checkpoint map, MSP-B0 mapper invocation, raw
    evidence storage, and the B7 admission hand-off. A concrete native connector
    sets ``provider`` / ``connector_id`` (as class attributes on a subclass, e.g.
    :class:`~discovery.ingest.aws_event_connector.AWSEventConnector`) or passes
    them to ``__init__``, and supplies a :class:`CloudPollSource`. Everything else
    is shared — that shared-ness IS the contract with MSP-B2.

    The admission stream is stateful for the lifetime of the connector: after
    :meth:`ingest_changes` has been driven, :meth:`active_signals` returns the
    folded, deduplicated signals and :meth:`budget_report` the deferral proof.
    """

    provider: str = ""            # 'aws' | 'azure' — set by subclass or __init__
    connector_id: str = ""        # stable runner key — set by subclass or __init__
    reports_deletes = False       # append-only observation stream (see module docstring)

    def __init__(
        self,
        poll_source: CloudPollSource,
        *,
        provider: Optional[str] = None,
        connector_id: Optional[str] = None,
        raw_store: Optional[RawEventStore] = None,
        stream: Optional[OpsEventStream] = None,
        active_period_seconds: int = DEFAULT_ACTIVE_PERIOD_SECONDS,
        budget: Optional[int] = None,
    ) -> None:
        if provider:
            self.provider = provider
        if connector_id:
            self.connector_id = connector_id
        if not self.provider:
            raise ValueError("CloudEventConnector requires a provider ('aws'/'azure')")
        if not self.connector_id:
            raise ValueError("CloudEventConnector requires a connector_id")
        self.poll_source = poll_source
        self.raw_store = raw_store
        self.stream = stream if stream is not None else OpsEventStream(
            active_period_seconds=active_period_seconds, budget=budget
        )

    # ── ChangeBasedIngestor contract ─────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of natively-ingested operational events since ``since``.

        Polls every scope forward from its stored position, normalises each raw
        payload through its MSP-B0 mapper, re-stamps ``source_system`` to the
        provider family, stores the raw payload against the event's evidence
        pointer, and hands the event to B7 admission. First run (``since is
        None``): a full poll of every scope, streamed as checkpointed pages
        (resumable). An idle poll yields a single empty :class:`DeltaBatch` whose
        ``next_checkpoint`` echoes the incoming per-scope map.

        The records carry the per-event normalised event (the same shape the
        bridge emits); the DEDUPLICATED view is the admission stream, read via
        :meth:`active_signals`.
        """
        if not org_id or not str(org_id).strip():
            raise ValueError("org_id is required")

        positions = _decode_positions(since.value if since else None)
        running: Dict[str, str] = dict(positions)

        scopes = self.poll_source.list_scopes(org_id)
        logger.info(
            "%s: org=%s %s — %d scope(s)",
            self.connector_id, org_id,
            "first run (full poll)" if since is None else "incremental poll",
            len(scopes),
        )

        # Poll + admit everything up front (admission is a side effect into the
        # stream), collecting the record-bearing pages so exactly one terminal
        # batch can be flagged is_complete=True — the runner needs one terminal
        # batch to advance the checkpoint (R16-A1 / AT-378). Event VOLUME is bound
        # by the B7 per-run budget enforced inside admission, not by this buffer.
        pages_out: List[tuple] = []  # (records, positions_snapshot)
        for scope in scopes:
            if scope.provider != self.provider:
                logger.debug(
                    "%s: skipping scope %s (provider %s != %s)",
                    self.connector_id, scope.scope_key, scope.provider, self.provider,
                )
                continue
            mapper = MAPPERS.get(scope.mapper)
            if mapper is None:
                logger.warning(
                    "%s: no MSP-B0 mapper %r for scope %s — scope skipped",
                    self.connector_id, scope.mapper, scope.scope_key,
                )
                running.setdefault(scope.scope_key, positions.get(scope.scope_key, ""))
                continue

            pos = positions.get(scope.scope_key, "")
            while True:
                page = self.poll_source.poll(org_id, scope, pos)
                records: List[Dict[str, Any]] = []
                for raw in page.events:
                    rec = self._process(org_id, scope, mapper, raw)
                    if rec is not None:
                        records.append(rec)
                pos = page.next_position or pos
                running[scope.scope_key] = pos
                if records:
                    pages_out.append((records, dict(running)))
                if not page.has_more:
                    break

        if not pages_out:
            # Idle poll → empty delta echoing the (possibly advanced) positions.
            yield DeltaBatch(records=[], next_checkpoint=_encode_positions(running), is_complete=True)
            return

        # The terminal batch must carry the COMPLETE final position map (scopes
        # polled after the last record-bearing page still advanced their cursor).
        pages_out[-1] = (pages_out[-1][0], dict(running))
        last = len(pages_out) - 1
        for i, (records, snapshot) in enumerate(pages_out):
            yield DeltaBatch(
                records=records,
                next_checkpoint=_encode_positions(snapshot),
                is_complete=(i == last),
            )

    # ── Mapping + admission + emission ───────────────────────────────────────
    def _process(
        self, org_id: str, scope: CloudScope, mapper, raw: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Normalise one raw payload, store its evidence, admit it, shape a record.

        Returns ``None`` (loud-skip) when the mapper raises — a single malformed
        payload must never wedge the whole poll, mirroring the bridge's loud-skip
        discipline. Returns ``None`` (no record) for an event the admission layer
        classified as an exact re-delivery (idempotent) or deferred by the budget
        — both are handled by B7, not silently dropped.
        """
        try:
            event = mapper(raw, org_id=org_id)
        except Exception:  # mappers are meant to be tolerant; stay robust anyway
            logger.warning(
                "%s: mapper %s failed on a %s payload — skipped",
                self.connector_id, getattr(mapper, "__name__", mapper), scope.surface,
                exc_info=True,
            )
            return None

        # Re-stamp the transport to the provider family WITHOUT recomputing the
        # recurrence signature (the mapper derived it from the same family), so a
        # native event equals its bridged twin except for source_system (AC4).
        event.source_system = self.provider
        # Re-point provenance at the native cloud artifact, keyed so it resolves
        # through the raw-event store.
        event.provenance = EvidencePointer.observed(
            source_system=self.provider,
            source_artifact=event.signal_id,
            source_timestamp=event.observed_at,
            source_artifact_type=CLOUD_EVENT_ARTIFACT_TYPE,
        ).to_dict()

        # Persist the raw payload so the evidence pointer resolves back to the
        # original provider event (MSP-B0 / AT-638). The detector-visible event
        # never embeds it.
        if self.raw_store is not None:
            self.raw_store.put(org_id, self.provider, event.signal_id, raw)

        # Admission hand-off (B7 dedup + budget): re-fires fold into one active
        # signal with a count; an exact re-delivery is idempotent; a budget-
        # exhausted event is deferred-and-counted.
        admission: Admission = self.stream.admit(event)
        if admission.is_deferred or admission.is_duplicate:
            return None
        return self._to_record(scope, event, admission)

    def _to_record(
        self, scope: CloudScope, event: OperationalEvent, admission: Admission
    ) -> Dict[str, Any]:
        """Shape one natively-ingested event into a :class:`DeltaBatch` record.

        The detector-visible, provider-agnostic ``event`` (identical in shape to a
        bridge record's ``event``) wrapped with the change vocabulary and native
        trace-back metadata — ``provider_event_id`` for dedupe, the scope's
        ``surface``/``account``/``region`` for source trace, the ``evidence_pointer``
        that resolves to the raw payload, and the B7 admission ``disposition``.
        """
        return {
            "artifact_id": f"{self.provider}:{event.signal_id}",
            "change_kind": ChangeKind.CREATED,
            "source_system": self.provider,
            "provider": self.provider,
            "surface": scope.surface,
            "account": scope.account,
            "region": scope.region,
            "provider_event_id": event.signal_id,
            "event_signature": event.event_signature,
            "event": event.to_dict(),
            "evidence_pointer": event.provenance,
            "admission": admission.disposition,
        }

    # ── Admission read side (the deduplicated view) ──────────────────────────
    def active_signals(self, org_id: Optional[str] = None):
        """The folded, deduplicated active signals produced by admission (AC5)."""
        return self.stream.active_signals(org_id)

    def budget_report(self):
        """The run's B7 event-budget outcome (deferred volume; loud degradation)."""
        return self.stream.budget_report()


# ─────────────────────────────────────────────────────────────────────────────
# Opaque per-scope checkpoint map (encode/decode — opaque to the runner)
# ─────────────────────────────────────────────────────────────────────────────

def _encode_positions(positions: Dict[str, str]) -> str:
    """Encode the per-scope position map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": CHECKPOINT_VERSION, "scopes": {k: str(v) for k, v in positions.items()}},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_positions(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-scope position map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (poll every scope from the beginning) rather than raising — a degenerate
    checkpoint degrades to a safe full re-read, never a crash.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "cloud_event_connector: could not decode checkpoint value; treating "
            "as first run (full re-poll)."
        )
        return {}
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if not isinstance(scopes, dict):
        return {}
    return {str(k): str(v) for k, v in scopes.items()}
