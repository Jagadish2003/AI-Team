"""
R16-A2 / AT-417 (T2) — Slack signal extraction (reach phase).

Turns the structured Slack messages produced by :mod:`discovery.ingest.slack`
(AT-416) into the three operational SIGNAL types the story's Section 2 calls for —
WITHOUT reading the meaning of any conversation. This is the reach phase: counts,
timing, participation, and pattern-matched markers only. Deep conversation-content
NLP (summarisation, topic/sentiment/entity extraction over message bodies) is the
separate 1.8 deep-content story and is deliberately NOT done here (AC8).

The three signal types
----------------------
1. **Channel activity patterns** — message volume, cadence (messages/day), and
   bursts (a spike of messages in a short window) per channel. See
   :func:`extract_channel_activity`.
2. **Participation & escalation signals** — threads with many participants and
   rapid back-and-forth that indicate friction/escalation. See
   :func:`extract_escalation_signal`. Aggregated into the corroboration-ready
   ``escalation_pattern`` block by :func:`build_slack_signal`.
3. **Cross-reference markers** — mentions of tickets / PRs / systems (e.g.
   ``INC-4821``, ``PROJ-123``, ``PR #2290``) that let the corroboration engine
   link a Slack signal to a finding in another system. See
   :func:`extract_cross_reference_markers`. This is structured pattern matching
   (regex over IDs/URLs), NOT content understanding.

Why pattern-matching markers is not "deep content" (AC8)
--------------------------------------------------------
Extracting ``INC-4821`` from a message is the same class of operation the
Salesforce connector already does in ``get_cross_system_references`` (subject
``LIKE '%INC-%'``): it finds a structured external identifier. It does not parse
what the message *says*. That distinction is the reach/depth boundary.

Downstream shape
----------------
:func:`build_slack_signal` aggregates per-message records into the block the
corroboration engine reads for COR-05/COR-06 (see
``app/corroboration_engine.py`` and ``discovery/packs/corroboration_rules.py``)::

    {
      "escalation_pattern": {"fired": bool, "timestamp": "<iso>", ...},
      "activity": {"<channel_id>": {...}, ...},
      "cross_references": [{"system": ..., "kind": ..., "ref": ...}, ...],
    }

The MEDIUM corroboration ceiling for Slack-only signal is enforced by the
corroboration engine (COR-05) and is the subject of AT-419 (T4); this module only
*produces* the signal in a shape that rule can consume.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

# ─────────────────────────────────────────────────────────────────────────────
# Escalation thresholds (participation / back-and-forth)
# ─────────────────────────────────────────────────────────────────────────────
#: A thread with at least this many distinct repliers is "many participants".
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

# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference marker patterns (structured IDs / URLs — NOT NLP)
# ─────────────────────────────────────────────────────────────────────────────
#: ServiceNow record prefixes (incident/change/request-item/problem).
_SERVICENOW_RE = re.compile(r"\b(INC|CHG|RITM|PRB)-?(\d{3,})\b")
#: GitHub PR / issue shorthands: ``PR #12``, ``PR-12``, ``GH-12``, ``#12``.
_GITHUB_REF_RE = re.compile(r"(?:\bPR[-\s]?#?|\bGH-|(?<!\w)#)(\d+)\b")
#: Jira-style issue keys: ``PROJ-123`` (2–10 char uppercase project + number).
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
#: Any URL — classified to a system by host below.
_URL_RE = re.compile(r"https?://[^\s>|]+")
#: ServiceNow prefixes excluded from the generic Jira-key scan so e.g. INC-1
#: is not double-classified as a Jira key.
_SERVICENOW_PREFIXES = {"INC", "CHG", "RITM", "PRB"}


def _slack_ts_to_iso(ts: str) -> Optional[str]:
    """Convert a Slack ``epoch.micro`` ts string to a UTC ISO-8601 string.

    Returns None for an unparseable ts. Used so the aggregated escalation block
    carries a ``timestamp`` the corroboration engine recognises and can window.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def build_evidence_pointer(channel_id: str, ts: str) -> Dict[str, Any]:
    """Build the R16-B1 EvidencePointer for a single Slack message signal (AT-418).

    Every Slack signal must be traceable back to its source message, so each
    record carries a fully-populated, OBSERVED provenance pointer (AC5):

      * ``source_system`` = ``'slack'``
      * ``source_artifact`` = the message identity ``"{channel_id}:{ts}"`` — the
        unique id of the source message (stable, so ``source_artifact_type`` is
        ``'record_id'``); for a threaded reply this is still the reply message's
        own ``channel:ts``, which uniquely identifies it within the thread.
      * ``source_timestamp`` = the message's own UTC ISO-8601 timestamp (derived
        from the Slack ``epoch.micro`` ts); falls back to now only if the ts is
        missing/unparseable, so the mandatory spine is always populated.
      * ``origin`` = ``'observed'`` — read directly from Slack, never inferred, so
        no ``extraction_job_id`` is required.

    Returned as a JSON-serialisable dict (the extensible 1.6 detail fields are
    present-but-null) ready to attach to the delta record's metadata.
    """
    return EvidencePointer.observed(
        source_system="slack",
        source_artifact=f"{channel_id}:{ts}",
        source_timestamp=_slack_ts_to_iso(ts) or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


def extract_cross_reference_markers(text: Optional[str]) -> List[Dict[str, str]]:
    """Extract structured cross-system reference markers from message text.

    Finds ticket ids, PR/issue refs, Jira keys, and system URLs by pattern —
    never by interpreting message meaning (AC8). Returns a de-duplicated list of
    ``{"system", "kind", "ref"}`` dicts in first-seen order; an empty list when
    the message references nothing.
    """
    if not text or not isinstance(text, str):
        return []

    markers: List[Dict[str, str]] = []
    seen: set = set()
    #: Exact refs already classified to a concrete system, so the generic Jira
    #: key scan does not re-classify e.g. ``PR-2290`` or ``GH-12`` as Jira keys.
    claimed: set = set()

    def _add(system: str, kind: str, ref: str) -> None:
        claimed.add(ref.upper())
        key = (system, ref.upper())
        if key not in seen:
            seen.add(key)
            markers.append({"system": system, "kind": kind, "ref": ref})

    # ServiceNow tickets (INC/CHG/RITM/PRB).
    for m in _SERVICENOW_RE.finditer(text):
        _add("servicenow", "ticket", m.group(0))

    # GitHub PR / issue shorthands.
    for m in _GITHUB_REF_RE.finditer(text):
        _add("github", "pr_or_issue", m.group(0).strip())

    # Jira issue keys — skip ServiceNow-prefixed ids and any ref already claimed
    # by a more specific system (GitHub PR/GH shorthands share the KEY-N shape).
    for m in _JIRA_KEY_RE.finditer(text):
        if m.group(1).upper() in _SERVICENOW_PREFIXES:
            continue
        if m.group(0).upper() in claimed:
            continue
        _add("jira", "issue", m.group(0))

    # URLs, classified to a system by host.
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,);")
        low = url.lower()
        if "atlassian.net" in low or "/jira" in low:
            system = "jira"
        elif "github.com" in low:
            system = "github"
        elif "service-now.com" in low or "servicenow" in low:
            system = "servicenow"
        else:
            system = "other"
        _add(system, "url", url)

    return markers


