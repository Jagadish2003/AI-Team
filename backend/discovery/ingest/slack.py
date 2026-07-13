"""
R16-A2 / AT-416 (T1) — Slack change-based ingestor.

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for Slack. The single most important rule it honours: Slack is NOT
re-read in full on every discovery run. Slack's native change signal is message
timestamp (``ts``) ordering within a channel, so the connector encodes its
position as the opaque checkpoint value and asks each channel only for messages
newer than the last-seen ``ts``.

Scope (reach phase)
-------------------
This file is the change-based ingestor: checkpointed incremental message
ingestion plus a resumable, checkpointed first load (AT-416 / T1, AC2 + AC3).
Each delta record also carries an extracted ``signals`` block (cross-reference
markers + escalation signal) via :mod:`discovery.ingest.slack_signals`
(AT-417 / T2) so reach-phase signal travels with the delta, plus a fully
populated ``evidence_pointer`` (R16-B1, ``origin='observed'``) attached to every
record so each Slack signal is traceable back to its source message (AT-418 /
T3, AC5). The remaining downstream pieces are deliberately separate stories and
are NOT done here:

  * The Slack MEDIUM corroboration ceiling — T4 / AT-419.
  * OAuth connect wiring (auth-url / callback / vault) — T5 / AT-420 (the Slack
    OAuth config already exists in ``app/auth/configs.py``).
  * ``ingestion.artifact_changed`` event emission — handled by the shared runner
    (``change_runner.py``, AT-381); every record this ingestor yields already
    carries ``artifact_id`` + ``change_kind`` so the runner can emit them.

Per the reach/depth boundary (AC8), the reach path reads only structured message
*signal* — it carries message metadata (ts, author, reply/reaction counts, the
raw text for later cross-reference marker scanning) through to the records.

Deep-content path (R18-A4 / AT-594 — T1)
----------------------------------------
The 1.8 depth phase adds a CONTENT path BESIDE the unchanged signal path. It reads
the conversation TEXT itself: the same change-based delta records are assembled
into threads (or a time-bounded window of channel messages where no thread
structure exists — threads are the conversational unit), scope-checked against the
P5 channel selection so only explicitly selected channels are read (AC2), rendered
as author-attributed thread text, and each thread is handed to the R18-B1 retrieval
substrate via ``retrieval.ingest_content(org_id, artifacts)`` as a
``ContentArtifact`` carrying an ``origin='observed'`` thread-level evidence pointer
(AC1). The substrate owns everything after the hand-off (chunking under its
*conversation* policy, embedding, indexing) — this connector never writes vectors.
The reach-phase signal extraction is untouched and continues to feed scoring; the
depth path rides the SAME per-``(org, 'slack')`` checkpoint — no new connector, no
new checkpointing. See :meth:`SlackIngestor.ingest_deep_content`.

Edit/delete propagation (R18-A4 / AT-596 — T3)
----------------------------------------------
Edits and deletions are wired into R18-B2 freshness at the THREAD level (chunks are
stored per thread, so freshness must act per thread, not per message). Within one
change-runner pass, a message the delta marks ``updated`` (an edit) — or a
``created`` reply to a thread whose root is not in the batch — emits a thread-level
``updated`` event so R18-B2 marks the thread's chunks stale and queues an async
re-chunk of the WHOLE thread (via the registered content resolver
:func:`resolve_thread_content`); a ``deleted`` standalone message emits a
thread-level ``deleted`` event that purges it from retrieval immediately (B2's
delete rule). Only new/changed messages since the checkpoint are processed, so this
never re-reads full channel history (AC4). Slack history polling cannot itself
surface an edit whose ``ts`` is unchanged nor a deletion (``reports_deletes =
False``) — the propagation honours a delete/tombstone record whenever one is
produced (e.g. a future Events-API tombstone), and Teams (AT-595), whose Graph
delta natively re-surfaces edits and ``@removed`` deletions, exercises the full
incremental edit/delete path.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'slack')`` checkpoint row is persisted by the runner, but a
Slack workspace has many channels each with its own position. The connector
therefore encodes a per-channel cursor MAP as the opaque checkpoint value::

    {"v": 1, "channels": {"C001": "1718090000.000400", "C002": "1718004000.000200"}}

The runner never interprets this — it persists and returns the string verbatim
(R16-A1 AC5). Only this connector, which owns the shape, parses it back. A
channel absent from the map is read from the beginning, which is exactly what
makes a first load resumable: if the streamed first load fails partway, the next
run finds a checkpoint (incremental mode) whose map covers the channels already
loaded, resumes the partially-loaded channel from its last batch ``ts``, and
loads any not-yet-started channels in full. No records are skipped and the load
completes across runs.

Permissions / privacy (AC4)
---------------------------
Only public channels AgentIQ has been invited to are read. Private channels and
DMs are never accessed: :meth:`_accessible_channels` filters to
``is_private == False and is_member == True and is_archived == False``.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/slack_sample.json`` — parity with the Salesforce/ServiceNow/Jira/
GitHub connectors. Live: calls the Slack Web API (``conversations.list`` /
``conversations.history``) using the OAuth token from the per-run credential
context. Credentials are resolved exactly like the other connectors —
``get_live_connector('slack')`` first, then the env fallback for CLI use.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import get_live_connector, is_live, resolve_vault_connector
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .conversation_content import (
    CONTENT_TYPE,
    ConversationChange,
    ConversationDeepContentError,
    ConversationDeepContentResult,
    ConversationMessage,
    ConversationThread,
    assemble_threads,
    ingest_conversation_changes,
    normalise_change_kind,
    resolve_conversation_thread,
    thread_to_artifact,
)
from .slack_signals import (
    build_evidence_pointer,
    extract_cross_reference_markers,
    extract_escalation_signal,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "slack_sample.json"

#: The retrieval substrate's canonical source-system tag for Slack conversation
#: content (R18-B1 ``KNOWN_SOURCE_SYSTEMS``). Slack's connector id and its
#: substrate source-system happen to coincide (``'slack'``) — reporting content
#: under this exact name is also what keeps the conversation MEDIUM ceiling
#: applicable to a retrieval hit (AC6), so it must never be relabelled.
RETRIEVAL_SOURCE_SYSTEM = "slack"

#: Messages that carry no thread structure are grouped into a time-bounded window
#: so the conversational unit is never a lone, context-free message (R18-A4 §1 /
#: §4 "Threads are the unit of meaning"). Consecutive standalone messages within
#: this many seconds of the window's first message form one window "thread".
THREAD_WINDOW_SECONDS = 3600

#: R18-A4 deep-content types are shared with Teams (AT-595) — the two platforms
#: diverge only at the collection edge (:meth:`SlackIngestor._to_conversation_message`).
#: Re-exported under the Slack names the T1 API established.
SlackDeepContentError = ConversationDeepContentError
SlackDeepContentResult = ConversationDeepContentResult

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of messages emitted per :class:`DeltaBatch`. Kept modest so a
#: large initial load is streamed as many small, individually-checkpointed
#: batches (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Slack Web API base (live mode).
_SLACK_API_BASE = "https://slack.com/api"
_REQUEST_TIMEOUT = 30


class SlackIngestError(Exception):
    """Raised when live Slack ingestion fails with a clear, actionable message."""


def _encode_checkpoint(cursors: Dict[str, str]) -> str:
    """Encode the per-channel cursor map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "channels": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-channel cursor map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every channel from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "slack: could not decode checkpoint value; treating as first run "
            "(full re-read). value=%r",
            value,
        )
        return {}
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, dict):
        return {}
    # Keep only string→string entries; ignore anything malformed.
    return {str(k): str(v) for k, v in channels.items() if v is not None}


