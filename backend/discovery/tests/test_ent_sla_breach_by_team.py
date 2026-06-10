"""
ENT-5 / AT-264 (T3) — ENT_SLA_BREACH_BY_TEAM detector unit tests.

Acceptance criteria coverage:
  AC4 — fires when top_team_breach_pct >= 0.40 AND top_team_breach_rate >= 0.25
         AND teams_analysed >= 3.
  AC5 — confidence elevates to HIGH when the top team resolves to a Team entity
         in the knowledge graph (ENT-1 overlay) AND that team has a high Jira
         open-issue count. Exact-name fallback never elevates above MEDIUM.
  AC6 (partial) — SIGNAL_METRICS defined; numeric metrics numeric and present in
         raw_evidence, at most 8 (top_team_name is the documented string id).

Plus edge cases: exact-threshold firing, per-condition non-firing, raw-incident
grouping, ties/empty data, degraded signal, and the cross-system matching paths.
"""
from __future__ import annotations

from discovery.detectors.ent_sla_breach_by_team import (
    CONFIDENCE_CORROBORATED,
    CONFIDENCE_STANDALONE,
    DETECTOR_ID,
    HIGH_JIRA_OPEN_ISSUES,
    MIN_TEAMS_ANALYSED,
    SIGNAL_METRICS,
    TOP_TEAM_BREACH_PCT_THRESHOLD,
    TOP_TEAM_BREACH_RATE_THRESHOLD,
    corroborate_top_team,
    detect,
    evaluate,
)


def _sn(teams, *, degraded=False, overlay=None):
    """sn_data with pre-grouped team breach rows. teams: list of (name, total, breached)."""
    block = {
        "sla_breach_by_team": {
            "teams": [
                {"team": name, "total_tickets": total, "breached": breached}
                for name, total, breached in teams
            ],
            "degraded_signal": degraded,
        }
    }
    if overlay is not None:
        block["team_entity_overlay"] = overlay
    return block


def _jira(open_by_team=None, *, overlay=None):
    data = {}
    if open_by_team is not None:
        data["team_backlog"] = {"open_issues_by_team": open_by_team}
    if overlay is not None:
        data["team_entity_overlay"] = overlay
    return data


# A concentrated breach distribution: Commercial Credit owns most breaches.
_CONCENTRATED = [
    ("Commercial Credit", 80, 50),   # rate 0.625, breaches 50
    ("Retail Ops", 60, 10),
    ("Infrastructure", 40, 5),
]


# ─── AC6: SIGNAL_METRICS shape ────────────────────────────────────────────────

class TestSignalMetrics:
    def test_signal_metrics_exact_set(self):
        assert SIGNAL_METRICS == [
            "top_team_breach_pct",
            "top_team_breach_rate",
            "top_team_name",
            "org_breach_rate",
            "teams_analysed",
        ]

    def test_signal_metrics_max_eight(self):
        assert len(SIGNAL_METRICS) <= 8

    def test_numeric_metrics_numeric_and_in_raw_evidence(self):
        ev = evaluate(None, _sn(_CONCENTRATED), _jira())
        for metric in SIGNAL_METRICS:
            assert metric in ev.raw_evidence, f"{metric} missing from raw_evidence"
        # top_team_name is the documented string identifier; the rest are numeric.
        assert isinstance(ev.raw_evidence["top_team_name"], str)
        for metric in (
            "top_team_breach_pct",
            "top_team_breach_rate",
            "org_breach_rate",
            "teams_analysed",
        ):
            assert isinstance(ev.raw_evidence[metric], (int, float)), (
                f"{metric} must be numeric"
            )


# ─── AC4: fires when all three conditions met ─────────────────────────────────

class TestFires:
    def test_fires_when_concentrated(self):
        results = detect(None, _sn(_CONCENTRATED), _jira())
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == DETECTOR_ID
        assert r.signal_source == "servicenow"
        assert r.threshold == TOP_TEAM_BREACH_PCT_THRESHOLD
        assert r.raw_evidence["top_team_name"] == "Commercial Credit"
        assert r.raw_evidence["top_team_breach_pct"] == round(50 / 65, 4)
        assert r.raw_evidence["top_team_breach_rate"] == round(50 / 80, 4)
        assert r.raw_evidence["teams_analysed"] == 3
        assert r.metric_value == r.raw_evidence["top_team_breach_pct"]

    def test_fires_at_exact_thresholds(self):
        # A: pct exactly 0.40 (40/100), rate exactly 0.25 (40/160).
        teams = [
            ("Team A", 160, 40),
            ("Team B", 70, 35),
            ("Team C", 50, 25),
        ]
        ev = evaluate(None, _sn(teams), _jira())
        assert ev.raw_evidence["top_team_name"] == "Team A"
        assert ev.raw_evidence["top_team_breach_pct"] == 0.4
        assert ev.raw_evidence["top_team_breach_rate"] == 0.25
        assert ev.fired is True

    def test_fires_from_raw_incident_grouping(self):
        # Same concentration, but expressed as raw incidents grouped by the detector.
        incidents = []
        for name, total, breached in _CONCENTRATED:
            for i in range(total):
                incidents.append({"assignment_group": name, "sla_breached": i < breached})
        sn_data = {"sla_breach_by_team": {"incidents": incidents}}
        ev = evaluate(None, sn_data, _jira())
        assert ev.raw_evidence["teams_analysed"] == 3
        assert ev.raw_evidence["top_team_name"] == "Commercial Credit"
        assert ev.fired is True


# ─── AC5: confidence elevation ────────────────────────────────────────────────

