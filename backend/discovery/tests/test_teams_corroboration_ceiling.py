"""R17-A1 / AT-433 (T4) — Teams MEDIUM corroboration ceiling (AC6).

AC6: a Teams-only signal is capped at MEDIUM confidence and never produces a
standalone HIGH finding. Teams is a conversation source — like Slack — so it
REUSES the existing corroboration ceiling (no new mechanism): the engine's
conversation-source rules (COR-05 supporting-only / COR-06 elevates only WITH a
primary corroborator) plus the T3 ceiling clamp. These tests exercise the
connector's feed adapter against the REAL engine to prove the ceiling holds end to
end, and that Teams still legitimately elevates WITH a system-of-record (COR-06).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    evaluate_corroboration,
)
from discovery.ingest.teams_signals import (
    TEAMS_CORROBORATION_KEY,
    build_teams_corroboration_payload,
)
from discovery.t3_ceiling_clamp import apply_t3_ceiling_clamp

_DETECTOR = "HANDOFF_FRICTION"
_PACK = "service_cloud"


def _now():
    return datetime.now(timezone.utc)


def _escalation_records(now):
    """One escalated Teams thread (>= 3 distinct repliers) with a recent ts."""
    return [
        {
            "team_id": "T-eng",
            "channel_id": "19:ops",
            "channel_name": "ops-incidents",
            "created_at": now.isoformat(),
            "reply_count": 6,
            "reply_users_count": 4,  # >= ESCALATION_MIN_PARTICIPANTS → escalation
            "reactions": [],
            "text": "war room — customers blocked",
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Feed adapter — correct shape, no smuggled confidence
# ─────────────────────────────────────────────────────────────────────────────
def test_adapter_wraps_signal_under_teams_key():
    payload = build_teams_corroboration_payload(_escalation_records(_now()))
    assert set(payload.keys()) == {TEAMS_CORROBORATION_KEY} == {"teams"}
    block = payload["teams"]
    assert block["escalation_pattern"]["fired"] is True
    assert "confidence" not in block
    assert "elevated_confidence" not in block


def test_engine_discovers_the_fed_teams_block():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "teams"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(_escalation_records(now))],
    )
    assert run_data["teams"]["escalation_pattern"]["fired"] is True


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Teams-only is capped at MEDIUM and never standalone HIGH
# ─────────────────────────────────────────────────────────────────────────────
def test_teams_only_signal_fires_cor05_and_stays_medium():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "teams"],  # 2 systems → COR-08 single-source not triggered
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(_escalation_records(now))],
    )

    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    assert "COR-05" in result.rule_ids            # supporting-only conversation rule
    assert "COR-06" not in result.rule_ids        # no primary corroborator present
    assert any("Teams" in s for s in result.corroboration_sources)
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


def test_teams_only_never_produces_standalone_high():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "teams"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(_escalation_records(now))],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    final = apply_corroboration_confidence("MEDIUM", result)
    assert final == "MEDIUM"
    # And it never downgrades a scorer that was already HIGH for other reasons.
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_teams_only_no_escalation_does_not_corroborate():
    now = _now()
    quiet = [{
        "team_id": "T-eng", "channel_id": "19:random", "channel_name": "random",
        "created_at": now.isoformat(), "reply_count": 0, "reply_users_count": 0,
        "reactions": [], "text": "lunch?",
    }]
    run_data = build_corroboration_run_data(
        systems=["salesforce", "teams"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(quiet)],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")
    assert "COR-05" not in result.rule_ids
    assert "COR-06" not in result.rule_ids


# ─────────────────────────────────────────────────────────────────────────────
# Ceiling respected, not bypassed — Teams elevates WITH a primary corroborator
# ─────────────────────────────────────────────────────────────────────────────
def test_teams_with_servicenow_primary_elevates_to_high():
    """COR-06: Teams escalation + a primary system corroborator (ServiceNow) does
    elevate to HIGH — proving the feed is correct and the ceiling is respected
    (Teams contributes to HIGH only when a system of record corroborates)."""
    now = _now()
    recent_iso = (now - timedelta(days=1)).isoformat()
    run_data = build_corroboration_run_data(
        systems=["servicenow", "teams"],
        sn_by_detector={_DETECTOR: [{"state": "Open", "sys_created_on": recent_iso}]},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(_escalation_records(now))],
    )

    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    assert "COR-01" in result.rule_ids  # ServiceNow primary fired
    assert "COR-06" in result.rule_ids  # Teams elevates WITH the primary
    assert "COR-05" not in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# T3 ceiling clamp — defence-in-depth for the conversation-source ceiling
# ─────────────────────────────────────────────────────────────────────────────
def test_t3_clamp_caps_teams_system_id_at_medium():
    # A finding scored from a Teams system_id can never be HIGH from the scorer.
    assert apply_t3_ceiling_clamp("HIGH", system_id="teams") == "MEDIUM"
    assert apply_t3_ceiling_clamp("HIGH", system_id="teams_workspace") == "MEDIUM"


def test_t3_clamp_caps_teams_only_corroboration_at_medium():
    assert apply_t3_ceiling_clamp(
        "HIGH", corroboration_sources=["Teams (supporting only)"]
    ) == "MEDIUM"
    # Mixed Teams + Slack (both conversation sources, no primary) is still capped.
    assert apply_t3_ceiling_clamp(
        "HIGH", corroboration_sources=["Teams (escalation pattern)", "Slack (supporting only)"]
    ) == "MEDIUM"


def test_t3_clamp_allows_high_when_primary_corroborates_teams():
    # Teams + a system of record → HIGH permitted (COR-06).
    assert apply_t3_ceiling_clamp(
        "HIGH", corroboration_sources=["ServiceNow", "Teams (escalation pattern)"]
    ) == "HIGH"
