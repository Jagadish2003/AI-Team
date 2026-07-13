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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

from . import get_live_connector, is_live, resolve_vault_connector
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
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

#: Slack conversation text chunks under the substrate's *conversation* policy
#: (thread-aware splitting with overlap) — R18-A4 §1.
CONTENT_TYPE = "conversation"

#: Messages that carry no thread structure are grouped into a time-bounded window
#: so the conversational unit is never a lone, context-free message (R18-A4 §1 /
#: §4 "Threads are the unit of meaning"). Consecutive standalone messages within
#: this many seconds of the window's first message form one window "thread".
THREAD_WINDOW_SECONDS = 3600

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


def _ts_to_iso(ts: Any) -> Optional[str]:
    """Convert a Slack ``epoch.micro`` ts string to a UTC ISO-8601 string.

    Returns None for a missing/unparseable ts so callers can fall back to
    ``utc_now_iso()`` and keep the evidence-pointer spine populated.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


class SlackDeepContentError(RuntimeError):
    """Raised by the deep-content hand-off when the substrate reports failures.

    Propagated into the change runner so the checkpoint is NOT advanced past
    conversation content that never reached retrieval — the batch is re-read and
    re-handed on the next run (idempotent via ``ingest_content``'s per-artifact
    replace by ``(source_system, source_artifact)``).
    """


@dataclass
class SlackDeepContentResult:
    """Accounting for one :meth:`SlackIngestor.ingest_deep_content` call.

    Records how many in-scope threads were assembled and the substrate's
    per-artifact totals, so a caller sees both how much conversation was read and
    how much was indexed.
    """

    org_id: str
    threads: int = 0
    windows: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0


@dataclass
class _SlackThread:
    """One assembled conversational unit ready for the retrieval substrate.

    A thread is either an explicit Slack thread (a parent message plus its
    replies, grouped by ``thread_ts``) or a time-bounded window of standalone
    channel messages where no thread structure exists (R18-A4 §1). ``messages``
    are the delta records in oldest-first order; ``key`` is the thread anchor
    (parent ``ts`` for an explicit thread, the window's first ``ts`` otherwise).
    """

    channel_id: str
    channel_name: str
    key: str
    messages: List[Dict[str, Any]]
    is_window: bool = False

    @property
    def root_ts(self) -> str:
        return str(self.messages[0].get("ts", "")) if self.messages else ""

    @property
    def latest_ts(self) -> str:
        return str(self.messages[-1].get("ts", "")) if self.messages else ""

    @property
    def unit(self) -> str:
        return "window" if self.is_window else "thread"

    @property
    def participants(self) -> List[str]:
        seen: List[str] = []
        for m in self.messages:
            user = m.get("user")
            if user and user not in seen:
                seen.append(str(user))
        return seen

    def source_artifact(self, channel_id: Optional[str] = None) -> str:
        """The substrate identity for this thread: ``"{channel_id}:{key}"``.

        Stable and thread-level (AC1): every chunk the substrate derives for this
        thread shares this id, so a retrieval hit points at the exact thread. The
        anchor ``key`` is a message ``ts`` (parent or window-first), so the id
        format is the uniform ``channel:ts`` used across the Slack connector.
        """
        return f"{self.channel_id}:{self.key}"


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

    def assemble_threads(self, records: List[Dict[str, Any]]) -> List[_SlackThread]:
        """Assemble delta records into conversational units — threads or windows.

        Threads are the unit of meaning (R18-A4 §4): messages are grouped per
        channel by their thread anchor (``thread_ts`` when present, else the
        message's own ``ts`` when it is itself a thread parent). Messages that
        carry no thread structure at all are bucketed into time-bounded windows
        (:data:`THREAD_WINDOW_SECONDS`) so a lone message is never handed over as a
        context-free artifact. Deterministically ordered (channel id, then anchor).
        """
        by_channel: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            by_channel.setdefault(str(r.get("channel_id", "")), []).append(r)

        threads: List[_SlackThread] = []
        for channel_id in sorted(by_channel):
            threads.extend(self._assemble_channel(channel_id, by_channel[channel_id]))
        return threads

    def _assemble_channel(
        self, channel_id: str, msgs: List[Dict[str, Any]]
    ) -> List[_SlackThread]:
        """Assemble one channel's messages into explicit threads + time windows."""
        channel_name = ""
        for m in msgs:
            if m.get("channel_name"):
                channel_name = str(m["channel_name"])
                break

        explicit: Dict[str, List[Dict[str, Any]]] = {}
        loose: List[Dict[str, Any]] = []
        for m in msgs:
            key = self._thread_key(m)
            if key is None:
                loose.append(m)
            else:
                explicit.setdefault(key, []).append(m)

        threads: List[_SlackThread] = []
        for key in sorted(explicit, key=_ts_float):
            group = sorted(explicit[key], key=lambda x: _ts_float(x.get("ts")))
            threads.append(_SlackThread(channel_id, channel_name, key, group, is_window=False))
        threads.extend(self._window_loose(channel_id, channel_name, loose))
        return threads

    @staticmethod
    def _thread_key(msg: Dict[str, Any]) -> Optional[str]:
        """The thread anchor for a message, or None when it belongs to no thread.

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

    def _window_loose(
        self, channel_id: str, channel_name: str, loose: List[Dict[str, Any]]
    ) -> List[_SlackThread]:
        """Bucket standalone messages into time-bounded windows (R18-A4 §1).

        A new window starts once a message is more than
        :data:`THREAD_WINDOW_SECONDS` after the current window's first message, so
        a channel with no threads still yields readable, time-coherent units rather
        than one artifact per message.
        """
        if not loose:
            return []
        ordered = sorted(loose, key=lambda m: _ts_float(m.get("ts")))
        windows: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        window_start = 0.0
        for m in ordered:
            ts = _ts_float(m.get("ts"))
            if not current:
                current = [m]
                window_start = ts
            elif ts - window_start <= THREAD_WINDOW_SECONDS:
                current.append(m)
            else:
                windows.append(current)
                current = [m]
                window_start = ts
        if current:
            windows.append(current)
        return [
            _SlackThread(
                channel_id,
                channel_name,
                str(w[0].get("ts", "")),
                w,
                is_window=True,
            )
            for w in windows
        ]

    def _render_thread_text(self, thread: _SlackThread) -> str:
        """Render a thread as author-attributed text, oldest message first.

        Each message becomes an ``author: text`` line so a human reviewing a
        finding can read who said what (R18-A4 §1: "Message text with author").
        The author is the Slack user id (resolved to an entity elsewhere — T4);
        blank-bodied messages keep their attribution line so participation is
        visible.
        """
        lines: List[str] = []
        for m in thread.messages:
            author = str(m.get("user") or "unknown")
            text = str(m.get("text") or "").strip()
            lines.append(f"{author}: {text}" if text else f"{author}:")
        return "\n".join(lines)

    def _thread_evidence_pointer(self, thread: _SlackThread) -> Dict[str, Any]:
        """Build the R16-B1 OBSERVED, THREAD-LEVEL evidence pointer (AC1, AC5).

        A finding citing this conversation can point at the exact thread: the
        pointer's ``source_artifact`` is the thread identity ``"{channel}:{key}"``
        and ``origin='observed'`` (read directly from Slack). ``source_timestamp``
        anchors to the thread's first message, falling back to now only when the ts
        is unparseable so the mandatory spine is always populated.
        """
        return EvidencePointer.observed(
            source_system=RETRIEVAL_SOURCE_SYSTEM,
            source_artifact=thread.source_artifact(),
            source_timestamp=_ts_to_iso(thread.root_ts) or utc_now_iso(),
            source_artifact_type="record_id",
        ).to_dict()

    def _thread_to_artifact(self, thread: _SlackThread) -> Any:
        """Map one assembled thread to a substrate :class:`ContentArtifact`.

        Carries the rendered author-attributed text plus thread-level provenance
        (channel, thread/window position, participants, and the observed evidence
        pointer) so a retrieval hit shows the exact source thread (AC1/AC5).
        Imported lazily so this discovery module carries no import-time dependency
        on the app.retrieval package.
        """
        from app.retrieval.ingest import ContentArtifact

        return ContentArtifact(
            source_system=RETRIEVAL_SOURCE_SYSTEM,
            source_artifact=thread.source_artifact(),
            content=self._render_thread_text(thread),
            content_type=CONTENT_TYPE,
            # Recency of the last message drives freshness; the evidence pointer
            # anchors to the thread's origin.
            source_timestamp=_ts_to_iso(thread.latest_ts),
            provenance={
                "channel_id": thread.channel_id,
                "channel_name": thread.channel_name,
                "thread_ts": None if thread.is_window else thread.key,
                "unit": thread.unit,
                "message_count": len(thread.messages),
                "participants": thread.participants,
                "first_ts": thread.root_ts or None,
                "last_ts": thread.latest_ts or None,
                "origin": "observed",
                "evidence_pointer": self._thread_evidence_pointer(thread),
            },
        )

    def ingest_deep_content(
        self,
        org_id: str,
        records: List[Dict[str, Any]],
        *,
        ingest_fn: Optional[Callable[[str, List[Any]], Any]] = None,
    ) -> SlackDeepContentResult:
        """Hand a batch's conversation content to the retrieval substrate (T1).

        The depth path that rides beside the unchanged reach signal path: it
        scope-checks the batch's records against the P5 channel selection (AC2),
        assembles the in-scope messages into threads/windows, and hands each as a
        ``ContentArtifact`` to ``retrieval.ingest_content(org_id, artifacts)``
        (AC1). The substrate owns chunking/embedding/indexing — this method never
        writes vectors.

        Called per fully-processed delta batch (so it is naturally incremental and
        rides the existing ``(org, 'slack')`` checkpoint — no new checkpointing).
        ``ingest_fn`` is injectable for tests; it defaults to the real
        ``ingest_content``. Raises :class:`SlackDeepContentError` when the substrate
        reports any failed artifact, so the change runner leaves the checkpoint
        un-advanced and the batch is re-handed next run (at-least-once; idempotent
        replace by artifact id).
        """
        result = SlackDeepContentResult(org_id=org_id)
        if not records:
            return result

        scope = self._selected_scope_channel_ids(org_id)
        in_scope = [r for r in records if str(r.get("channel_id", "")) in scope]
        if not in_scope:
            return result

        threads = self.assemble_threads(in_scope)
        result.threads = len(threads)
        result.windows = sum(1 for t in threads if t.is_window)
        if not threads:
            return result

        artifacts = [self._thread_to_artifact(t) for t in threads]
        result.artifacts_handed_off = len(artifacts)

        fn = ingest_fn
        if fn is None:
            from app.retrieval.ingest import ingest_content as fn  # type: ignore

        ingest_result = fn(org_id, artifacts)
        result.artifacts_indexed = getattr(ingest_result, "artifacts_indexed", 0)
        result.artifacts_empty = getattr(ingest_result, "artifacts_empty", 0)
        result.artifacts_failed = getattr(ingest_result, "artifacts_failed", 0)
        result.chunks_indexed = getattr(ingest_result, "chunks_indexed", 0)

        logger.info(
            "slack deep content: org=%s threads=%d (windows=%d) handed_off=%d "
            "indexed=%d empty=%d failed=%d chunks_indexed=%d (embedding is async)",
            org_id,
            result.threads,
            result.windows,
            result.artifacts_handed_off,
            result.artifacts_indexed,
            result.artifacts_empty,
            result.artifacts_failed,
            result.chunks_indexed,
        )

        if result.artifacts_failed:
            # Do not let the checkpoint advance past content the substrate did not
            # accept — raise so the runner leaves the position for a re-hand next
            # run (idempotent replace by (source_system, source_artifact)).
            raise SlackDeepContentError(
                f"{result.artifacts_failed} Slack thread(s) failed retrieval "
                f"hand-off for org {org_id}; checkpoint not advanced (will retry)"
            )
        return result

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
        # An edited message is an update to an artifact we may already have seen;
        # everything else newly appearing is a creation. (Pure metadata — no
        # content inspection.)
        change_kind = ChangeKind.UPDATED if msg.get("edited") else ChangeKind.CREATED
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
