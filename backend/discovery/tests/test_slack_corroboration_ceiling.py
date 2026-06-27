"""R16-A2 / AT-419 (T4) — Slack MEDIUM corroboration ceiling.

AC6: a Slack-only signal is capped at MEDIUM confidence and never produces a
standalone HIGH finding. The ceiling already exists in the corroboration engine
(COR-05 supporting-only / COR-06 elevates only with a primary corroborator, plus
the T3 defence-in-depth clamp). The Slack connector's job is to *feed* its signal
into the engine in the shape the engine consumes — under the ``'slack'`` key and
reported as Slack — so those existing rules apply the cap. These tests exercise
the connector's feeding adapter against the REAL engine to prove the ceiling
holds end to end, and that Slack still legitimately elevates WITH a primary
corroborator (COR-06) — i.e. the ceiling is respected, not bypassed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    evaluate_corroboration,
)
from discovery.ingest.slack_signals import (
    SLACK_CORROBORATION_KEY,
    build_slack_corroboration_payload,
)

_DETECTOR = "HANDOFF_FRICTION"
_PACK = "service_cloud"


def _now():
    return datetime.now(timezone.utc)


def _escalation_records(now):
    """One escalated thread with a recent ts (inside the 30-day window)."""
    ts = f"{now.timestamp():.6f}"
    return [
        {
            "channel_id": "C1",
            "channel_name": "ops-incidents",
            "ts": ts,
            "user": "u1",
            "reply_count": 6,
            "reply_users_count": 4,  # >= ESCALATION_MIN_PARTICIPANTS → escalation
            "reactions": [],
            "text": "war room — customers blocked",
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Feed adapter — correct shape, no smuggled confidence
# ─────────────────────────────────────────────────────────────────────────────
def test_adapter_wraps_signal_under_slack_key():
    payload = build_slack_corroboration_payload(_escalation_records(_now()))
    assert set(payload.keys()) == {SLACK_CORROBORATION_KEY} == {"slack"}
    block = payload["slack"]
    assert block["escalation_pattern"]["fired"] is True
    # The adapter must never smuggle a confidence/elevation — the engine owns that.
    assert "confidence" not in block
    assert "elevated_confidence" not in block


def test_engine_discovers_the_fed_slack_block():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "slack"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_escalation_records(now))],
    )
    assert run_data["slack"]["escalation_pattern"]["fired"] is True


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Slack-only is capped at MEDIUM and never standalone HIGH
# ─────────────────────────────────────────────────────────────────────────────
def test_slack_only_signal_fires_cor05_and_stays_medium():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "slack"],  # 2 systems → COR-08 single-source not triggered
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_escalation_records(now))],
    )

    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    # Slack escalation registers as the supporting-only COR-05 …
    assert "COR-05" in result.rule_ids
    # … never the elevating COR-06 (no primary corroborator present) …
    assert "COR-06" not in result.rule_ids
    # … so the verdict is capped at MEDIUM and is not an elevation.
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


def test_slack_only_never_produces_standalone_high():
    """Even applied against a MEDIUM scorer baseline, a Slack-only verdict can
    never reach HIGH (AC6) — the engine's defence-in-depth clamp holds."""
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "slack"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_escalation_records(now))],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    final = apply_corroboration_confidence("MEDIUM", result)
    assert final == "MEDIUM"
    # And it never downgrades a scorer that was already HIGH for other reasons.
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_slack_only_no_escalation_does_not_corroborate():
    """A quiet workspace (no escalation) feeds an empty pattern — no COR-05/06."""
    now = _now()
    quiet = [{"channel_id": "C1", "channel_name": "random", "ts": f"{now.timestamp():.6f}",
              "user": "u1", "reply_count": 0, "reply_users_count": 0, "reactions": []}]
    run_data = build_corroboration_run_data(
        systems=["salesforce", "slack"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(quiet)],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")
    assert "COR-05" not in result.rule_ids
    assert "COR-06" not in result.rule_ids


# ─────────────────────────────────────────────────────────────────────────────
# Ceiling respected, not bypassed — Slack elevates WITH a primary corroborator
# ─────────────────────────────────────────────────────────────────────────────
def test_slack_with_servicenow_primary_elevates_to_high():
    """COR-06: Slack escalation + a primary system corroborator (ServiceNow) does
    elevate to HIGH. This proves the feeding is correct (the engine sees the Slack
    block) and that the ceiling is respected — Slack contributes to HIGH only when
    a system of record corroborates, never alone."""
    now = _now()
    recent_iso = (now - timedelta(days=1)).isoformat()
    run_data = build_corroboration_run_data(
        systems=["servicenow", "slack"],
        sn_by_detector={_DETECTOR: [{"state": "Open", "sys_created_on": recent_iso}]},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_escalation_records(now))],
    )

    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    assert "COR-01" in result.rule_ids  # ServiceNow primary fired
    assert "COR-06" in result.rule_ids  # Slack elevates WITH the primary
    assert "COR-05" not in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
