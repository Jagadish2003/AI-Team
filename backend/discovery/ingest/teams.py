"""
R17-A1 / AT-430 (T1) — Microsoft Teams change-based ingestor.

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for Microsoft Teams. The single most important rule it honours: Teams is
NOT re-read in full on every discovery run. Teams' native change signal is the
Microsoft Graph **delta query** for channel messages
(``/teams/{id}/channels/{id}/messages/delta``), which returns only the messages
that changed since the last call and hands back an opaque *delta token* marking
the new position. The connector encodes that delta token as the opaque checkpoint
value (R16-A1 §1) and, on an incremental run, asks each channel only for what
changed since its stored token.

Scope (AT-430 / T1, AT-431 / T2, AT-432 / T3)
---------------------------------------------
This file is the change-based ingestor: checkpointed incremental message
ingestion via Graph delta queries plus a resumable, checkpointed first load
(AT-430 / T1, AC2 + AC3). Each delta record also carries an extracted ``signals``
block — the per-message cross-reference markers + escalation signal — via
:mod:`discovery.ingest.teams_signals` (AT-431 / T2, AC8), plus a fully-populated
``evidence_pointer`` (R16-B1, ``origin='observed'``) attached to every record so
each Teams signal is traceable back to its source message (AT-432 / T3, AC5). The
remaining downstream pieces are deliberately SEPARATE stories and are NOT done
here:

  * The Teams MEDIUM corroboration ceiling — T4.
  * Microsoft Graph OAuth connect wiring (auth-url / callback / vault) — T5 /
    AT-434. The Teams catalog tile already exists; this ingestor reads whatever
    OAuth token that flow lands in the per-run credential context.
  * ``ingestion.artifact_changed`` event emission — handled by the shared runner
    (``change_runner.py``, AT-381); every record this ingestor yields already
    carries ``artifact_id`` + ``change_kind`` so the runner can emit them.

Per the reach/depth boundary (AC8), this ingestor reads only structured message
*signal* — it carries message metadata (id, author, timestamps, reply/mention
counts, the raw body text for cross-reference marker scanning) through to the
records and extracts only counts/timing/pattern-matched markers. It does NOT do
deep conversation-content NLP; that is the separate 1.8 deep-content story.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'teams')`` checkpoint row is persisted by the runner, but a
Teams workspace has many channels (across teams) each with its own Graph delta
position. The connector therefore encodes a per-channel delta-token MAP as the
opaque checkpoint value, keyed by ``"{team_id}/{channel_id}"``. Each token is the
high-water change marker (the last-modified/created timestamp) of the newest
message seen in that channel — opaque to the runner, owned by this connector::

    {"v": 1, "channels": {"T-eng/19:ops": "2026-06-11T08:05:00Z",
                          "T-eng/19:deploys": "2026-06-10T09:30:00Z"}}

The runner never interprets this — it persists and returns the string verbatim
(R16-A1 AC5). Only this connector, which owns the shape, parses it back. A
channel absent from the map is read from the beginning (no delta token → full
enumeration), which is exactly what makes a first load resumable: if the streamed
first load fails partway, the next run finds a checkpoint (incremental mode)
whose map covers the channels already loaded, resumes the partially-loaded
channel from its last delta token, and loads any not-yet-started channel in full.
No records are skipped and the load completes across runs.

Permissions / privacy (AC4)
---------------------------
Only standard channels AgentIQ has been explicitly granted are read. Private
channels are excluded, and private chats / direct messages are NEVER accessed:
the connector only ever enumerates ``/teams/{id}/channels`` and never touches the
``/chats`` surface where 1:1 and group DMs live. :meth:`_accessible_channels`
filters to ``membership_type == 'standard' and is_accessible == True and not
is_archived``.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/teams_sample.json`` — parity with the Salesforce/ServiceNow/Jira/
Slack connectors. Live: calls the Microsoft Graph API (``/teams``,
``/teams/{id}/channels``, ``/teams/{id}/channels/{id}/messages/delta``) using the
OAuth token from the per-run credential context. Credentials are resolved exactly
like the other connectors — ``get_live_connector('teams')`` first, then the env
fallback for CLI use.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .teams_signals import (
    build_evidence_pointer,
    extract_cross_reference_markers,
    extract_escalation_signal,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "teams_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of messages emitted per :class:`DeltaBatch`. Kept modest so a
#: large initial load is streamed as many small, individually-checkpointed
#: batches (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Microsoft Graph API base (live mode).
_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
_REQUEST_TIMEOUT = 30

#: Channel membership types Microsoft Graph reports. Only ``standard`` channels
#: are read; ``private`` and ``shared`` channels are excluded at the access
#: boundary (AC4). Private chats / DMs are not channels at all and are never
#: enumerated.
_STANDARD_MEMBERSHIP = "standard"


class TeamsIngestError(Exception):
    """Raised when live Teams ingestion fails with a clear, actionable message."""


def _channel_key(team_id: str, channel_id: str) -> str:
    """Build the per-channel checkpoint-map key ``"{team_id}/{channel_id}"``.

    A channel id is only unique within its team, so the delta-token map is keyed
    by the team/channel pair to keep two teams' channels from colliding.
    """
    return f"{team_id}/{channel_id}"


def _encode_checkpoint(tokens: Dict[str, str]) -> str:
    """Encode the per-channel delta-token map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "channels": tokens},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-channel delta-token map.

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
            "teams: could not decode checkpoint value; treating as first run "
            "(full re-read). value=%r",
            value,
        )
        return {}
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, dict):
        return {}
    # Keep only string→string entries; ignore anything malformed.
    return {str(k): str(v) for k, v in channels.items() if v is not None}