class TestConfidenceElevation:
    def test_high_when_entity_resolved_and_high_jira_backlog(self):
        # Overlay resolves both the SN team and a differently-spelled Jira team
        # to the same entity; Jira open count (45) exceeds the high-backlog bar.
        overlay = {"Commercial Credit": "team:cc", "Comm Credit": "team:cc"}
        jira = _jira({"Comm Credit": 45}, overlay=overlay)
        ev = evaluate(None, _sn(_CONCENTRATED), jira)
        assert ev.fired is True
        assert ev.raw_evidence["team_entity_resolved"] is True
        assert ev.raw_evidence["match_strategy"] == "entity_graph"
        assert ev.raw_evidence["top_team_jira_open_issues"] == 45
        assert ev.raw_evidence["jira_corroborated"] is True
        assert ev.raw_evidence["confidence"] == CONFIDENCE_CORROBORATED

    def test_medium_when_no_overlay_even_with_high_exact_match(self):
        # Exact-name match with a high count must NOT elevate (degraded path).
        jira = _jira({"Commercial Credit": 99})
        ev = evaluate(None, _sn(_CONCENTRATED), jira)
        assert ev.raw_evidence["match_strategy"] == "exact_name"
        assert ev.raw_evidence["team_entity_resolved"] is False
        assert ev.raw_evidence["confidence"] == CONFIDENCE_STANDALONE

    def test_medium_when_entity_resolved_but_low_jira_backlog(self):
        overlay = {"Commercial Credit": "team:cc", "Comm Credit": "team:cc"}
        jira = _jira({"Comm Credit": 5}, overlay=overlay)  # below HIGH bar
        ev = evaluate(None, _sn(_CONCENTRATED), jira)
        assert ev.raw_evidence["team_entity_resolved"] is True
        assert ev.raw_evidence["jira_corroborated"] is False
        assert ev.raw_evidence["confidence"] == CONFIDENCE_STANDALONE

    def test_medium_standalone_when_no_jira(self):
        ev = evaluate(None, _sn(_CONCENTRATED), _jira())
        assert ev.raw_evidence["confidence"] == CONFIDENCE_STANDALONE
        assert ev.raw_evidence["match_strategy"] == "none"

    def test_corroborate_helper_threshold_boundary(self):
        overlay = {"Commercial Credit": "team:cc"}
        jira_hi = _jira({"Commercial Credit": HIGH_JIRA_OPEN_ISSUES}, overlay=overlay)
        out = corroborate_top_team("Commercial Credit", None, jira_hi)
        assert out["confidence"] == CONFIDENCE_CORROBORATED
        jira_lo = _jira({"Commercial Credit": HIGH_JIRA_OPEN_ISSUES - 1}, overlay=overlay)
        out_lo = corroborate_top_team("Commercial Credit", None, jira_lo)
        assert out_lo["confidence"] == CONFIDENCE_STANDALONE


# ─── AC4 guards: each firing condition independently ──────────────────────────

class TestDoesNotFire:
    def test_does_not_fire_when_breach_pct_below_threshold(self):
        # Breaches spread evenly so no team owns >= 40%.
        teams = [("A", 50, 20), ("B", 50, 18), ("C", 50, 17)]
        ev = evaluate(None, _sn(teams), _jira())
        assert ev.raw_evidence["top_team_breach_pct"] < TOP_TEAM_BREACH_PCT_THRESHOLD
        assert ev.fired is False

    def test_does_not_fire_when_breach_rate_below_threshold(self):
        # Top team owns most breaches but on a huge ticket base → low own rate.
        teams = [("A", 300, 50), ("B", 40, 5), ("C", 30, 3)]
        ev = evaluate(None, _sn(teams), _jira())
        assert ev.raw_evidence["top_team_breach_pct"] >= TOP_TEAM_BREACH_PCT_THRESHOLD
        assert ev.raw_evidence["top_team_breach_rate"] < TOP_TEAM_BREACH_RATE_THRESHOLD
        assert ev.fired is False

    def test_does_not_fire_when_too_few_teams(self):
        teams = [("A", 80, 50), ("B", 60, 10)]  # only 2 teams
        ev = evaluate(None, _sn(teams), _jira())
        assert ev.raw_evidence["teams_analysed"] == 2
        assert ev.fired is False

    def test_does_not_fire_when_degraded(self):
        ev = evaluate(None, _sn(_CONCENTRATED, degraded=True), _jira())
        assert ev.raw_evidence["degraded_signal"] is True
        assert ev.fired is False

    def test_does_not_fire_when_no_data(self):
        assert detect(None, {}, {}) == []
        assert detect(None, None, None) == []
        ev = evaluate(None, {"sla_breach_by_team": {"teams": []}}, {})
        assert ev.raw_evidence["teams_analysed"] == 0
        assert ev.fired is False

    def test_does_not_fire_when_no_breaches(self):
        teams = [("A", 50, 0), ("B", 50, 0), ("C", 50, 0)]
        ev = evaluate(None, _sn(teams), _jira())
        assert ev.raw_evidence["top_team_breach_pct"] == 0.0
        assert ev.fired is False


# ─── evaluate() contract ──────────────────────────────────────────────────────

class TestEvaluateContract:
    def test_org_breach_rate_computed(self):
        ev = evaluate(None, _sn(_CONCENTRATED), _jira())
        # 65 breaches / 180 tickets
        assert ev.raw_evidence["org_breach_rate"] == round(65 / 180, 4)

    def test_constants_wired(self):
        assert MIN_TEAMS_ANALYSED == 3
        assert TOP_TEAM_BREACH_PCT_THRESHOLD == 0.40
        assert TOP_TEAM_BREACH_RATE_THRESHOLD == 0.25