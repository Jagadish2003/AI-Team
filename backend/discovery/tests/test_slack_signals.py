"""
R16-A2 / AT-417 (T2) — tests for Slack signal extraction (reach phase).

Covers the acceptance criterion assigned to this subtask:

  AC8 — The connector ingests SIGNAL only: it does not attempt deep
        conversation-content extraction. All three signal types — channel
        activity, participation/escalation, and cross-reference markers — are
        extracted and passed downstream.

The three signal types are tested individually as pure functions, then end to
end through ``SlackIngestor`` (records carry a ``signals`` block) and through the
``build_slack_signal`` aggregator that produces the corroboration-ready payload.
"""
from __future__ import annotations

from datetime import datetime

from discovery.ingest.slack import SlackIngestor
from discovery.ingest.slack_signals import (
    build_slack_signal,
    extract_channel_activity,
    extract_cross_reference_markers,
    extract_escalation_signal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Signal type 3 — cross-reference markers (structured IDs / URLs, not NLP)
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_reference_servicenow_jira_github():
    text = "Saw INC-4821, tracked in PROJ-145, fixed by PR #2290"
    markers = extract_cross_reference_markers(text)
    by_system = {(m["system"], m["ref"]) for m in markers}
    assert ("servicenow", "INC-4821") in by_system
    assert ("jira", "PROJ-145") in by_system
    assert any(m["system"] == "github" and "2290" in m["ref"] for m in markers)


def test_cross_reference_github_shorthands_not_double_classified_as_jira():
    # PR-2290 / GH-12 share the KEY-N shape but must classify as github only.
    markers = extract_cross_reference_markers("rolled back PR-2290 and GH-12")
    systems = {m["system"] for m in markers}
    assert systems == {"github"}
    refs = {m["ref"] for m in markers}
    assert refs == {"PR-2290", "GH-12"}


def test_cross_reference_url_classification():
    text = (
        "see https://acme.atlassian.net/browse/OPS-9 and "
        "https://github.com/acme/app/pull/5 and "
        "https://acme.service-now.com/nav_to.do?uri=incident.do"
    )
    markers = extract_cross_reference_markers(text)
    url_systems = {m["system"] for m in markers if m["kind"] == "url"}
    assert {"jira", "github", "servicenow"} <= url_systems


def test_cross_reference_empty_and_none():
    assert extract_cross_reference_markers("just a normal message") == []
    assert extract_cross_reference_markers("") == []
    assert extract_cross_reference_markers(None) == []


def test_cross_reference_deduplicates():
    markers = extract_cross_reference_markers("INC-4821 again INC-4821 and INC-4821")
    assert len([m for m in markers if m["ref"] == "INC-4821"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Signal type 2 — participation / escalation
# ─────────────────────────────────────────────────────────────────────────────
def test_escalation_many_participants():
    sig = extract_escalation_signal(
        {"reply_count": 4, "reply_users_count": 3, "reactions": []}
    )
    assert sig["many_participants"] is True
    assert sig["is_escalation"] is True


def test_escalation_rapid_back_and_forth():
    sig = extract_escalation_signal(
        {"reply_count": 6, "reply_users_count": 2, "reactions": []}
    )
    assert sig["rapid_back_and_forth"] is True
    assert sig["is_escalation"] is True


def test_escalation_none_for_quiet_message():
    sig = extract_escalation_signal(
        {"reply_count": 1, "reply_users_count": 1, "reactions": [{"name": "ok", "count": 1}]}
    )
    assert sig["is_escalation"] is False
    assert sig["reaction_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Signal type 1 — channel activity / cadence / bursts
# ─────────────────────────────────────────────────────────────────────────────
def test_channel_activity_volume_participants_and_burst():
    channel = {"id": "C1", "name": "ops"}
    # Three messages within one hour → a burst; two distinct users.
    messages = [
        {"ts": "1718000000.000000", "user": "U1"},
        {"ts": "1718000600.000000", "user": "U2"},
        {"ts": "1718003600.000000", "user": "U1"},
    ]
    act = extract_channel_activity(channel, messages)
    assert act["message_count"] == 3
    assert act["participant_count"] == 2
    assert act["max_burst"] == 3
    assert act["burst_detected"] is True


def test_channel_activity_no_burst_when_spread_out():
    channel = {"id": "C1", "name": "ops"}
    messages = [
        {"ts": "1718000000.000000", "user": "U1"},
        {"ts": "1718100000.000000", "user": "U2"},  # >1 day later
    ]
    act = extract_channel_activity(channel, messages)
    assert act["burst_detected"] is False


def test_channel_activity_empty():
    act = extract_channel_activity({"id": "C1", "name": "ops"}, [])
    assert act["message_count"] == 0
    assert act["burst_detected"] is False
    assert act["first_ts"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AC8 end-to-end — SlackIngestor records carry all three signal types, no NLP
# ─────────────────────────────────────────────────────────────────────────────
def test_ingestor_records_carry_signals_block():
    records = [r for b in SlackIngestor().ingest_changes("org1", None) for r in b.records]
    assert records
    for r in records:
        assert "signals" in r
        sig = r["signals"]
        # Exactly the reach-phase structured signal — no content/meaning fields.
        assert set(sig.keys()) == {"cross_references", "escalation"}
        assert isinstance(sig["cross_references"], list)
        assert "is_escalation" in sig["escalation"]


def test_ingestor_extracts_all_three_signal_types_end_to_end():
    records = [r for b in SlackIngestor().ingest_changes("org1", None) for r in b.records]
    signal = build_slack_signal(records)

    # (1) Activity — per-channel volume/burst present for accessible channels.
    assert set(signal["activity"].keys()) == {"C001", "C002"}
    assert signal["activity"]["C001"]["message_count"] == 4

    # (2) Escalation — the high-participation thread fires the escalation pattern,
    #     in the shape the corroboration engine reads (fired + recognisable ts).
    esc = signal["escalation_pattern"]
    assert esc["fired"] is True
    assert esc["escalated_thread_count"] >= 1
    # timestamp is an ISO string the corroboration engine's _record_timestamp reads.
    assert isinstance(esc["timestamp"], str)
    datetime.fromisoformat(esc["timestamp"])  # parses without raising

    # (3) Cross-reference markers — the fixture's INC + PR references surface.
    refs = {(m["system"], m["ref"]) for m in signal["cross_references"]}
    assert ("servicenow", "INC-4821") in refs
    assert any(m["system"] == "github" for m in signal["cross_references"])


def test_ac8_text_passed_through_verbatim_not_transformed():
    """Reach phase carries the raw text for downstream marker scanning but never
    transforms/summarises it — proof no deep-content NLP is applied here."""
    records = [r for b in SlackIngestor().ingest_changes("org1", None) for r in b.records]
    msg2 = next(r for r in records if r["artifact_id"] == "C001:1718000600.000200")
    assert msg2["text"] == "Looking — looks related to INC-4821 in ServiceNow"


def test_build_slack_signal_empty_records():
    signal = build_slack_signal([])
    assert signal["escalation_pattern"]["fired"] is False
    assert signal["escalation_pattern"]["timestamp"] is None
    assert signal["activity"] == {}
    assert signal["cross_references"] == []
