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

Per the reach/depth boundary (AC8), this ingestor reads only structured message
*signal* — it carries message metadata (ts, author, reply/reaction counts, the
raw text for later cross-reference marker scanning) through to the records. It
does NOT do deep conversation-content NLP; that is the separate 1.8 deep-content
story.

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
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live, resolve_vault_connector
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .slack_signals import (
    build_evidence_pointer,
    extract_cross_reference_markers,
    extract_escalation_signal,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "slack_sample.json"

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

    # ── Channel access (AC4) ─────────────────────────────────────────────────
    def _accessible_channels(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only public channels AgentIQ is a member of and that are live.

        Private channels (``is_private``), DMs, channels AgentIQ was never
        invited to (``is_member == False``), and archived channels are excluded.
        This is the privacy guarantee in AC4 enforced at the source of the read.
        """
        channels = self._raw_channels(org_id)
        return [
            c
            for c in channels
            if not c.get("is_private", False)
            and c.get("is_member", False)
            and not c.get("is_archived", False)
        ]

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
