"""
R17-A1 / AT-431 (T2) + AT-432 (T3) — Teams signal extraction (reach phase).

Turns the structured Teams messages produced by :mod:`discovery.ingest.teams`
(AT-430) into the operational SIGNAL types the story's Section 2 calls for —
WITHOUT reading the meaning of any conversation. This is the reach phase: counts,
timing, participation, and pattern-matched markers only. Deep conversation-content
NLP (summarisation, topic/sentiment/entity extraction over message bodies) is the
separate 1.8 deep-content story and is deliberately NOT done here (AC8).

The signal types (Section 2)
----------------------------
1. **Channel activity patterns** — message volume, cadence (messages/day), and
   bursts (a spike of messages in a short window) per channel. See
   :func:`extract_channel_activity`.
2. **Participation & escalation signals** — threads with many participants and
   rapid back-and-forth that indicate friction/urgency. See
   :func:`extract_escalation_signal`. Aggregated into the corroboration-ready
   ``escalation_pattern`` block by :func:`build_teams_signal`.
3. **Cross-reference markers** — mentions of tickets / PRs / systems (e.g.
   ``INC-4821``, ``PROJ-123``, ``PR #2290``) that let the corroboration engine
   link a Teams signal to a finding in another system. This is structured pattern
   matching (regex over IDs/URLs), NOT content understanding, and is identical
   across conversation sources, so the source-agnostic extractor from
   :mod:`discovery.ingest.slack_signals` is reused verbatim (a ticket id is a
   ticket id whether it is mentioned in Slack or Teams) — see the re-export of
   :func:`extract_cross_reference_markers` below.

Why pattern-matching markers is not "deep content" (AC8)
--------------------------------------------------------
Extracting ``INC-4821`` from a message is the same class of operation the
Salesforce connector already does (subject ``LIKE '%INC-%'``): it finds a
structured external identifier. It does not parse what the message *says*. That
distinction is the reach/depth boundary.

Provenance (AT-432 / T3, AC5)
-----------------------------
:func:`build_evidence_pointer` builds the R16-B1 :class:`EvidencePointer` every
Teams signal must carry: ``source_system='teams'``, the message/thread id, the
message timestamp, and ``origin='observed'`` — so no Teams signal enters the
system without a verifiable, auditable source reference.

Downstream shape
----------------
:func:`build_teams_signal` aggregates per-message records into the block the
corroboration engine reads, mirroring the Slack aggregator::

    {
      "escalation_pattern": {"fired": bool, "timestamp": "<iso>", ...},
      "activity": {"<channel_id>": {...}, ...},
      "cross_references": [{"system": ..., "kind": ..., "ref": ...}, ...],
    }

The MEDIUM corroboration ceiling for a Teams-only signal is enforced by the
corroboration engine (the subject of T4), not here. This module only *produces*
the signal in a shape that rule can consume — exactly as ``slack_signals`` does
for Slack.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

# Cross-reference marker extraction is source-agnostic structured pattern matching
# (ServiceNow/Jira/GitHub ids + URLs). Reuse the Slack implementation verbatim so a
# ticket/PR reference is detected identically across every conversation source,
# rather than maintaining a second copy of the same regex set.
from .slack_signals import extract_cross_reference_markers

__all__ = [
    "extract_cross_reference_markers",
    "extract_escalation_signal",
    "extract_channel_activity",
    "build_evidence_pointer",
    "build_teams_signal",
    "build_teams_corroboration_payload",
    "TEAMS_CORROBORATION_KEY",
    "ESCALATION_MIN_PARTICIPANTS",
    "ESCALATION_MIN_REPLIES",
]

# ─────────────────────────────────────────────────────────────────────────────
# Escalation thresholds (participation / back-and-forth)
# ─────────────────────────────────────────────────────────────────────────────
#: A thread with at least this many distinct participants is "many participants".
ESCALATION_MIN_PARTICIPANTS = 3
#: A thread with at least this many replies is rapid back-and-forth.
ESCALATION_MIN_REPLIES = 5

# ─────────────────────────────────────────────────────────────────────────────
# Channel activity / burst tuning
# ─────────────────────────────────────────────────────────────────────────────
#: Rolling window (seconds) used to detect a burst of messages.
BURST_WINDOW_SECONDS = 3600
#: Messages within one window at or above this count constitute a burst.
BURST_MIN_MESSAGES = 3
_SECONDS_PER_DAY = 86400.0


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Microsoft Graph uses ``...Z``) to epoch seconds.

    Returns None for a missing/unparseable value so callers can skip it. Used for
    the cadence/burst windowing maths; the signal block itself keeps the original
    ISO strings the corroboration engine recognises.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _msg_timestamp(msg: Dict[str, Any]) -> Optional[str]:
    """The message's own timestamp, from a delta record (``created_at``) or a raw
    Graph message (``createdDateTime``). Reach-phase signal is keyed off this."""
    return msg.get("created_at") or msg.get("createdDateTime")


def build_evidence_pointer(
    team_id: str, channel_id: str, message_id: str, timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 EvidencePointer for a single Teams message signal (AT-432).

    Every Teams signal must be traceable back to its source message, so each
    record carries a fully-populated, OBSERVED provenance pointer (AC5):

      * ``source_system`` = ``'teams'``
      * ``source_artifact`` = the message identity ``"{team_id}/{channel_id}:{message_id}"``
        — the unique id of the source message (stable, so ``source_artifact_type``
        is ``'record_id'``); for a threaded reply this is still the reply
        message's own id, which uniquely identifies it within the thread.
      * ``source_timestamp`` = the message's own UTC ISO-8601 timestamp; falls
        back to now only if the timestamp is missing/unparseable, so the mandatory
        spine is always populated and a signal is never dropped for provenance.
      * ``origin`` = ``'observed'`` — read directly from Teams via Microsoft Graph,
        never inferred, so no ``extraction_job_id`` is required.

    Returned as a JSON-serialisable dict (the extensible 1.6 detail fields are
    present-but-null) ready to attach to the delta record's metadata.
    """
    return EvidencePointer.observed(
        source_system="teams",
        source_artifact=f"{team_id}/{channel_id}:{message_id}",
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


def extract_escalation_signal(message: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a per-thread participation / escalation signal from a message.

    Pure counts of structured fields (replies, distinct repliers, @-mentions,
    reactions) — the reach-phase proxy for "friction": a thread with many
    participants or rapid back-and-forth. No content is read (AC8).

    Teams adds @-mentions as a native participation signal alongside reply
    counts, so a heavily-mentioned thread also counts as "many participants".
    ``is_escalation`` is True when a thread has many participants OR is rapid
    back-and-forth, matching the friction patterns in Section 2.

    Accepts either a raw Graph message or an enriched delta record — both expose
    ``reply_count`` / ``reply_users_count`` / ``mentions`` / ``reactions``; any
    absent field defaults to zero rather than raising.
    """
    reply_count = int(message.get("reply_count", 0) or 0)
    participant_count = int(message.get("reply_users_count", 0) or 0)
    mentions = message.get("mentions") or []
    mention_count = len(mentions) if isinstance(mentions, list) else 0
    reactions = message.get("reactions") or []
    # Graph reactions are one entry per reacting user; the offline fixture may
    # collapse them with an explicit ``count``. Handle both: an explicit count
    # wins, otherwise each entry counts as one.
    reaction_count = sum(
        int(r.get("count", 1) or 0) for r in reactions if isinstance(r, dict)
    )

    many_participants = (
        participant_count >= ESCALATION_MIN_PARTICIPANTS
        or mention_count >= ESCALATION_MIN_PARTICIPANTS
    )
    back_and_forth = reply_count >= ESCALATION_MIN_REPLIES and participant_count >= 2

    return {
        "reply_count": reply_count,
        "participant_count": participant_count,
        "mention_count": mention_count,
        "reaction_count": reaction_count,
        "many_participants": many_participants,
        "rapid_back_and_forth": back_and_forth,
        "is_escalation": bool(many_participants or back_and_forth),
    }


def extract_channel_activity(
    channel: Dict[str, Any], messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Extract volume / cadence / burst activity signal for one channel.

    ``messages`` may be raw Graph messages or enriched delta records — only the
    message timestamp and author are read. All values are derived from counts and
    timestamps (no content). ``burst_detected`` flags a spike of
    ``BURST_MIN_MESSAGES`` within any ``BURST_WINDOW_SECONDS`` window.
    """
    ts_values: List[float] = []
    users: set = set()
    for m in messages:
        epoch = _iso_to_epoch(_msg_timestamp(m))
        if epoch is not None:
            ts_values.append(epoch)
        if m.get("user"):
            users.add(m["user"])

    message_count = len(messages)
    ts_values.sort()
    if ts_values:
        span_seconds = ts_values[-1] - ts_values[0]
        first_epoch, last_epoch = ts_values[0], ts_values[-1]
    else:
        span_seconds = 0.0
        first_epoch = last_epoch = None

    # Cadence: messages per day across the observed span (>=1 day floor so a tight
    # burst does not report an astronomically high rate).
    days = max(span_seconds / _SECONDS_PER_DAY, 1.0)
    cadence_per_day = round(message_count / days, 4)

    # Burst: max messages within any rolling BURST_WINDOW_SECONDS window
    # (two-pointer over sorted timestamps).
    max_burst = 0
    start = 0
    for end in range(len(ts_values)):
        while ts_values[end] - ts_values[start] > BURST_WINDOW_SECONDS:
            start += 1
        max_burst = max(max_burst, end - start + 1)

    def _iso(epoch: Optional[float]) -> Optional[str]:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    return {
        "channel_id": channel.get("id", channel.get("channel_id", "")),
        "channel_name": channel.get("displayName", channel.get("channel_name", "")),
        "message_count": message_count,
        "participant_count": len(users),
        "span_seconds": round(span_seconds, 3),
        "cadence_per_day": cadence_per_day,
        "max_burst": max_burst,
        "burst_detected": max_burst >= BURST_MIN_MESSAGES,
        "first_ts": _iso(first_epoch),
        "last_ts": _iso(last_epoch),
    }


def build_teams_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate enriched Teams delta records into the downstream signal block.

    Groups records by channel and produces the structure the corroboration engine
    reads (an ``escalation_pattern`` block with ``fired`` + ``timestamp``), plus
    per-channel activity and the de-duplicated set of cross-reference markers.
    This is the "passed downstream" payload — it *produces* Teams signal; whether
    it elevates confidence (and the MEDIUM ceiling) is decided by the
    corroboration engine (T4).
    """
    records = list(records)

    by_channel: Dict[str, Dict[str, Any]] = {}
    for r in records:
        cid = r.get("channel_id", "")
        ch = by_channel.setdefault(
            cid,
            {
                "channel": {"id": cid, "displayName": r.get("channel_name", "")},
                "messages": [],
            },
        )
        ch["messages"].append(r)

    activity = {
        cid: extract_channel_activity(group["channel"], group["messages"])
        for cid, group in by_channel.items()
    }

    # Escalation: a record's signal may already carry escalation (attached by the
    # ingestor); fall back to computing it from the record's own fields.
    escalated: List[Dict[str, Any]] = []
    for r in records:
        esc = (r.get("signals") or {}).get("escalation") or extract_escalation_signal(r)
        if esc.get("is_escalation"):
            escalated.append(r)

    latest_ts = max(
        (_msg_timestamp(r) or "" for r in escalated),
        default="",
        key=lambda t: _iso_to_epoch(t) if _iso_to_epoch(t) is not None else float("-inf"),
    )
    escalation_pattern = {
        "fired": bool(escalated),
        "timestamp": latest_ts if escalated and latest_ts else None,
        "escalated_thread_count": len(escalated),
        "channels": sorted({r.get("channel_id", "") for r in escalated}),
    }

    # Aggregate, de-duplicated cross-reference markers across all records.
    cross_references: List[Dict[str, str]] = []
    seen: set = set()
    for r in records:
        markers = (r.get("signals") or {}).get("cross_references")
        if markers is None:
            markers = extract_cross_reference_markers(r.get("text"))
        for mk in markers:
            key = (mk.get("system"), (mk.get("ref") or "").upper())
            if key not in seen:
                seen.add(key)
                cross_references.append(mk)

    return {
        "escalation_pattern": escalation_pattern,
        "activity": activity,
        "cross_references": cross_references,
    }


#: The connector's own source identity in the corroboration input. The engine
#: keys the Teams MEDIUM ceiling off this exact system id (the conversation-source
#: cap T4 enforces), so the block MUST be fed under this key and the signal MUST
#: be reported as Teams — never relabelled as another system, which would bypass
#: the ceiling.
TEAMS_CORROBORATION_KEY = "teams"


def build_teams_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package Teams signal into the corroboration-engine input block.

    Wraps :func:`build_teams_signal` under the ``'teams'`` key the corroboration
    engine recognises. This function only *feeds* Teams signal in the shape the
    engine consumes — it deliberately attaches no confidence and performs no
    elevation. The Teams MEDIUM ceiling (a Teams-only signal is capped at MEDIUM
    and never produces a standalone HIGH finding, AC6) is enforced entirely by the
    corroboration engine (T4). Reporting the signal as ``'teams'`` is exactly what
    lets that rule apply the cap — do not relabel it or add a confidence here.
    """
    return {TEAMS_CORROBORATION_KEY: build_teams_signal(records)}