def _ts_gt(ts: str, cursor: Optional[str]) -> bool:
    """True when message ``ts`` is strictly newer than ``cursor``.

    Slack ``ts`` is an ``epoch.micro`` string. We compare by float (the shape's
    owner is allowed to interpret it) so ordering is correct even as the integer
    epoch part gains digits over time, where a plain lexicographic compare would
    eventually break.
    """
    if not cursor:
        return True
    try:
        return float(ts) > float(cursor)
    except (TypeError, ValueError):
        # Fall back to string compare if a ts is somehow non-numeric.
        return ts > cursor


def _ts_float(ts: Any) -> float:
    """Parse a Slack ``epoch.micro`` ts to a float; unparseable → -inf.

    Used only to ORDER messages within a thread/window deterministically, so a
    malformed ts sorts first rather than crashing the assembly.
    """
    try:
        return float(ts)
    except (TypeError, ValueError):
        return float("-inf")


#: Slack message subtypes that denote a removed message (R18-A4 / AT-596 T3).
_DELETED_SUBTYPES = {"message_deleted", "tombstone"}


def _is_deleted_message(msg: Dict[str, Any]) -> bool:
    """True when a Slack message represents a deletion (subtype/flag based).

    History polling does not reliably surface deletions, so this is only ever true
    for a delete/tombstone record produced out-of-band; the deep-content path routes
    it into R18-B2 freshness so the removed message's content leaves retrieval.
    """
    if msg.get("deleted") is True:
        return True
    return str(msg.get("subtype") or "") in _DELETED_SUBTYPES