def _change_marker(msg: Dict[str, Any]) -> str:
    """Return a message's change position: its last-modified (else created) time.

    Microsoft Graph stamps every channel message with ``createdDateTime`` and,
    once edited, ``lastModifiedDateTime`` — both present on live Graph messages
    AND in the offline fixture. This timestamp is the per-message change signal
    the connector advances its opaque delta token to, exactly as the Slack
    connector advances per-channel by message ``ts``. An edit moves the marker
    forward (newer ``lastModifiedDateTime``), so a re-modified message re-surfaces
    in the next delta.
    """
    return msg.get("lastModifiedDateTime") or msg.get("createdDateTime") or ""


def _marker_epoch(marker: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 change marker (Graph uses ``...Z``) to epoch seconds.

    Returns None for a missing/unparseable marker so callers can fall back to a
    string compare. The opaque checkpoint stores the ISO string verbatim; only
    comparison goes through epoch, so ordering is correct regardless of offset
    format.
    """
    if not marker:
        return None
    try:
        return datetime.fromisoformat(marker.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _marker_gt(marker: str, token: Optional[str]) -> bool:
    """True when a message's change marker is strictly newer than the delta token.

    The delta token is opaque to the runner but owned by this connector: it is the
    ISO-8601 high-water change marker of the last message seen in a channel.
    ``token`` falsy (channel absent from the checkpoint map) means read from the
    beginning — the first-load / resume-a-new-channel case. Comparison is by parsed
    timestamp, falling back to a lexicographic compare if either side is
    unparseable.
    """
    if not token:
        return True
    me, te = _marker_epoch(marker), _marker_epoch(token)
    if me is None or te is None:
        return str(marker) > str(token)
    return me > te


class TeamsIngestor(ChangeBasedIngestor):
    """Change-based Microsoft Teams ingestor (R17-A1 / AT-430).

    Encodes its position as a per-channel Graph delta-token map (opaque to the
    runner) and yields only messages newer than that token per channel. A first
    run (``since is None``) performs a full initial load of accessible standard
    channels, streamed as resumable, individually-checkpointed batches.

    Deletes / tombstones (R16-A1 §5)
    --------------------------------
    ``reports_deletes = False``: Microsoft Graph delta DOES surface removed
    messages (as ``@removed`` annotations), but consuming and propagating that
    removal stream is out of scope for the reach phase (this subtask ingests
    structured message signal only). The gap is declared explicitly here rather
    than silently pretending deletes are caught; deletion/tombstone handling can
    be layered on later by reading the ``@removed`` annotation and yielding a
    :func:`~discovery.ingest.base.tombstone` record.
    """

    connector_id = "teams"
    reports_deletes = False

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed Teams messages since ``since``.

        First run (``since is None``): full load of every accessible standard
        channel, streamed as checkpointed batches (resumable — AC3). Incremental
        run: a Graph delta query per channel returns only messages newer than the
        stored delta token (AC2). An unchanged workspace yields a single empty
        :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming position
        (AC2).
        """
        tokens: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        # Working copy we advance as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a
        # valid resume point on the next run.
        running = dict(tokens)

        channels = self._accessible_channels(org_id)
        logger.info(
            "teams: org=%s %s — %d accessible standard channel(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(channels),
        )

        # Run each channel's delta query first so we know which batch is the final
        # one overall and can flag is_complete=True on exactly that batch (the
        # runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (channel, [messages]) for channels with changes
        for channel in channels:
            key = _channel_key(channel["team_id"], channel["id"])
            token = tokens.get(key)
            changed = self._messages_delta(org_id, channel, token)
            if changed:
                pending.append((channel, changed))

        if not pending:
            # Unchanged workspace → empty delta that echoes the incoming
            # position (no regression). On a first run with no accessible
            # channels this records an empty delta-token map.
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
            key = _channel_key(channel["team_id"], channel["id"])
            for start in range(0, len(messages), self.batch_size):
                page = messages[start : start + self.batch_size]
                records = [self._to_record(channel, m) for m in page]
                # Advance this channel's delta token to the newest change marker in
                # the page — the high-water last-modified/created timestamp. This is
                # the opaque position the next run resumes the delta query from.
                running[key] = _change_marker(page[-1])
                emitted += 1
                yield DeltaBatch(
                    records=records,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )

    # ── Channel access (AC4) ─────────────────────────────────────────────────
    def _accessible_channels(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only standard channels AgentIQ is granted and that are live.

        Private channels (``membership_type != 'standard'``), channels AgentIQ
        was never granted (``is_accessible == False``), and archived channels are
        excluded. Private chats and DMs are never enumerated at all — the
        connector only ever reads ``/teams/{id}/channels``. This is the privacy
        guarantee in AC4 enforced at the source of the read.

        Each returned channel dict carries its owning ``team_id`` / ``team_name``
        so records and checkpoint keys can be team-scoped.
        """
        accessible: List[Dict[str, Any]] = []
        for team in self._raw_teams(org_id):
            team_id = team.get("id", "")
            team_name = team.get("displayName", team.get("name", ""))
            for c in self._raw_channels(org_id, team_id):
                if c.get("membership_type", _STANDARD_MEMBERSHIP) != _STANDARD_MEMBERSHIP:
                    continue  # private / shared channel — excluded
                if not c.get("is_accessible", False):
                    continue  # AgentIQ was not granted this channel
                if c.get("is_archived", False):
                    continue  # archived channel — excluded
                accessible.append({**c, "team_id": team_id, "team_name": team_name})
        return accessible

    def _messages_delta(
        self, org_id: str, channel: Dict[str, Any], token: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Return this channel's messages changed since ``token``, oldest-first.

        This is the Graph delta query: ``token`` falsy (channel absent from the
        checkpoint map) means a full enumeration from the beginning — the
        first-load / resume-a-new-channel case; otherwise only messages whose
        change marker is strictly newer than the stored delta token are returned.
        Sorting oldest-first (by change marker) guarantees the checkpoint advances
        monotonically as batches are emitted.
        """
        messages = self._raw_messages(org_id, channel)
        fresh = [m for m in messages if _marker_gt(_change_marker(m), token)]
        fresh.sort(key=lambda m: _marker_epoch(_change_marker(m)) or float("-inf"))
        return fresh

    def _to_record(self, channel: Dict[str, Any], msg: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Teams message into a change-delta record.

        Carries structured message *signal* only (AC8): identity, team, channel,
        author, timestamps, and the engagement counts that feed escalation
        detection. The raw ``text`` is passed through for cross-reference marker
        scanning (ticket/PR mentions, Section 2) — no NLP / meaning extraction is
        done here. ``artifact_id`` + ``change_kind`` let the shared runner emit
        ``ingestion.artifact_changed`` events (AC7).

        R17-A1 / AT-431 (T2): each record also carries an extracted ``signals``
        block — the per-message cross-reference markers and escalation signal — so
        the reach-phase signal travels with the delta to downstream corroboration.
        Channel-level activity (volume/cadence/bursts) is derived across records by
        :func:`teams_signals.build_teams_signal`.

        R17-A1 / AT-432 (T3): every record also carries a fully-populated
        ``evidence_pointer`` (R16-B1) with ``source_system='teams'``, the message
        id, the message timestamp, and ``origin='observed'`` — so no Teams signal
        enters the system without a verifiable, auditable source reference (AC5).
        """
        team_id = channel["team_id"]
        channel_id = channel["id"]
        message_id = msg.get("id", "")
        # A message whose last edit differs from its creation is an update to an
        # artifact we may already have seen; everything else newly appearing is a
        # creation. (Pure metadata — no content inspection.)
        last_modified = msg.get("lastModifiedDateTime")
        created = msg.get("createdDateTime")
        change_kind = (
            ChangeKind.UPDATED
            if last_modified and created and last_modified != created
            else ChangeKind.CREATED
        )
        body = msg.get("body") or {}
        text = body.get("content", "") if isinstance(body, dict) else ""
        sender = (msg.get("from") or {}).get("user") or {}
        reply_count = msg.get("reply_count", 0)
        reply_users_count = msg.get("reply_users_count", 0)
        mentions = msg.get("mentions", [])
        reactions = msg.get("reactions", [])
        return {
            "artifact_id": f"{team_id}/{channel_id}:{message_id}",
            "change_kind": change_kind,
            "source_system": "teams",
            "team_id": team_id,
            "team_name": channel.get("team_name", ""),
            "channel_id": channel_id,
            "channel_name": channel.get("displayName", ""),
            "message_id": message_id,
            "created_at": created,
            "last_modified_at": last_modified,
            "user": sender.get("id"),
            "user_display_name": sender.get("displayName"),
            "reply_count": reply_count,
            "reply_users_count": reply_users_count,
            "mentions": mentions,
            "reactions": reactions,
            "text": text,
            # R17-A1 / AT-431 (T2): reach-phase signal travels with the delta.
            "signals": {
                "cross_references": extract_cross_reference_markers(text),
                "escalation": extract_escalation_signal(
                    {
                        "reply_count": reply_count,
                        "reply_users_count": reply_users_count,
                        "mentions": mentions,
                        "reactions": reactions,
                    }
                ),
            },
            # R17-A1 / AT-432 (T3, AC5): observed provenance pointer back to this
            # exact Teams message.
            "evidence_pointer": build_evidence_pointer(
                team_id, channel_id, message_id, created
            ),
        }

    # ── Source access: offline fixture vs live Microsoft Graph API ───────────
    def _raw_teams(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("teams", []))
        return self._client(org_id).list_teams()

    def _raw_channels(self, org_id: str, team_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("channels", {}).get(team_id, []))
        return self._client(org_id).list_channels(team_id)

    def _raw_messages(
        self, org_id: str, channel: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if not is_live():
            key = _channel_key(channel["team_id"], channel["id"])
            return list(self._fixture().get("messages", {}).get(key, []))
        return self._client(org_id).messages_delta(channel["team_id"], channel["id"])

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise TeamsIngestError(f"Teams fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "TeamsGraphClient":
        """Build a Microsoft Graph client from the per-run OAuth credentials.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token, isolated per org/run) first, then the
        ``TEAMS_GRAPH_TOKEN`` env var as a CLI/standalone fallback. The OAuth
        connect flow that lands the token in the vault is T5 / AT-434.
        """
        cred = get_live_connector("teams")
        token = cred.get("token") if cred else os.getenv("TEAMS_GRAPH_TOKEN")
        if not token:
            raise TeamsIngestError(
                "Live mode requires a Microsoft Graph OAuth token, provided by the "
                "Teams Connect flow (credential vault). Set INGEST_MODE=offline to "
                "run without credentials."
            )
        return TeamsGraphClient(token.strip())


class TeamsGraphClient:
    """Thin wrapper around the Microsoft Graph API for channel-message signal reads.

    Only the three read endpoints the reach phase needs are implemented:
    ``/teams`` (joined teams), ``/teams/{id}/channels`` (standard channels), and
    ``/teams/{id}/channels/{id}/messages/delta`` (changed messages via the native
    delta query). Private chats and DMs (the ``/chats`` surface) are never
    requested — the client has no method that touches them (AC4).
    """

    def __init__(self, token: str):
        self.token = token
        self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise TeamsIngestError(
                "requests library required for live Teams mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self.token}"})
        return self._session

    def _get(self, url: str) -> Dict[str, Any]:
        resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise TeamsIngestError(
                f"Microsoft Graph GET {url} HTTP {resp.status_code}"
            )
        return resp.json()

    def _get_all(self, url: str) -> List[Dict[str, Any]]:
        """Follow Graph ``@odata.nextLink`` pagination, collecting ``value`` rows."""
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        while next_url:
            data = self._get(next_url)
            items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
        return items

    def list_teams(self) -> List[Dict[str, Any]]:
        """Return the teams AgentIQ has joined (``/me/joinedTeams``)."""
        return self._get_all(f"{_GRAPH_API_BASE}/me/joinedTeams")

    def list_channels(self, team_id: str) -> List[Dict[str, Any]]:
        """Return a team's channels, normalised to the fixture's field shape.

        Graph reports ``membershipType`` (standard / private / shared); the
        ingestor's access filter reads ``membership_type``, so it is mapped here.
        """
        raw = self._get_all(f"{_GRAPH_API_BASE}/teams/{team_id}/channels")
        channels: List[Dict[str, Any]] = []
        for c in raw:
            channels.append(
                {
                    **c,
                    "membership_type": c.get("membershipType", _STANDARD_MEMBERSHIP),
                    # Graph only returns channels the caller can see, so a returned
                    # channel is one AgentIQ is granted.
                    "is_accessible": True,
                    "is_archived": c.get("isArchived", False),
                }
            )
        return channels

    def messages_delta(self, team_id: str, channel_id: str) -> List[Dict[str, Any]]:
        """Return changed messages for a channel via the Graph delta query.

        Follows ``@odata.nextLink`` pagination to the final ``@odata.deltaLink``,
        collecting all changed messages. (The delta token itself is threaded
        through the opaque checkpoint by the ingestor; this client returns the
        message rows.)
        """
        url = (
            f"{_GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages/delta"
        )
        return self._get_all(url)