def extract_escalation_signal(message: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a per-thread participation / escalation signal from a message.

    Pure counts of structured fields (replies, distinct repliers, reactions) —
    the reach-phase proxy for "friction": a thread with many participants or
    rapid back-and-forth. No content is read (AC8).

    ``is_escalation`` is True when a thread has many participants OR is rapid
    back-and-forth, matching the friction patterns in Section 2.
    """
    reply_count = int(message.get("reply_count", 0) or 0)
    participant_count = int(message.get("reply_users_count", 0) or 0)
    reactions = message.get("reactions") or []
    reaction_count = sum(int(r.get("count", 0) or 0) for r in reactions if isinstance(r, dict))

    many_participants = participant_count >= ESCALATION_MIN_PARTICIPANTS
    back_and_forth = reply_count >= ESCALATION_MIN_REPLIES and participant_count >= 2

    return {
        "reply_count": reply_count,
        "participant_count": participant_count,
        "reaction_count": reaction_count,
        "many_participants": many_participants,
        "rapid_back_and_forth": back_and_forth,
        "is_escalation": bool(many_participants or back_and_forth),
    }


def extract_channel_activity(
    channel: Dict[str, Any], messages: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Extract volume / cadence / burst activity signal for one channel.

    ``messages`` may be raw Slack messages or enriched delta records — only
    ``ts`` and ``user`` are read. All values are derived from counts and
    timestamps (no content). ``burst_detected`` flags a spike of
    ``BURST_MIN_MESSAGES`` within any ``BURST_WINDOW_SECONDS`` window.
    """
    ts_values: List[float] = []
    users: set = set()
    for m in messages:
        try:
            ts_values.append(float(m.get("ts", "")))
        except (TypeError, ValueError):
            continue
        if m.get("user"):
            users.add(m["user"])

    message_count = len(messages)
    ts_values.sort()
    if ts_values:
        span_seconds = ts_values[-1] - ts_values[0]
        first_ts, last_ts = ts_values[0], ts_values[-1]
    else:
        span_seconds = 0.0
        first_ts = last_ts = None

    # Cadence: messages per day across the observed span (>=1 day floor so a
    # tight burst does not report an astronomically high rate).
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

    return {
        "channel_id": channel.get("id", ""),
        "channel_name": channel.get("name", ""),
        "message_count": message_count,
        "participant_count": len(users),
        "span_seconds": round(span_seconds, 3),
        "cadence_per_day": cadence_per_day,
        "max_burst": max_burst,
        "burst_detected": max_burst >= BURST_MIN_MESSAGES,
        "first_ts": ("%.6f" % first_ts) if first_ts is not None else None,
        "last_ts": ("%.6f" % last_ts) if last_ts is not None else None,
    }


def build_slack_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate enriched Slack delta records into the downstream signal block.

    Groups records by channel and produces the structure the corroboration
    engine reads for COR-05 / COR-06 (an ``escalation_pattern`` block with
    ``fired`` + ``timestamp``), plus per-channel activity and the de-duplicated
    set of cross-reference markers. This is the "passed downstream" payload — it
    *produces* Slack signal; whether it elevates confidence (and the MEDIUM
    ceiling) is decided by the corroboration engine (AT-419 / T4).
    """
    records = list(records)

    by_channel: Dict[str, Dict[str, Any]] = {}
    for r in records:
        cid = r.get("channel_id", "")
        ch = by_channel.setdefault(
            cid,
            {"channel": {"id": cid, "name": r.get("channel_name", "")}, "messages": []},
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
        (r.get("ts", "") for r in escalated),
        default="",
        key=lambda t: float(t) if _is_float(t) else float("-inf"),
    )
    escalation_pattern = {
        "fired": bool(escalated),
        "timestamp": _slack_ts_to_iso(latest_ts) if escalated else None,
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
#: keys the Slack MEDIUM ceiling off this exact system id (COR-05/COR-06 and the
#: T3 ``SLACK_SYSTEM_IDS`` clamp), so the block MUST be fed under this key and the
#: signal MUST be reported as Slack — never relabelled as another system, which
#: would bypass the ceiling.
SLACK_CORROBORATION_KEY = "slack"


def build_slack_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package Slack signal into the corroboration-engine input block (AT-419 / T4).

    Wraps :func:`build_slack_signal` under the ``'slack'`` key that the
    corroboration engine's ``_find_corroboration_block('slack', …)`` recognises.
    The engine reads the ``escalation_pattern`` from this block to fire COR-05
    (Slack supporting-only, stays MEDIUM) or COR-06 (Slack WITH a primary system
    corroborator, elevates to HIGH).

    This function only *feeds* Slack signal in the shape the engine consumes — it
    deliberately attaches no confidence and performs no elevation. The Slack
    MEDIUM ceiling (a Slack-only signal is capped at MEDIUM and never produces a
    standalone HIGH finding, AC6) is enforced entirely by the corroboration
    engine (COR-05's ``elevates=False`` / exclusion from elevating rules and the
    defence-in-depth clamp in ``apply_corroboration_confidence``) and the T3
    ceiling clamp. Reporting the signal as ``'slack'`` is exactly what lets those
    existing rules apply the cap — do not relabel it or add a confidence here.
    """
    return {SLACK_CORROBORATION_KEY: build_slack_signal(records)}


def _is_float(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