def _ts_to_iso(ts: Any) -> Optional[str]:
    """Convert a Slack ``epoch.micro`` ts string to a UTC ISO-8601 string.

    Returns None for a missing/unparseable ts so callers can fall back to
    ``utc_now_iso()`` and keep the evidence-pointer spine populated.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


class SlackIngestor(ChangeBasedIngestor):
    """Change-based Slack ingestor (R16-A2 / AT-416).

    Encodes its position as a per-channel ``ts`` cursor map (opaque to the
    runner) and yields only messages newer than that cursor per channel. A first
    run (``since is None``) performs a full initial load of accessible public
    channels, streamed as resumable, individually-checkpointed batches.

    Deletes / tombstones (R16-A1 §5)
    --------------------------------
    ``reports_deletes = False``: this connector reads message history via
    ``conversations.history`` (timestamp-forward polling), which does not surface
    deletions of previously-seen messages — a deleted message simply stops
    appearing in future pages. Slack *does* deliver ``message_deleted`` through
    the Events API, but consuming the live event stream is out of scope for the
    reach phase. The gap is declared explicitly here rather than silently
    pretending deletes are caught; deletion handling can be layered on later.
    """

    connector_id = "slack"
    reports_deletes = False
    # Deep content is indexed per THREAD; freshness is driven at the thread level by
    # the deep-content path (R18-A4 / AT-596 T3), so the runner must NOT also fire
    # per-message freshness for Slack.
    manages_retrieval_freshness = True

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed Slack messages since ``since``.

        First run (``since is None``): full load of every accessible public
        channel, streamed as checkpointed batches (resumable — AC3). Incremental
        run: only messages newer than the stored per-channel cursor (AC2). An
        unchanged workspace yields a single empty :class:`DeltaBatch` whose
        ``next_checkpoint`` echoes the incoming position (AC2).
        """
        cursors: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        # Working copy we advance as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a
        # valid resume point on the next run.
        running = dict(cursors)

        channels = self._accessible_channels(org_id)
        logger.info(
            "slack: org=%s %s — %d accessible public channel(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(channels),
        )

        # Collect each channel's pending (changed) messages first so we know
        # which batch is the final one overall and can flag is_complete=True on
        # exactly that batch (the runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (channel, [messages]) for channels with changes
        for channel in channels:
            cursor = cursors.get(channel["id"])
            changed = self._messages_since(org_id, channel, cursor)
            if changed:
                pending.append((channel, changed))

        if not pending:
            # Unchanged workspace → empty delta that echoes the incoming
            # position (no regression). On a first run with no accessible
            # channels this records an empty cursor map.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        # Total number of batches across all channels, so the very last one is
        # marked terminal.
        total_batches = sum(
            (len(msgs) + self.batch_size - 1) // self.batch_size
            for _, msgs in pending
        )
        emitted = 0
        for channel, messages in pending:
            for start in range(0, len(messages), self.batch_size):
                page = messages[start : start + self.batch_size]
                records = [self._to_record(channel, m) for m in page]
                # Advance this channel's cursor to the newest ts in the page.
                running[channel["id"]] = page[-1]["ts"]
                emitted += 1
                yield DeltaBatch(
                    records=records,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )

    # ── Channel access (AC4 + R18-C0 P5 selection) ───────────────────────────
    def _public_member_channels(self, org_id: str) -> List[Dict[str, Any]]:
        """Public channels AgentIQ is a member of and that are live.

        Private channels (``is_private``), DMs, channels AgentIQ was never
        invited to (``is_member == False``), and archived channels are excluded.
        This is the privacy guarantee in AC4 enforced at the source of the read.
        These are the channels a customer may CHOOSE from — the per-org selection
        (below) narrows this further.
        """
        channels = self._raw_channels(org_id)
        return [
            c
            for c in channels
            if not c.get("is_private", False)
            and c.get("is_member", False)
            and not c.get("is_archived", False)
        ]

    def _selected_channel_ids(self, org_id: str) -> Optional[set]:
        """The org's saved Slack channel selection (R18-C0 P5), or None if unset.

        Stored on the Slack connector record by
        ``PATCH /api/connectors/slack/channels`` under the ``channels`` key.
        ``None`` means "no selection configured" → read every accessible channel
        (backwards-compatible default). A saved list (possibly empty) means read
        ONLY those channels. Any lookup failure degrades to ``None`` so a run is
        never blocked by the selection store being unavailable (e.g. offline
        tests with no DB).
        """
        try:
            from app.db import org_connector_get

            record = org_connector_get(org_id, "slack")
        except Exception:  # pragma: no cover - defensive: never block a run
            return None
        if not record:
            return None
        channels = record.get("channels")
        if not isinstance(channels, list):
            return None
        return {str(c) for c in channels}

    def _accessible_channels(self, org_id: str) -> List[Dict[str, Any]]:
        """Channels this ingestor actually reads: public+member+live, narrowed to
        the org's saved selection (R18-C0 P5 / AC5).

        With no selection configured, every accessible public channel is read
        (unchanged default). With a selection saved, only the selected channels
        are read — a channel the customer did not select is never ingested, even
        when the token has access to it.
        """
        channels = self._public_member_channels(org_id)
        selected = self._selected_channel_ids(org_id)
        if selected is None:
            return channels
        return [c for c in channels if str(c.get("id")) in selected]

    # ── Deep-content path (R18-A4 / AT-594 — T1) ─────────────────────────────
    # Thread assembly, rendering, provenance, and the substrate hand-off are the
    # SHARED conversation model (:mod:`discovery.ingest.conversation_content`),
    # reused verbatim by the Teams ingestor (AT-595). Only the collection edge
    # below — turning a Slack delta record into a neutral ``ConversationMessage``
    # and resolving the P5 scope — is Slack-specific.
    def _selected_scope_channel_ids(self, org_id: str) -> set:
        """Ids of channels the deep-content path may read conversation text from.

        The SAME boundary the reach path applies in :meth:`_accessible_channels`,
        expressed as an id set: a channel must be public + member + live (so
        private channels, DMs, non-member and archived channels are excluded — the
        AC4 access guarantee) AND, when a P5 selection is configured, be in that
        selection. With no selection configured, every accessible channel is in
        scope (backwards-compatible default). This is the AC2 boundary: content
        from unselected/private/DM channels is never assembled or handed off.
        """
        accessible = {str(c.get("id")) for c in self._public_member_channels(org_id)}
        selected = self._selected_channel_ids(org_id)
        if selected is None:
            return accessible
        return accessible & selected

    def _in_selected_scope(self, org_id: str, channel_id: Optional[str]) -> bool:
        """True when ``channel_id`` is in the deep-content read scope (AC2).

        The per-channel form of :meth:`_selected_scope_channel_ids`. The batch
        path computes the scope set once and filters against it; this predicate
        exists so the boundary can be asserted for a single channel too.
        """
        if not channel_id:
            return False
        return str(channel_id) in self._selected_scope_channel_ids(org_id)

    @staticmethod
    def _thread_key(msg: Dict[str, Any]) -> Optional[str]:
        """The Slack thread anchor for a message, or None when it has no thread.

        A reply carries ``thread_ts`` = the parent's ``ts``; a parent explicitly in
        a thread carries ``thread_ts`` = its own ``ts`` — both key to the parent
        ``ts``. A parent WITH replies but no ``thread_ts`` (``reply_count > 0``)
        keys to its own ``ts`` so it and any replies group together. Everything
        else is a standalone message (windowed, not threaded).
        """
        thread_ts = msg.get("thread_ts")
        if thread_ts:
            return str(thread_ts)
        try:
            reply_count = int(msg.get("reply_count", 0) or 0)
        except (TypeError, ValueError):
            reply_count = 0
        if reply_count > 0:
            return str(msg.get("ts", ""))
        return None

    def _to_conversation_message(self, record: Dict[str, Any]) -> ConversationMessage:
        """The Slack collection edge: one delta record → a neutral message.

        This is the ONLY Slack-specific part of the depth path — everything after
        (thread assembly, rendering, provenance, hand-off) is the shared model.
        """
        ts = str(record.get("ts", ""))
        channel_id = str(record.get("channel_id", ""))
        return ConversationMessage(
            container_id=channel_id,
            container_name=str(record.get("channel_name", "") or ""),
            msg_id=ts,
            thread_key=self._thread_key(record),
            sort_key=_ts_float(ts),
            iso_ts=_ts_to_iso(ts),
            author=str(record.get("user") or ""),
            text=str(record.get("text") or ""),
            extra={"channel_id": channel_id},
        )

    def assemble_threads(self, records: List[Dict[str, Any]]) -> List[ConversationThread]:
        """Assemble Slack delta records into conversational units (threads/windows).

        Adapts each record to a neutral :class:`ConversationMessage` then delegates
        to the shared :func:`conversation_content.assemble_threads` so Slack and
        Teams share identical thread semantics.
        """
        messages = [self._to_conversation_message(r) for r in records]
        return assemble_threads(
            RETRIEVAL_SOURCE_SYSTEM, messages, window_seconds=THREAD_WINDOW_SECONDS
        )

    def _thread_to_artifact(self, thread: ConversationThread) -> Any:
        """Map one assembled thread to a substrate ``ContentArtifact`` (shared)."""
        return thread_to_artifact(thread)

    def _read_container_messages(
        self, org_id: str, channel_id: str
    ) -> List[ConversationMessage]:
        """Read a channel's CURRENT messages as neutral messages (T3 resolver input).

        The re-extraction the freshness refresh worker needs: read the channel's live
        messages (offline fixture or live Web API) and adapt each to the neutral
        :class:`ConversationMessage` shape, so :func:`resolve_conversation_thread` can
        re-assemble and re-render the WHOLE thread through the same shared path used at
        ingest. A deleted message is simply absent from this read, so a thread re-read
        after a deletion naturally excludes it.
        """
        channel = {"id": channel_id, "name": self._channel_name(org_id, channel_id)}
        messages: List[ConversationMessage] = []
        for msg in self._raw_messages(org_id, channel):
            if _is_deleted_message(msg):
                continue
            record = {
                "channel_id": channel_id,
                "channel_name": channel["name"],
                "ts": msg.get("ts", ""),
                "thread_ts": msg.get("thread_ts"),
                "reply_count": msg.get("reply_count", 0),
                "user": msg.get("user"),
                "text": msg.get("text", ""),
            }
            messages.append(self._to_conversation_message(record))
        return messages

    def _channel_name(self, org_id: str, channel_id: str) -> str:
        """Best-effort human channel name for provenance (never blocks a refresh)."""
        try:
            for c in self._raw_channels(org_id):
                if str(c.get("id")) == str(channel_id):
                    return str(c.get("name", "") or "")
        except Exception:  # noqa: BLE001 — a name lookup must never fail a refresh
            return ""
        return ""

    def ingest_deep_content(
        self,
        org_id: str,
        records: List[Dict[str, Any]],
        *,
        ingest_fn: Optional[Callable[[str, List[Any]], Any]] = None,
        freshness_fn: Optional[Callable[[dict], Any]] = None,
    ) -> SlackDeepContentResult:
        """Hand a batch's conversation content to the substrate + freshness (T1/T3).

        The depth path that rides beside the unchanged reach signal path: it adapts
        the batch's records to neutral messages (carrying each record's
        ``change_kind``), scope-checks them against the P5 channel selection (AC2),
        and routes them via the shared conversation model:

          * newly-created threads present in the batch are handed directly to
            ``retrieval.ingest_content`` as ``conversation`` artifacts (AC1); and
          * edited messages / deletions / replies to pre-existing threads are wired
            into R18-B2 freshness at the THREAD level — an edit re-chunks the whole
            thread, a deletion removes its content (R18-A4 / AT-596, T3, AC3).

        The substrate owns chunking/embedding/indexing and the async refresh — this
        method never writes vectors. Called per fully-processed delta batch, so it is
        naturally incremental and rides the existing ``(org, 'slack')`` checkpoint —
        no new checkpointing (AC4). ``ingest_fn`` / ``freshness_fn`` are injectable
        for tests (defaulting to ``ingest_content`` / ``on_artifact_changed``). Raises
        :class:`SlackDeepContentError` when the create hand-off reports a failed
        artifact (at-least-once; idempotent replace by artifact id).
        """
        changes = [
            ConversationChange(
                self._to_conversation_message(r),
                normalise_change_kind(r.get("change_kind")),
            )
            for r in records
        ]
        return ingest_conversation_changes(
            org_id,
            RETRIEVAL_SOURCE_SYSTEM,
            changes,
            scope_container_ids=self._selected_scope_channel_ids(org_id),
            window_seconds=THREAD_WINDOW_SECONDS,
            ingest_fn=ingest_fn,
            freshness_fn=freshness_fn,
        )

    def _messages_since(
        self, org_id: str, channel: Dict[str, Any], cursor: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Return this channel's messages newer than ``cursor``, oldest-first.

        ``cursor is None`` (channel absent from the checkpoint map) means read
        from the beginning — the first-load / resume-a-new-channel case. Sorting
        oldest-first guarantees the checkpoint advances monotonically as batches
        are emitted.
        """
        messages = self._raw_messages(org_id, channel)
        fresh = [m for m in messages if _ts_gt(m.get("ts", ""), cursor)]
        fresh.sort(key=lambda m: float(m.get("ts", "0") or "0"))
        return fresh

    def _to_record(self, channel: Dict[str, Any], msg: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Slack message into a change-delta record.

        Carries structured message *signal* only (AC8): identity, channel,
        author, timestamps, and the engagement counts that feed escalation
        detection. The raw ``text`` is passed through for cross-reference marker
        scanning (ticket/PR mentions, Section 2) — no NLP / meaning extraction is
        done here. ``artifact_id`` + ``change_kind`` let the shared runner emit
        ``ingestion.artifact_changed`` events (AC7).

        R16-A2 / AT-417 (T2): each record also carries an extracted ``signals``
        block — the per-message cross-reference markers and escalation signal —
        so the reach-phase signal travels with the delta to downstream
        corroboration. Channel-level activity (volume/cadence/bursts) is derived
        across records by :func:`slack_signals.build_slack_signal`.

        R16-A2 / AT-418 (T3): every record also carries a fully-populated
        ``evidence_pointer`` (R16-B1) with ``source_system='slack'``, the
        message id, the message timestamp, and ``origin='observed'`` — so no
        Slack signal enters the system without a verifiable, auditable source
        reference (AC5).
        """
        ts = msg.get("ts", "")
        # Deletion first: Slack marks a removed message with a ``message_deleted`` /
        # ``tombstone`` subtype (or a truthy ``deleted``). History polling does not
        # reliably surface these (``reports_deletes = False``), but when one IS
        # produced (e.g. a future Events-API tombstone) it must propagate as a delete
        # so the thread's content leaves retrieval (R18-A4 / AT-596, T3). An edited
        # message is an update to an artifact we may already have seen; everything
        # else newly appearing is a creation. (Pure metadata — no content inspection.)
        if _is_deleted_message(msg):
            change_kind = ChangeKind.DELETED
        elif msg.get("edited"):
            change_kind = ChangeKind.UPDATED
        else:
            change_kind = ChangeKind.CREATED
        text = msg.get("text", "")
        return {
            "artifact_id": f"{channel['id']}:{ts}",
            "change_kind": change_kind,
            "source_system": "slack",
            "channel_id": channel["id"],
            "channel_name": channel.get("name", ""),
            "ts": ts,
            "thread_ts": msg.get("thread_ts"),
            "user": msg.get("user"),
            "reply_count": msg.get("reply_count", 0),
            "reply_users_count": msg.get("reply_users_count", 0),
            "reactions": msg.get("reactions", []),
            "text": text,
            "signals": {
                "cross_references": extract_cross_reference_markers(text),
                "escalation": extract_escalation_signal(msg),
            },
            # R16-B1 provenance (AT-418 / AC5): observed pointer back to this
            # exact Slack message.
            "evidence_pointer": build_evidence_pointer(channel["id"], ts),
        }

    # ── Source access: offline fixture vs live Slack Web API ─────────────────
    def _raw_channels(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("channels", []))
        return self._client(org_id).conversations_list()

    def _raw_messages(
        self, org_id: str, channel: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("messages", {}).get(channel["id"], []))
        return self._client(org_id).conversations_history(channel["id"])

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise SlackIngestError(f"Slack fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "SlackClient":
        """Build a Slack Web API client from the per-run OAuth credentials.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token, isolated per org/run) first, then the per-org
        vault via the single credential path — never a process-global env
        credential (R17-D3 Addendum A, AC8/AC11).
        """
        cred = get_live_connector("slack") or resolve_vault_connector("slack", org_id)
        token = cred.get("token") if cred else None
        if not token:
            raise SlackIngestError(
                "Live mode requires a Slack OAuth token from the credential vault. "
                "Connect Slack in the Integration Hub, or set INGEST_MODE=offline "
                "to run without credentials."
            )
        return SlackClient(token.strip())


def resolve_thread_content(org_id: str, source_artifact: str) -> Any:
    """Re-extract one Slack thread's CURRENT content for the refresh worker (T3).

    The content-resolver the R18-B2 refresh worker calls for a stale/queued Slack
    thread (``source_artifact = "{channel}:{thread_key}"``): re-read the channel's
    live messages and re-assemble the WHOLE thread via the shared conversation model.
    Returns the thread's ``ContentArtifact`` (empty content — chunks removed — when
    the thread no longer exists), or ``None`` when the channel cannot be read right
    now (the artifact stays queued for retry). Registered under ``'slack'`` by
    :func:`app.retrieval.default_resolvers.register_default_content_resolvers`.
    """
    ingestor = SlackIngestor()
    return resolve_conversation_thread(
        org_id,
        source_artifact,
        RETRIEVAL_SOURCE_SYSTEM,
        ingestor._read_container_messages,
        window_seconds=THREAD_WINDOW_SECONDS,
    )


def list_selectable_channels(org_id: str) -> List[Dict[str, str]]:
    """Public channels AgentIQ can read for ``org_id`` (member, not archived).

    The set of channels a customer chooses from in the Integration Hub (R18-C0
    P5). Selection filtering is deliberately NOT applied here — this is the full
    option list. Returned as lightweight ``{id, name}`` dicts. Resolves offline
    (fixture) or live (Slack Web API) exactly like a discovery run would see.
    """
    ingestor = SlackIngestor()
    return [
        {"id": str(c.get("id", "")), "name": str(c.get("name", ""))}
        for c in ingestor._public_member_channels(org_id)
    ]


class SlackClient:
    """Thin wrapper around the Slack Web API for public-channel signal reads.

    Only the two read endpoints the reach phase needs are implemented:
    ``conversations.list`` (public channels the app is a member of) and
    ``conversations.history`` (messages in a channel). Both follow Slack's
    cursor-based pagination. Private channels and DMs are never requested:
    ``conversations.list`` asks for ``types=public_channel`` only.
    """

    def __init__(self, token: str):
        self.token = token
        self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise SlackIngestError(
                "requests library required for live Slack mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self.token}"})
        return self._session

    def _get(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._sess().get(
            f"{_SLACK_API_BASE}/{method}", params=params, timeout=_REQUEST_TIMEOUT
        )
        if not resp.ok:
            raise SlackIngestError(f"Slack {method} HTTP {resp.status_code}")
        data = resp.json()
        if not data.get("ok", False):
            raise SlackIngestError(f"Slack {method} error: {data.get('error')}")
        return data

    def conversations_list(self) -> List[Dict[str, Any]]:
        """Return public channels the app can see (member filtering applied later)."""
        channels: List[Dict[str, Any]] = []
        cursor = ""
        while True:
            params = {
                "types": "public_channel",  # never private_channel / im / mpim
                "exclude_archived": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self._get("conversations.list", params)
            channels.extend(data.get("channels", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return channels

    def conversations_history(self, channel_id: str) -> List[Dict[str, Any]]:
        """Return all currently-visible messages for a channel (oldest-first)."""
        messages: List[Dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"channel": channel_id, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("conversations.history", params)
            messages.extend(data.get("messages", []))
            if not data.get("has_more"):
                break
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return messages
