"""
backend/tests/contract/test_corroboration_engine.py

ENT-2 — Cross-System Confidence Elevation — Contract tests (T6).

25+ tests covering every corroboration rule (positive AND negative), the Slack
ceiling, the 30-day time window, triple corroboration, single-source behaviour,
the never-downgrade application rule, and non-blocking failure semantics.

Maps to ENT-2 acceptance criteria:
  AC1  — COVENANT_TRACKING_GAP + ServiceNow within 30d -> HIGH, 'ServiceNow' in sources
  AC2  — COVENANT_TRACKING_GAP + Jira within 30d       -> HIGH, 'Jira' in sources
  AC3  — both SN AND Jira -> triple_corroboration=True + exact triple label
  AC4  — Slack-only escalation -> stays MEDIUM, 'Slack (supporting only)' in sources
  AC5  — Slack + ServiceNow -> HIGH, rule_ids includes COR-01 and COR-06
  AC6  — ServiceNow incident 31+ days ago -> not corroborated, not elevated
  AC7  — single connector -> corroboration_sources empty (no badge)
  AC9  — rule_ids lists every rule that fired (auditable)
  AC10 — engine failure is non-blocking (caller keeps scorer confidence)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.corroboration_engine import (
    CORROBORATION_WINDOW_DAYS,
    CorroborationResult,
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor01_servicenow_team_incidents,
    check_cor02_jira_process_issues,
    check_cor04_confluence_doc_gap,
    check_cor05_slack_escalation,
    check_cor07_jira_sprint_velocity,
    check_cor08_single_source,
    evaluate_corroboration,
    normalise_connected_systems,
)
from discovery.packs.corroboration_rules import (
    CORROBORATION_RULES,
    CORROBORATION_WINDOW_DAYS as REGISTRY_WINDOW_DAYS,
    TRIPLE_CORROBORATION_LABEL,
    is_elevating_rule,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ─────────────────────────────────────────────────────────────────────────────

RUN_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
COVENANT = "COVENANT_TRACKING_GAP"


def _within_window_ts() -> str:
    """A timestamp comfortably inside the 30-day window (10 days before run)."""
    return (RUN_TS - timedelta(days=10)).isoformat()


def _outside_window_ts() -> str:
    """A timestamp outside the 30-day window (31 days before run)."""
    return (RUN_TS - timedelta(days=CORROBORATION_WINDOW_DAYS + 1)).isoformat()


def _future_ts() -> str:
    """A timestamp after the run timestamp; must not corroborate."""
    return (RUN_TS + timedelta(hours=1)).isoformat()


def _sn_incident(detector_id=COVENANT, ts=None, state="Open", team="lending-ops"):
    return {
        "detector_ids": [detector_id],
        "state": state,
        "team": team,
        "sys_created_on": ts or _within_window_ts(),
    }


def _jira_issue(detector_id=COVENANT, ts=None, status="Open", process="covenant"):
    return {
        "detector_ids": [detector_id],
        "status": status,
        "process": process,
        "created": ts or _within_window_ts(),
    }


def _run_data(
    *,
    systems=("salesforce", "servicenow", "jira"),
    servicenow_incidents=None,
    jira_issues=None,
    jira_sprint_velocity=None,
    confluence=None,
    slack=None,
):
    rd = {"connected_systems": list(systems)}
    if servicenow_incidents is not None:
        rd["servicenow"] = {"incidents": servicenow_incidents}
    if jira_issues is not None or jira_sprint_velocity is not None:
        rd["jira"] = {}
        if jira_issues is not None:
            rd["jira"]["issues"] = jira_issues
        if jira_sprint_velocity is not None:
            rd["jira"]["sprint_velocity"] = jira_sprint_velocity
    if confluence is not None:
        rd["confluence"] = confluence
    if slack is not None:
        rd["slack"] = slack
    return rd


# 2.0-B2 T6 (AC5): a cross-source elevation now requires a RESOLVED entity
# identity, so a test whose subject is the cross-source rule mechanics (COR-03
# derivation, the triple label) has to supply one — otherwise it would also be
# asserting the identity gate's behaviour, which has its own suites
# (tests/unit/test_corroboration_identity_gate.py and its contract twin).
# Single-source tests need nothing: the gate only engages when BOTH fired.
def _resolved_identity(_org, _left, _right):
    """An identity the graph has genuinely resolved (explicit cross-reference)."""
    return "explicit_reference"


def _evaluate(
    detector_id=COVENANT, identity_resolver=None, **run_data_kwargs
) -> CorroborationResult:
    return evaluate_corroboration(
        detector_id=detector_id,
        pack_id="ncino",
        run_data=_run_data(**run_data_kwargs),
        run_timestamp=RUN_TS,
        org_id="demo-org",
        identity_resolver=identity_resolver,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — ServiceNow corroboration elevates to HIGH
# ─────────────────────────────────────────────────────────────────────────────

class TestAC1ServiceNow:
    def test_servicenow_within_window_elevates_high(self):
        result = _evaluate(servicenow_incidents=[_sn_incident()])
        assert result.elevated_confidence == "HIGH"

    def test_servicenow_source_present(self):
        result = _evaluate(servicenow_incidents=[_sn_incident()])
        assert "ServiceNow" in result.corroboration_sources

    def test_cor01_in_rule_ids(self):
        result = _evaluate(servicenow_incidents=[_sn_incident()])
        assert "COR-01" in result.rule_ids

    def test_servicenow_only_is_not_triple(self):
        result = _evaluate(servicenow_incidents=[_sn_incident()])
        assert result.triple_corroboration is False


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Jira corroboration elevates to HIGH
# ─────────────────────────────────────────────────────────────────────────────

class TestAC2Jira:
    def test_jira_within_window_elevates_high(self):
        result = _evaluate(jira_issues=[_jira_issue()])
        assert result.elevated_confidence == "HIGH"

    def test_jira_source_present(self):
        result = _evaluate(jira_issues=[_jira_issue()])
        assert "Jira" in result.corroboration_sources

    def test_cor02_in_rule_ids(self):
        result = _evaluate(jira_issues=[_jira_issue()])
        assert "COR-02" in result.rule_ids


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Triple corroboration
# ─────────────────────────────────────────────────────────────────────────────

class TestAC3Triple:
    def test_both_sources_sets_triple(self):
        # T6/AC5: the triple headline is itself a cross-source identity claim, so
        # it stands only on a resolved identity (see _resolved_identity above).
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
            identity_resolver=_resolved_identity,
        )
        assert result.triple_corroboration is True

    def test_both_sources_without_a_resolved_identity_is_not_a_triple(self):
        # The T6 counterpart: two systems agreeing about entities nobody resolved
        # is not "all three agree".
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
        )
        assert result.triple_corroboration is False
        assert result.elevated_confidence == "MEDIUM"

    def test_triple_label_exact(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
            identity_resolver=_resolved_identity,
        )
        assert result.corroboration_label == TRIPLE_CORROBORATION_LABEL
        assert result.corroboration_label == (
            "Triple corroboration: Salesforce + ServiceNow + Jira"
        )

    def test_triple_includes_cor01_cor02_cor03(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
        )
        for rid in ("COR-01", "COR-02", "COR-03"):
            assert rid in result.rule_ids

    def test_triple_sources_has_both_systems(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
        )
        assert "ServiceNow" in result.corroboration_sources
        assert "Jira" in result.corroboration_sources

    def test_triple_elevates_high(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
            identity_resolver=_resolved_identity,
        )
        assert result.elevated_confidence == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Slack-only does NOT elevate (the Slack ceiling)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC4SlackOnly:
    def test_slack_only_stays_medium(self):
        result = _evaluate(slack={"escalation_pattern": {"fired": True}})
        assert result.elevated_confidence == "MEDIUM"

    def test_slack_only_supporting_source(self):
        result = _evaluate(slack={"escalation_pattern": {"fired": True}})
        assert "Slack (supporting only)" in result.corroboration_sources

    def test_slack_only_fires_cor05(self):
        result = _evaluate(slack={"escalation_pattern": {"fired": True}})
        assert "COR-05" in result.rule_ids

    def test_slack_only_not_confidence_elevated(self):
        result = _evaluate(slack={"escalation_pattern": {"fired": True}})
        assert result.confidence_elevated is False

    def test_cor05_is_not_an_elevating_rule(self):
        assert is_elevating_rule("COR-05") is False


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — Slack + ServiceNow elevates (COR-06)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC5SlackPlusServiceNow:
    def test_slack_plus_servicenow_elevates_high(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            slack={"escalation_pattern": {"fired": True}},
        )
        assert result.elevated_confidence == "HIGH"

    def test_rule_ids_include_cor01_and_cor06(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            slack={"escalation_pattern": {"fired": True}},
        )
        assert "COR-01" in result.rule_ids
        assert "COR-06" in result.rule_ids

    def test_no_cor05_when_primary_present(self):
        """When a primary corroborator exists, Slack contributes COR-06, not COR-05."""
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            slack={"escalation_pattern": {"fired": True}},
        )
        assert "COR-05" not in result.rule_ids

    def test_slack_escalation_source_present(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            slack={"escalation_pattern": {"fired": True}},
        )
        assert "Slack (escalation pattern)" in result.corroboration_sources


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — 30-day time window enforcement
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6TimeWindow:
    def test_servicenow_outside_window_does_not_corroborate(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident(ts=_outside_window_ts())]
        )
        assert "COR-01" not in result.rule_ids
        assert result.elevated_confidence == "MEDIUM"

    def test_jira_outside_window_does_not_corroborate(self):
        result = _evaluate(jira_issues=[_jira_issue(ts=_outside_window_ts())])
        assert "COR-02" not in result.rule_ids
        assert result.elevated_confidence == "MEDIUM"

    def test_servicenow_inside_window_corroborates(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident(ts=_within_window_ts())]
        )
        assert "COR-01" in result.rule_ids

    def test_window_constant_is_30(self):
        assert CORROBORATION_WINDOW_DAYS == 30

    def test_unparseable_timestamp_does_not_corroborate(self):
        """A record with a junk timestamp is treated as outside the window."""
        result = _evaluate(
            servicenow_incidents=[_sn_incident(ts="not-a-date")]
        )
        assert "COR-01" not in result.rule_ids

    def test_future_timestamp_does_not_corroborate(self):
        result = _evaluate(servicenow_incidents=[_sn_incident(ts=_future_ts())])
        assert "COR-01" not in result.rule_ids
        assert result.elevated_confidence == "MEDIUM"

    def test_check_function_window_boundary(self):
        """Direct check-function test for the window boundary."""
        run_data = _run_data(servicenow_incidents=[_sn_incident(ts=_outside_window_ts())])
        assert check_cor01_servicenow_team_incidents(COVENANT, run_data, RUN_TS) is False


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — Single source: no corroboration, empty sources (no badge)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC7SingleSource:
    def test_single_system_no_sources(self):
        result = _evaluate(
            systems=("salesforce",),
            servicenow_incidents=[_sn_incident()],  # present but irrelevant
        )
        assert result.corroboration_sources == []

    def test_single_system_no_elevation(self):
        result = _evaluate(systems=("salesforce",), servicenow_incidents=[_sn_incident()])
        assert result.elevated_confidence == "MEDIUM"
        assert result.triple_corroboration is False

    def test_single_system_no_label(self):
        result = _evaluate(systems=("salesforce",))
        assert result.corroboration_label is None

    def test_cor08_check_fires_for_single(self):
        assert check_cor08_single_source({"connected_systems": ["salesforce"]}) is True

    def test_cor08_check_false_for_multiple(self):
        assert check_cor08_single_source(
            {"connected_systems": ["salesforce", "servicenow"]}
        ) is False

    def test_zero_systems_no_corroboration(self):
        result = _evaluate(systems=())
        assert result.corroboration_sources == []


# ─────────────────────────────────────────────────────────────────────────────
# AC9 — rule_ids audit trail
# ─────────────────────────────────────────────────────────────────────────────

class TestAC9RuleIdsAudit:
    def test_every_fired_rule_recorded(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
            confluence={"covenant_documentation_present": False},
        )
        # COR-01 (SN), COR-02 (Jira), COR-03 (triple), COR-04 (confluence gap)
        for rid in ("COR-01", "COR-02", "COR-03", "COR-04"):
            assert rid in result.rule_ids

    def test_no_corroboration_empty_rule_ids(self):
        result = _evaluate(servicenow_incidents=[], jira_issues=[])
        assert result.rule_ids == []

    def test_all_rule_ids_are_registered(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident()],
            jira_issues=[_jira_issue()],
        )
        for rid in result.rule_ids:
            assert rid in CORROBORATION_RULES


# ─────────────────────────────────────────────────────────────────────────────
# AC10 — non-blocking failure + never-downgrade application
# ─────────────────────────────────────────────────────────────────────────────

class TestAC10NonBlockingAndApply:
    def test_apply_never_downgrades_high(self):
        """A scorer HIGH must never be downgraded by a MEDIUM corroboration verdict."""
        medium_result = CorroborationResult(elevated_confidence="MEDIUM")
        assert apply_corroboration_confidence("HIGH", medium_result) == "HIGH"

    def test_apply_elevates_medium_to_high(self):
        high_result = CorroborationResult(elevated_confidence="HIGH")
        assert apply_corroboration_confidence("MEDIUM", high_result) == "HIGH"

    def test_apply_elevates_low_to_high(self):
        high_result = CorroborationResult(elevated_confidence="HIGH")
        assert apply_corroboration_confidence("LOW", high_result) == "HIGH"

    def test_apply_keeps_high_when_both_high(self):
        high_result = CorroborationResult(elevated_confidence="HIGH")
        assert apply_corroboration_confidence("HIGH", high_result) == "HIGH"

    def test_apply_rejects_missing_scorer_confidence(self):
        high_result = CorroborationResult(elevated_confidence="HIGH")
        with pytest.raises(ValueError):
            apply_corroboration_confidence(None, high_result)

    def test_engine_does_not_raise_on_malformed_run_data(self):
        """Defensive parsing: junk run_data must not raise — returns a result."""
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data={"connected_systems": ["a", "b"], "servicenow": "not-a-dict"},
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert isinstance(result, CorroborationResult)

    def test_engine_handles_none_run_data(self):
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=None,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert isinstance(result, CorroborationResult)
        assert result.corroboration_sources == []

    def test_engine_handles_none_timestamp(self):
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=_run_data(servicenow_incidents=[_sn_incident()]),
            run_timestamp=None,
            org_id="demo-org",
        )
        assert isinstance(result, CorroborationResult)


# ─────────────────────────────────────────────────────────────────────────────
# COR-04 Confluence documentation gap
# ─────────────────────────────────────────────────────────────────────────────

class TestCOR04Confluence:
    def test_confluence_gap_fires_and_is_reported(self):
        """The rule fires, contributes its source chip, and is visible to a reader."""
        result = _evaluate(confluence={"covenant_documentation_present": False})
        assert "COR-04" in result.rule_ids
        assert "Confluence (no process documentation)" in result.corroboration_sources

    def test_confluence_gap_alone_does_not_elevate(self):
        """Documentation SUPPORTS, it does not LEAD.

        Confluence is `documentation_system` in every industry and template that
        lists it (ROLE_WEIGHT 0.6, below the 0.80 lead threshold), so a configured
        run has never let COR-04 elevate unaided. An unconfigured run used to,
        because the neutral fallback assumed weight 1.0 — so the same evidence gave
        HIGH or MEDIUM depending only on whether Stack Builder had been completed.

        The rule is now the same either way. It matters most here of all, because
        COR-04's evidence is an ABSENCE: "no runbook was found" is also exactly what
        a broken index looks like, so it is the last basis that should reach HIGH on
        its own.
        """
        result = _evaluate(confluence={"covenant_documentation_present": False})
        assert result.elevated_confidence == "MEDIUM"

    def test_confluence_gap_contributes_alongside_a_lead_source(self):
        """Supporting is not the same as ignored: with ServiceNow leading, the
        finding reaches HIGH and Confluence is still credited on it."""
        result = _evaluate(
            confluence={"covenant_documentation_present": False},
            servicenow_incidents=[_sn_incident()],
        )
        assert result.elevated_confidence == "HIGH"
        assert "COR-04" in result.rule_ids
        assert "Confluence (no process documentation)" in result.corroboration_sources

    def test_confluence_present_does_not_fire(self):
        result = _evaluate(confluence={"covenant_documentation_present": True})
        assert "COR-04" not in result.rule_ids

    def test_confluence_gap_only_for_covenant_detector(self):
        """COR-04 must not fire for a non-covenant detector."""
        result = evaluate_corroboration(
            detector_id="CHECKLIST_BOTTLENECK",
            pack_id="ncino",
            run_data=_run_data(confluence={"covenant_documentation_present": False}),
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-04" not in result.rule_ids

    def test_cor04_check_function_direct(self):
        rd = _run_data(confluence={"covenant_documentation_present": False})
        assert check_cor04_confluence_doc_gap(COVENANT, rd) is True
        assert check_cor04_confluence_doc_gap("OTHER", rd) is False


# ─────────────────────────────────────────────────────────────────────────────
# COR-07 Jira sprint velocity (loan origination)
# ─────────────────────────────────────────────────────────────────────────────

class TestCOR07SprintVelocity:
    def test_velocity_decline_elevates_for_loan_origination(self):
        result = evaluate_corroboration(
            detector_id="LOAN_ORIGINATION_BOTTLENECK",
            pack_id="ncino",
            run_data=_run_data(
                jira_sprint_velocity={"declined": True, "team": "eng", "timestamp": _within_window_ts()},
            ),
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-07" in result.rule_ids
        assert result.elevated_confidence == "HIGH"

    def test_velocity_not_declined_does_not_fire(self):
        result = evaluate_corroboration(
            detector_id="LOAN_ORIGINATION_BOTTLENECK",
            pack_id="ncino",
            run_data=_run_data(jira_sprint_velocity={"declined": False}),
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-07" not in result.rule_ids

    def test_velocity_only_for_loan_origination_detector(self):
        result = _evaluate(  # COVENANT detector
            jira_sprint_velocity={"declined": True, "timestamp": _within_window_ts()},
        )
        assert "COR-07" not in result.rule_ids

    def test_cor07_check_function_direct(self):
        rd = _run_data(jira_sprint_velocity={"declined": True, "timestamp": _within_window_ts()})
        assert check_cor07_jira_sprint_velocity("LOAN_ORIGINATION_BOTTLENECK", rd, RUN_TS) is True


# ─────────────────────────────────────────────────────────────────────────────
# Closed/resolved records do not corroborate
# ─────────────────────────────────────────────────────────────────────────────

class TestClosedRecords:
    def test_closed_servicenow_incident_does_not_corroborate(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident(state="Closed")]
        )
        assert "COR-01" not in result.rule_ids

    def test_resolved_jira_issue_does_not_corroborate(self):
        result = _evaluate(jira_issues=[_jira_issue(status="Resolved")])
        assert "COR-02" not in result.rule_ids


# ─────────────────────────────────────────────────────────────────────────────
# Detector linkage filtering
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectorLinkage:
    def test_incident_linked_to_other_detector_does_not_corroborate(self):
        result = _evaluate(
            servicenow_incidents=[_sn_incident(detector_id="SOME_OTHER_DETECTOR")]
        )
        assert "COR-01" not in result.rule_ids

    def test_incident_without_detector_ids_is_treated_as_relevant(self):
        """A record with no detector_ids is a pre-filtered relevant record."""
        run_data = _run_data(
            servicenow_incidents=[{"state": "Open", "sys_created_on": _within_window_ts()}]
        )
        assert check_cor01_servicenow_team_incidents(COVENANT, run_data, RUN_TS) is True


# ─────────────────────────────────────────────────────────────────────────────
# Rule registry integrity (auditability — T2)
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleRegistry:
    def test_all_rules_defined(self):
        # R17-A3 added COR-09 (Java operational corroboration); R17-A4 added COR-10
        # (its .NET counterpart). COR-11/COR-12 are the documentation
        # cross-reference rules — the first rules to read the markers Confluence and
        # SharePoint have always extracted, and COR-12 is the first SharePoint rule.
        assert len(CORROBORATION_RULES) == 12
        for i in range(1, 13):
            assert f"COR-{i:02d}" in CORROBORATION_RULES

    def test_slack_only_and_single_source_never_elevate(self):
        assert is_elevating_rule("COR-05") is False
        assert is_elevating_rule("COR-08") is False

    def test_primary_rules_elevate(self):
        # COR-09 (Java-app) and COR-10 (.NET-app) operational friction are
        # first-class observed evidence and elevate like the system-of-record
        # corroborators (R17-A3 §3 / R17-A4 §3).
        for rid in ("COR-01", "COR-02", "COR-03", "COR-04", "COR-06", "COR-07", "COR-09", "COR-10"):
            assert is_elevating_rule(rid) is True

    def test_every_rule_has_description_and_target(self):
        for rule in CORROBORATION_RULES.values():
            assert rule.description
            assert rule.elevation_target in ("MEDIUM", "HIGH")

    def test_window_days_exported_from_rule_registry(self):
        assert REGISTRY_WINDOW_DAYS == 30


# ─────────────────────────────────────────────────────────────────────────────
# Result shape always predictable
# ─────────────────────────────────────────────────────────────────────────────

class TestResultShape:
    def test_no_corroboration_returns_full_result(self):
        result = _evaluate(servicenow_incidents=[], jira_issues=[])
        assert isinstance(result, CorroborationResult)
        assert result.rule_ids == []
        assert result.corroboration_sources == []
        assert result.triple_corroboration is False
        assert result.elevated_confidence == "MEDIUM"
        assert result.original_confidence == "MEDIUM"

    def test_check_cor02_excludes_velocity_only_signal(self):
        """A Jira run with only sprint_velocity (no issues) does not fire COR-02."""
        run_data = _run_data(jira_sprint_velocity={"declined": True})
        assert check_cor02_jira_process_issues(COVENANT, run_data, RUN_TS) is False

    def test_check_cor05_slack_flag_shape(self):
        """Alternative slack shape: boolean flag instead of nested dict."""
        run_data = _run_data(slack={"escalation_pattern_fired": True})
        assert check_cor05_slack_escalation(run_data, RUN_TS) is True


# ──────────────────────────────────────────────────────────────────────────────
# Runner wiring: Slack/Confluence blocks are carried into the shared engine
# ──────────────────────────────────────────────────────────────────────────────

class TestRunnerCorroborationWiring:
    def test_normalises_combined_connector_ids_for_corroboration(self):
        systems = normalise_connected_systems(["salesforce_ncino", "jira_confluence"])
        assert "salesforce" in systems
        assert "salesforce_ncino" not in systems
        assert "jira" in systems
        assert "confluence" in systems

    def test_runner_data_builder_passes_confluence_gap_to_engine(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "confluence"},
            sn_by_detector={},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
            source_payloads=[
                {
                    "corroboration": {
                        "confluence": {"covenant_documentation_present": False},
                    },
                },
            ],
        )

        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )

        assert "COR-04" in result.rule_ids
        assert "Confluence (no process documentation)" in result.corroboration_sources
        # The rule fires and is reported, but documentation does not LEAD — see
        # TestCOR04Confluence.test_confluence_gap_alone_does_not_elevate.
        assert result.elevated_confidence == "MEDIUM"

    def test_runner_data_builder_passes_slack_only_without_elevation(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "slack"},
            sn_by_detector={},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
            source_payloads=[
                {
                    "slack": {
                        "escalation_pattern": {
                            "fired": True,
                            "timestamp": RUN_TS.isoformat(),
                        },
                    },
                },
            ],
        )

        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )

        assert "COR-05" in result.rule_ids
        assert "Slack (supporting only)" in result.corroboration_sources
        assert result.elevated_confidence == "MEDIUM"
        assert result.confidence_elevated is False

    def test_runner_data_builder_combines_slack_with_servicenow_for_high(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "servicenow", "slack"},
            sn_by_detector={COVENANT: [{"sys_created_on": _within_window_ts()}]},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
            source_payloads=[
                {
                    "cross_system_evidence": {
                        "slack": {"escalation_pattern_fired": True},
                    },
                },
            ],
        )

        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )

        assert "COR-01" in result.rule_ids
        assert "COR-06" in result.rule_ids
        assert "Slack (escalation pattern)" in result.corroboration_sources
        assert result.elevated_confidence == "HIGH"

    def test_runner_data_builder_supports_triple_corroboration(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "servicenow", "jira"},
            # T6/AC5: the records carry the entity each side is about (as real
            # corroboration records do), because an elevation now needs an identity
            # to resolve — a record naming nothing cannot establish one.
            sn_by_detector={
                COVENANT: [{"sys_created_on": _within_window_ts(), "team": "lending-ops"}]
            },
            jira_by_detector={
                COVENANT: [{"created": _within_window_ts(), "process": "lending-ops"}]
            },
            run_timestamp_iso=RUN_TS.isoformat(),
        )

        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
            # T6/AC5: the builder's job is the run_data shape; the triple it
            # supports needs a resolved identity to elevate.
            identity_resolver=_resolved_identity,
        )

        assert result.triple_corroboration is True
        assert result.corroboration_label == TRIPLE_CORROBORATION_LABEL
        assert "ServiceNow" in result.corroboration_sources
        assert "Jira" in result.corroboration_sources
        assert result.elevated_confidence == "HIGH"

    def test_builder_uses_real_stale_servicenow_timestamp(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "servicenow"},
            sn_by_detector={COVENANT: ["snippet one", "snippet two"]},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
            source_payloads=[
                {
                    "lending_correlation": {
                        "lending_incidents": [
                            {
                                "detector_id": COVENANT,
                                "state": "Open",
                                "sys_created_on": _outside_window_ts(),
                                "snippet": "stale incident",
                            }
                        ]
                    }
                }
            ],
        )

        incidents = run_data["servicenow"]["incidents"]
        assert len(incidents) == 1
        assert incidents[0]["sys_created_on"] == _outside_window_ts()
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-01" not in result.rule_ids
        assert result.elevated_confidence == "MEDIUM"

    def test_builder_creates_one_placeholder_per_detector_not_per_snippet(self):
        run_data = build_corroboration_run_data(
            systems={"salesforce", "servicenow"},
            sn_by_detector={COVENANT: ["snippet one", "snippet two", "snippet three"]},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
        )

        incidents = run_data["servicenow"]["incidents"]
        assert len(incidents) == 1
        assert "sys_created_on" not in incidents[0]
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-01" not in result.rule_ids

    def test_builder_does_not_inflate_connected_systems_from_incidental_slack(self):
        run_data = build_corroboration_run_data(
            systems={"servicenow"},
            sn_by_detector={COVENANT: [{"sys_created_on": _within_window_ts()}]},
            jira_by_detector={},
            run_timestamp_iso=RUN_TS.isoformat(),
            source_payloads=[{"slack": {"escalation_pattern_fired": True}}],
        )

        assert run_data["connected_systems"] == ["servicenow"]
        assert "slack" not in run_data
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert result.corroboration_sources == []
        assert result.elevated_confidence == "MEDIUM"

    def test_builder_warns_for_connected_confluence_without_recognized_block(self, caplog):
        with caplog.at_level("WARNING"):
            run_data = build_corroboration_run_data(
                systems={"salesforce", "confluence"},
                sn_by_detector={},
                jira_by_detector={},
                run_timestamp_iso=RUN_TS.isoformat(),
                source_payloads=[{"confluence": "unexpected-format"}],
            )

        assert "confluence" not in run_data
        assert "Confluence is connected but no recognized" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# COR-11 / COR-12 — documentation that CITES the corroborating record
#
# Both connectors have always extracted cross_references (ServiceNow/Jira/GitHub
# markers from Confluence page titles and SharePoint file names) and nothing ever
# read them. These rules read them.
#
# Note what is deliberately NOT a rule: "documentation exists about this process".
# An approvals page is no evidence that approvals are slow, and nearly every org
# has documentation about nearly every process — such a rule would fire on almost
# everything and mean almost nothing. An EXPLICIT id match is different in kind.
# ─────────────────────────────────────────────────────────────────────────────

def _doc_run_data(*, confluence_refs=None, sharepoint_refs=None, incident_number="INC-4821"):
    rd = _run_data(
        systems=("salesforce", "servicenow", "confluence", "sharepoint"),
        servicenow_incidents=[{**_sn_incident(), "number": incident_number}],
    )
    if confluence_refs is not None:
        rd["confluence"] = {"cross_references": confluence_refs}
    if sharepoint_refs is not None:
        rd["sharepoint"] = {"cross_references": sharepoint_refs}
    return rd


def _evaluate_doc(**kw):
    return evaluate_corroboration(
        detector_id=COVENANT,
        pack_id="ncino",
        run_data=_doc_run_data(**kw),
        run_timestamp=RUN_TS,
        org_id="demo-org",
    )


class TestCOR11And12DocumentationCrossReference:
    def test_confluence_reference_to_a_linked_incident_fires(self):
        result = _evaluate_doc(
            confluence_refs=[{"system": "servicenow", "ref": "INC-4821"}]
        )
        assert "COR-11" in result.rule_ids
        assert "Confluence (references the same record)" in result.corroboration_sources

    def test_sharepoint_reference_to_a_linked_incident_fires(self):
        result = _evaluate_doc(
            sharepoint_refs=[{"system": "servicenow", "ref": "INC-4821"}]
        )
        assert "COR-12" in result.rule_ids
        assert "SharePoint (references the same record)" in result.corroboration_sources

    def test_reference_matching_is_case_and_separator_insensitive(self):
        """INC-4821, inc4821 and 'INC 4821' are one incident written three ways."""
        for ref in ("inc4821", "INC 4821", "inc-4821"):
            result = _evaluate_doc(confluence_refs=[{"system": "servicenow", "ref": ref}])
            assert "COR-11" in result.rule_ids, ref

    def test_a_near_miss_reference_does_not_corroborate(self):
        """Nothing fuzzier than normalisation: INC-48 must not match INC-4821."""
        result = _evaluate_doc(confluence_refs=[{"system": "servicenow", "ref": "INC-48"}])
        assert "COR-11" not in result.rule_ids

    def test_reference_to_an_unrelated_record_does_not_corroborate(self):
        result = _evaluate_doc(confluence_refs=[{"system": "servicenow", "ref": "INC-9999"}])
        assert "COR-11" not in result.rule_ids

    def test_no_references_does_not_fire(self):
        result = _evaluate_doc(confluence_refs=[])
        assert "COR-11" not in result.rule_ids

    def test_documentation_alone_cannot_reach_high(self):
        """The role-weight ceiling, applied to the new rules exactly as to COR-04.

        Reaching documentation-only takes a little setting up, because COR-11 needs
        a linked record to match against and that record usually fires COR-01 too.
        The realistic case is an incident OUTSIDE the 30-day corroboration window:
        ServiceNow no longer corroborates, but the page citing it is still there.
        Documentation then stands alone — and stays MEDIUM.
        """
        run_data = _run_data(
            systems=("salesforce", "servicenow", "confluence"),
            servicenow_incidents=[
                {**_sn_incident(ts=_outside_window_ts()), "number": "INC-4821"}
            ],
            confluence={"cross_references": [{"system": "servicenow", "ref": "INC-4821"}]},
        )
        result = evaluate_corroboration(
            detector_id=COVENANT,
            pack_id="ncino",
            run_data=run_data,
            run_timestamp=RUN_TS,
            org_id="demo-org",
        )
        assert "COR-01" not in result.rule_ids     # stale incident: no lead source
        assert "COR-11" in result.rule_ids         # the citation still corroborates
        assert result.elevated_confidence == "MEDIUM"

    def test_documentation_is_credited_on_an_elevation_another_source_leads(self):
        result = _evaluate_doc(
            confluence_refs=[{"system": "servicenow", "ref": "INC-4821"}]
        )
        # ServiceNow (COR-01) leads; the documentation rule rides along and is shown.
        assert result.elevated_confidence == "HIGH"
        assert "COR-01" in result.rule_ids
        assert "COR-11" in result.rule_ids
        assert "Confluence (references the same record)" in result.corroboration_sources

    def test_new_rules_are_registered_with_their_systems_and_labels(self):
        from app.corroboration_engine import _CORROBORATION_RULE_SYSTEMS
        from discovery.packs.corroboration_rules import (
            CORROBORATION_RULES,
            RULE_CARD_LABELS,
        )

        for rule_id, system in (("COR-11", "confluence"), ("COR-12", "sharepoint")):
            assert rule_id in CORROBORATION_RULES
            assert RULE_CARD_LABELS.get(rule_id)
            assert _CORROBORATION_RULE_SYSTEMS.get(rule_id) == system
