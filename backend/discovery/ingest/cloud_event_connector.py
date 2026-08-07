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

Bounded per-run work (MSP-B1 — the poll loop must end)
-----------------------------------------------------
A scope's backlog can be far larger than one poll (CloudTrail's ``LookupEvents``
retains 90 days and is rate-limited to a couple of calls a second), so the
continuation loop is BOUNDED per run by three rules, checked only between polls of
the same scope:

1. **Event budget** — MSP-B7 T4's per-run budget stops the *fetching*, not just the
   admission. Once :meth:`OpsEventStream.has_capacity` is False every further event
   would be deferred anyway, so continuing to page the provider buys nothing.
2. **Per-scope poll cap** (:data:`DEFAULT_MAX_POLLS_PER_SCOPE`) — one scope's
   backlog can never monopolise the run. The cap counts TOTAL polls of a scope in
   one run, first poll included: ``4`` means at most four polls, ``1`` means poll
   once and never continue, ``0`` means unbounded. (Counting continuations instead
   would leave "poll once, never continue" inexpressible, since ``0`` is already
   taken by unbounded.)
3. **Wall-clock deadline** (:data:`DEFAULT_POLL_DEADLINE_SECONDS`) — volume is not
   time: a throttled provider can spend minutes on a single page. The deadline is
   consulted for CONTINUATION polls only, so every scope still gets its first poll
   and a late scope is never starved by an earlier one's backlog.

