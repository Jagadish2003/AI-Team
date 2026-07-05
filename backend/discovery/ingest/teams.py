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
position. The connector therefore encodes a per-channel position MAP as the
opaque checkpoint value, keyed by ``"{team_id}/{channel_id}"``. The per-channel
value is opaque to the runner and owned by this connector, and its exact form
depends on the mode:

  * **Live** — the value is Microsoft Graph's own ``@odata.deltaLink`` for that
    channel (returned on the final page of the delta response). On the next run
    it is replayed as the delta position so Graph returns ONLY what changed since
    — the connector never re-scans the channel or filters client-side. This is the
    native Graph delta token the R16-A1 opaque-checkpoint contract was built for.
  * **Offline (fixtures)** — the value is the ISO-8601 high-water
    last-modified/created timestamp of the newest message seen; the connector
    filters the fixture messages by this marker. Fixtures have no Graph deltaLink,
    so the marker stands in for it.

Both forms are just strings the runner persists verbatim (R16-A1 AC5)::

    live:    {"v": 1, "channels": {"T-eng/19:ops": "https://graph.microsoft.com/v1.0/teams/.../messages/delta?$deltatoken=..."}}
    offline: {"v": 1, "channels": {"T-eng/19:ops": "2026-06-11T08:05:00Z"}}

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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import get_live_connector, is_live, resolve_vault_connector
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

#: Microsoft Graph throttles delta queries heavily (HTTP 429 + Retry-After).
#: Retry a throttled GET this many times, honouring Retry-After (capped) between
#: attempts, before giving up with an actionable error (L2).
_MAX_THROTTLE_RETRIES = 3
_MAX_RETRY_WAIT_SECONDS = 30

#: Total wall-clock a single client will spend sleeping on 429 ``Retry-After``
#: waits across ALL requests in one ingest before giving up. The per-request
#: retry cap (``_MAX_THROTTLE_RETRIES`` × ``_MAX_RETRY_WAIT_SECONDS`` ≈ 90s) is
#: otherwise unbounded across a channel-by-channel enumeration, so a heavily
#: throttled tenant could stall a run for many minutes. Giving up here is
#: non-blocking — the caller degrades to an empty corroboration block (same path
#: as the existing "throttling persisted" error). Env-tunable; the default is
#: generous so a normal run (occasional short waits) never trips it.
try:
    _MAX_TOTAL_THROTTLE_WAIT_SECONDS = max(
        0, int(os.getenv("TEAMS_MAX_THROTTLE_WAIT_SECONDS", "120"))
    )
except (TypeError, ValueError):
    _MAX_TOTAL_THROTTLE_WAIT_SECONDS = 120

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