Stopping early is NOT truncation: the scope's advanced position rides the terminal
batch's checkpoint, so the next run resumes exactly where this one stopped. Every
early stop is logged at WARNING and reported by :meth:`poll_report` (the R18-C2
connector-panel artifact), because a partial ingest must never read as a clean one.
"""
from __future__ import annotations

import json
import logging
import os
import time
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

#: Default cap on TOTAL polls of a single scope within one run — the first poll
#: included, so ``4`` is four polls (three of them continuations) and ``1`` is a
#: single poll with no continuation. Each poll already follows the provider's own
#: pagination internally, so this bounds a scope to a few thousand events per run
#: and leaves the remainder to the next run. ``0`` = unbounded.
DEFAULT_MAX_POLLS_PER_SCOPE = 4

#: Default wall-clock budget (seconds) for one run's whole poll phase. Bounds TIME
#: rather than volume, which is what a rate-limited/throttled provider actually
#: costs. Consulted for continuation polls only. ``0`` = unbounded.
DEFAULT_POLL_DEADLINE_SECONDS = 180.0

#: Why a scope stopped paging before its backlog drained (reported, never silent).
STOP_BUDGET = "event_budget_exhausted"
STOP_POLL_CAP = "max_polls_per_scope"
STOP_DEADLINE = "poll_deadline_seconds"


def _configured_max_polls_per_scope() -> int:
    """Per-scope TOTAL-poll cap from ``CLOUD_EVENT_MAX_POLLS_PER_SCOPE``.

    ``1`` polls each scope once and never continues; ``0`` is unbounded.

    The env name is spelled literally (not via a constant) so the ingest-layer env
    guard can statically confirm it is a numeric tuning knob and not a credential.
    """
    raw = os.environ.get("CLOUD_EVENT_MAX_POLLS_PER_SCOPE")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_POLLS_PER_SCOPE
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "cloud_event_connector: CLOUD_EVENT_MAX_POLLS_PER_SCOPE=%r is not an "
            "integer — using the default %d", raw, DEFAULT_MAX_POLLS_PER_SCOPE,
        )
        return DEFAULT_MAX_POLLS_PER_SCOPE
    return max(0, value)


def _configured_poll_deadline_seconds() -> float:
    """Poll-phase wall-clock budget from ``CLOUD_EVENT_POLL_DEADLINE_SECONDS``.

    Literal env name for the same reason as above.
    """
    raw = os.environ.get("CLOUD_EVENT_POLL_DEADLINE_SECONDS")
    if raw is None or not str(raw).strip():
        return DEFAULT_POLL_DEADLINE_SECONDS
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.warning(
            "cloud_event_connector: CLOUD_EVENT_POLL_DEADLINE_SECONDS=%r is not a "
            "number — using the default %s", raw, DEFAULT_POLL_DEADLINE_SECONDS,
        )
        return DEFAULT_POLL_DEADLINE_SECONDS
    return max(0.0, value)


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
    #: A cloud event is an observation, never an indexed retrieval artifact — so the
    #: change runner must not emit per-event artifact_changed/freshness work for it
    #: (see ChangeBasedIngestor.produces_retrieval_content).
    produces_retrieval_content = False

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
        max_polls_per_scope: Optional[int] = None,
        poll_deadline_seconds: Optional[float] = None,
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
        self.max_polls_per_scope = (
            _configured_max_polls_per_scope()
            if max_polls_per_scope is None
            else max(0, int(max_polls_per_scope))
        )
        self.poll_deadline_seconds = (
            _configured_poll_deadline_seconds()
            if poll_deadline_seconds is None
            else max(0.0, float(poll_deadline_seconds))
        )
        #: scope_key -> stop reason, for scopes whose backlog was not drained in the
        #: last run (loud: read via :meth:`poll_report`). Reset per ingest_changes.
        self._backlog_remaining: Dict[str, str] = {}
        self._polls_performed = 0
        self._scopes_polled = 0

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

        Per-run work is BOUNDED (see the module docstring): a scope stops paging on
        the event budget, the per-scope poll cap, or the wall-clock deadline, and the
        undrained remainder resumes from the persisted position next run. Every early
        stop is logged and reported by :meth:`poll_report`.
        """
        if not org_id or not str(org_id).strip():
            raise ValueError("org_id is required")

        positions = _decode_positions(since.value if since else None)
        running: Dict[str, str] = dict(positions)
        self._backlog_remaining = {}
        self._polls_performed = 0
        self._scopes_polled = 0
        started_at = time.monotonic()

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
        # batch to advance the checkpoint (R16-A1 / AT-378). This buffer is bounded
        # by the per-run poll bounds below (poll cap × deadline × B7 budget), which
        # is what keeps a huge provider backlog from being buffered in one run.
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
            polls = 0
            self._scopes_polled += 1
            while True:
                page = self.poll_source.poll(org_id, scope, pos)
                polls += 1
                self._polls_performed += 1
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
                # The scope reports a remaining backlog. Continue only while all
                # three per-run bounds allow it; otherwise stop LOUDLY and leave the
                # remainder to the next run (the advanced position is checkpointed,
                # so nothing is dropped — this is resume, not truncation).
                stop_reason = self._continuation_stop_reason(polls, started_at)
                if stop_reason is not None:
                    self._backlog_remaining[scope.scope_key] = stop_reason
                    logger.warning(
                        "%s: scope %s still has a backlog after %d poll(s) — stopping "
                        "this run (%s); the remainder resumes from the checkpointed "
                        "position on the next run (no events dropped).",
                        self.connector_id, scope.scope_key, polls, stop_reason,
                    )
                    break

        # Runtime visibility: the mapping + admission outcome for the whole poll —
        # how many provider payloads became OperationalEvents, how many DISTINCT
        # active signals they folded into (B7 T1 dedup), and whether the per-run
        # budget deferred anything (B7 T4). Without this, "events were fetched" and
        # "OperationalEvents reached the detectors" were indistinguishable in logs.
        _mapped = sum(len(recs) for recs, _ in pages_out)
        try:
            _folded = len(self.active_signals())
        except Exception:  # pragma: no cover - observability must never break a poll
            _folded = -1
        _budget = None
        try:
            _budget = self.budget_report()
        except Exception:  # pragma: no cover - same
            pass
        logger.info(
            "%s: org=%s mapped %d OperationalEvent(s) -> %d active signal(s) "
            "across %d poll(s) of %d scope(s)%s%s",
            self.connector_id,
            org_id,
            _mapped,
            _folded,
            self._polls_performed,
            self._scopes_polled,
            (
                f"; budget deferred {_budget.deferred} of {_budget.seen}"
                if _budget is not None and getattr(_budget, "deferred", 0)
                else ""
            ),
            (
                f"; {len(self._backlog_remaining)} scope(s) still have a backlog "
                f"(resumes next run): {sorted(self._backlog_remaining)}"
                if self._backlog_remaining
                else ""
            ),
        )

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

    # ── Per-run bounds ───────────────────────────────────────────────────────
    def _continuation_stop_reason(
        self, polls_done: int, started_at: float
    ) -> Optional[str]:
        """Why this scope must stop paging now, or ``None`` to keep going.

        Called ONLY when a scope reports a remaining backlog, and only after at least
        one poll of that scope — so no scope is ever starved of its first poll by an
        earlier scope's backlog or by the deadline.

        ``polls_done`` is the number of polls of THIS scope already performed in this
        run (>= 1 here). The poll cap is compared against it directly, so the cap is
        a bound on total polls per scope, first poll included.
        """
        # 1. B7 budget: past it every further event is deferred at admission, so
        #    fetching more provider pages buys nothing (MSP-B7 T4 — the budget must
        #    stop the run PROCESSING everything, which includes the fetch).
        try:
            if not self.stream.has_capacity():
                return STOP_BUDGET
        except Exception:  # a stream without the read side — never silently
            # This bound is one of only three that terminate the poll loop, so
            # losing it must be visible: with the poll cap and the deadline both
            # disabled the loop would otherwise revert to the unbounded hang this
            # module exists to prevent. The other two bounds still apply below.
            logger.warning(
                "%s: budget bound unavailable — stream.has_capacity() failed, so the "
                "poll loop is bounded only by the poll cap (%s) and deadline (%s)",
                self.connector_id, self.max_polls_per_scope, self.poll_deadline_seconds,
                exc_info=True,
            )
        # 2. Per-scope poll cap (TOTAL polls of one scope in this run).
        if self.max_polls_per_scope and polls_done >= self.max_polls_per_scope:
            return STOP_POLL_CAP
        # 3. Wall-clock deadline for the whole poll phase.
        if (
            self.poll_deadline_seconds
            and (time.monotonic() - started_at) >= self.poll_deadline_seconds
        ):
            return STOP_DEADLINE
        return None

    def poll_report(self) -> Dict[str, Any]:
        """The last run's poll-phase outcome — the R18-C2 connector-panel artifact.

        ``backlog_remaining`` maps each scope that did NOT drain to the bound that
        stopped it, so a partial ingest is visible as a fact instead of looking like
        a clean one. ``complete`` is True only when every polled scope drained.
        """
        return {
            "scopes_polled": self._scopes_polled,
            "polls": self._polls_performed,
            "max_polls_per_scope": self.max_polls_per_scope,
            "poll_deadline_seconds": self.poll_deadline_seconds,
            "backlog_remaining": dict(self._backlog_remaining),
            "complete": not self._backlog_remaining,
        }

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
        ``surface``/``account_scope``/``region`` for source trace, the
        ``evidence_pointer`` that resolves to the raw payload, and the B7 admission
        ``disposition``. ``account_scope`` names the managed account/subscription
        the event was ingested from — the MSP "one connection, many accounts, each
        account a scope" model (MSP-B1 / AT-642); it rides every record so a
        multi-account run never loses which account a signal belongs to.
        """
        return {
            "artifact_id": f"{self.provider}:{event.signal_id}",
            "change_kind": ChangeKind.CREATED,
            "source_system": self.provider,
            "provider": self.provider,
            "surface": scope.surface,
            "account_scope": scope.account,
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