def _retry_after_seconds(resp: Any) -> int:
    """Parse a Microsoft Graph ``Retry-After`` header into a bounded sleep (L2).

    Returns the header's seconds value clamped to ``[1, _MAX_RETRY_WAIT_SECONDS]``,
    defaulting to 1 second when the header is missing or unparseable. (Graph sends
    Retry-After as an integer number of seconds for 429s.)
    """
    try:
        raw = resp.headers.get("Retry-After", "") or ""
    except Exception:  # pragma: no cover — defensive on odd header objects
        raw = ""
    try:
        secs = int(float(raw))
    except (TypeError, ValueError):
        secs = 1
    return max(1, min(secs, _MAX_RETRY_WAIT_SECONDS))


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
        # Live Microsoft Graph client, created once per run and reused across
        # list_teams / list_channels / messages_delta (avoids the N+1 session
        # churn, M2) then closed at the end of ingest_changes (no leak, H2).
        self._graph_client: Optional["TeamsGraphClient"] = None

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed Teams messages since ``since``.

        First run (``since is None``): full load of every accessible standard
        channel, streamed as checkpointed batches (resumable — AC3). Incremental
        run: a Graph delta query per channel returns only messages changed since
        the stored delta position (AC2). An unchanged workspace yields a single
        empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming
        position (AC2).

        Live vs offline (see the module "Checkpoint shape" docstring): in live
        mode the per-channel checkpoint is Microsoft Graph's own ``deltaLink`` and
        Graph returns only the changed messages, so the connector advances a
        channel's checkpoint to the new deltaLink only after that channel's LAST
        batch (a mid-channel failure resumes from the OLD deltaLink, which Graph
        re-serves losslessly). In offline mode it filters the fixture by an ISO
        high-water marker and advances per batch (fine-grained resume).
        """
        tokens: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        # Working copy we advance as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a
        # valid resume point on the next run.
        running = dict(tokens)
        live = is_live()

        try:
            channels = self._accessible_channels(org_id)
            logger.info(
                "teams: org=%s %s — %d accessible standard channel(s)",
                org_id,
                "first run (full load)" if since is None else "incremental run",
                len(channels),
            )

            # Query each channel's delta first so we know which batch is the final
            # one overall and can flag is_complete=True on exactly that batch (the
            # runner needs one terminal batch to advance). Each entry carries the
            # channel's changed messages plus its NEW opaque position token
            # (the Graph deltaLink in live mode; None offline).
            pending: List[Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[str]]] = []
            for channel in channels:
                key = _channel_key(channel["team_id"], channel["id"])
                messages, next_token = self._channel_delta(org_id, channel, tokens.get(key))
                if messages:
                    pending.append((channel, messages, next_token))

            if not pending:
                # Unchanged workspace → empty delta that echoes the incoming
                # position (no regression). On a first run with no accessible
                # channels this records an empty position map.
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
                for _, msgs, _ in pending
            )
            emitted = 0
            for channel, messages, next_token in pending:
                key = _channel_key(channel["team_id"], channel["id"])
                n_pages = (len(messages) + self.batch_size - 1) // self.batch_size
                for page_idx, start in enumerate(range(0, len(messages), self.batch_size)):
                    page = messages[start : start + self.batch_size]
                    records = [self._to_record(channel, m) for m in page]
                    if live:
                        # Advance to Graph's new deltaLink only after this channel's
                        # LAST page — a mid-channel failure must resume from the OLD
                        # deltaLink (Graph re-serves the same delta), never skip.
                        if page_idx == n_pages - 1 and next_token:
                            running[key] = next_token
                    else:
                        # Offline: advance per batch to the newest ISO marker in the
                        # page (fine-grained, per-batch resumability).
                        running[key] = _change_marker(page[-1])
                    emitted += 1
                    yield DeltaBatch(
                        records=records,
                        next_checkpoint=_encode_checkpoint(running),
                        is_complete=(emitted == total_batches),
                    )
        finally:
            # Release the live Graph client's HTTP session (H2) regardless of how
            # the generator ends (exhausted, closed, or raised).
            self._close_client()

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

    def _channel_delta(
        self, org_id: str, channel: Dict[str, Any], token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return ``(changed_messages, next_token)`` for a channel.

        Live: run the Microsoft Graph delta query, passing the stored
        ``@odata.deltaLink`` as ``token`` (or None on the first run). Graph returns
        ONLY the messages changed since that position and a NEW ``@odata.deltaLink``
        to persist — so the connector never re-scans or filters client-side (H1).
        Returns ``(records, new_delta_link)``.

        Offline: ``token`` is the ISO high-water marker; filter the fixture's
        messages to those strictly newer, oldest-first (so the marker advances
        monotonically). ``next_token`` is None — the offline path advances per batch
        by the message marker instead.
        """
        if is_live():
            return self._client(org_id).messages_delta(
                channel["team_id"], channel["id"], token
            )
        key = _channel_key(channel["team_id"], channel["id"])
        messages = list(self._fixture().get("messages", {}).get(key, []))
        fresh = [m for m in messages if _marker_gt(_change_marker(m), token)]
        fresh.sort(key=lambda m: _marker_epoch(_change_marker(m)) or float("-inf"))
        return fresh, None

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

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise TeamsIngestError(f"Teams fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "TeamsGraphClient":
        """Return the per-run Microsoft Graph client, creating it once and reusing it.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token, isolated per org/run) first, then the per-org
        vault via the single credential path — never a process-global env
        credential (R17-D3 Addendum A, AC8/AC11). The OAuth connect flow that
        lands the token in the vault is T5 / AT-434.

        The client (and its single HTTP session) is cached on the ingestor for the
        duration of one ``ingest_changes`` call — so enumerating N teams' channels
        reuses ONE connection pool (M2) instead of opening a new session per team —
        and is closed in ``ingest_changes``'s ``finally`` (H2).
        """
        if self._graph_client is None:
            cred = get_live_connector("teams") or resolve_vault_connector("teams", org_id)
            token = cred.get("token") if cred else None
            if not token:
                raise TeamsIngestError(
                    "Live mode requires a Microsoft Graph OAuth token from the "
                    "credential vault. Connect Teams in the Integration Hub, or set "
                    "INGEST_MODE=offline to run without credentials."
                )
            self._graph_client = TeamsGraphClient(token.strip())
        return self._graph_client

    def _close_client(self) -> None:
        """Close and drop the cached Graph client's HTTP session, if any (H2)."""
        if self._graph_client is not None:
            self._graph_client.close()
            self._graph_client = None


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
        # Cumulative seconds slept on 429 Retry-After waits across this client's
        # lifetime (one ingest). Bounded by _MAX_TOTAL_THROTTLE_WAIT_SECONDS.
        self._throttle_waited = 0.0

    # Reusable as a context manager so callers can guarantee the session closes.
    def __enter__(self) -> "TeamsGraphClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session, releasing its pooled connections (H2)."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # pragma: no cover — close must never raise
                pass
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
            self._session.headers.update(
                {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            )
        return self._session

    def _get(self, url: str) -> Dict[str, Any]:
        """GET a Graph URL, transparently retrying on 429 throttling (L2).

        Microsoft Graph throttles delta queries with HTTP 429 + a ``Retry-After``
        header. Rather than aborting the whole run on the first 429, wait the
        indicated interval (capped) and retry up to ``_MAX_THROTTLE_RETRIES`` times;
        only if throttling persists do we raise, with an actionable message.
        """
        for attempt in range(_MAX_THROTTLE_RETRIES + 1):
            resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_THROTTLE_RETRIES:
                wait = _retry_after_seconds(resp)
                # Bound total throttle sleep across the whole ingest, not just per
                # request. Once the budget is spent, give up (non-blocking: the
                # caller degrades to an empty block, same as persistent throttling)
                # rather than let a throttled tenant stall the run indefinitely.
                if self._throttle_waited + wait > _MAX_TOTAL_THROTTLE_WAIT_SECONDS:
                    raise TeamsIngestError(
                        "Microsoft Graph throttling (HTTP 429) exceeded the Teams "
                        f"throttle-wait budget ({_MAX_TOTAL_THROTTLE_WAIT_SECONDS}s) "
                        "for this ingest — re-run the discovery after the "
                        "Retry-After window."
                    )
                self._throttle_waited += wait
                logger.warning(
                    "Microsoft Graph throttled (429) on %s; retry %d/%d after %ds",
                    url, attempt + 1, _MAX_THROTTLE_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            if not resp.ok:
                if resp.status_code == 429:
                    raise TeamsIngestError(
                        "Microsoft Graph throttling (HTTP 429) persisted after "
                        f"{_MAX_THROTTLE_RETRIES} retries — re-run the discovery "
                        "after the Retry-After window."
                    )
                raise TeamsIngestError(
                    f"Microsoft Graph GET {url} HTTP {resp.status_code}"
                )
            return resp.json()
        # Unreachable: the loop returns or raises on the final attempt.
        raise TeamsIngestError("Microsoft Graph request exhausted retries")  # pragma: no cover

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

    def messages_delta(
        self, team_id: str, channel_id: str, delta_link: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Run the Graph message delta query and return ``(records, next_delta_link)``.

        This is the heart of the native change mechanism (H1): when ``delta_link``
        is provided (the token persisted from the previous run) it is replayed
        verbatim, so Microsoft Graph returns ONLY the messages changed since that
        position — the connector never re-scans the channel. On the first run
        (``delta_link is None``) it starts a fresh delta enumeration. Either way it
        follows ``@odata.nextLink`` pagination to the terminal page, whose
        ``@odata.deltaLink`` is captured and returned as the new position to
        persist for next time.
        """
        url: Optional[str] = delta_link or (
            f"{_GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages/delta"
        )
        items: List[Dict[str, Any]] = []
        next_delta: Optional[str] = None
        while url:
            data = self._get(url)
            items.extend(data.get("value", []))
            next_page = data.get("@odata.nextLink")
            if next_page:
                url = next_page
                continue
            # Terminal page: Graph hands back the deltaLink to replay next run.
            next_delta = data.get("@odata.deltaLink")
            url = None
        return items, next_delta
