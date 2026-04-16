"""
SF-2.5 tests — All seven detector implementations.
Grounded in SF-1.3 dev org values. These are the official unit tests.
"""
from __future__ import annotations
import json
import pytest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sf_std():   return load("synthetic_org_standard.json")
@pytest.fixture
def sf_edge():  return load("synthetic_org_edge_cases.json")
@pytest.fixture
def sn_std():   return load("sn_standard.json")
@pytest.fixture
def sn_none():  return load("sn_no_signal.json")
@pytest.fixture
def jira_std(): return load("jira_standard.json")


# ─── D1: REPETITIVE_AUTOMATION ───────────────────────────────────────────────

class TestD1RepetitiveAutomation:
    def test_fires_on_standard_org(self, sf_std):
        from discovery.detectors.repetition import detect
        results = detect(sf_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "REPETITIVE_AUTOMATION"
        assert r.signal_source == "salesforce"
        assert r.metric_value == 2.128
        assert r.threshold == 0.6
        assert r.raw_evidence["flow_activity_score"] == 2.128
        assert r.raw_evidence["records_90d"] == 300

    def test_does_not_fire_below_threshold(self, sf_std):
        from discovery.detectors.repetition import detect
        sf = {**sf_std, "flow_inventory": {**sf_std["flow_inventory"], "flow_activity_score": 0.5}}
        assert detect(sf) == []

    def test_does_not_fire_high_complexity_flows(self, sf_std):
        from discovery.detectors.repetition import detect
        # All flows > 15 elements = not LOW complexity
        sf = dict(sf_std)
        sf["flow_inventory"] = dict(sf_std["flow_inventory"])
        sf["flow_inventory"]["flows"] = [
            {"flow_id": "x", "flow_label": "Complex", "process_type": "AutoLaunchedFlow",
             "element_count": 20, "trigger_object": "Case"}
        ]
        sf["flow_inventory"]["flow_activity_score"] = 2.0
        assert detect(sf) == []

    def test_does_not_fire_insufficient_volume(self, sf_std):
        from discovery.detectors.repetition import detect
        sf = dict(sf_std)
        sf["case_metrics"] = {**sf_std["case_metrics"], "total_cases_90d": 40}
        assert detect(sf) == []

    def test_borderline_fires(self, sf_edge):
        from discovery.detectors.repetition import detect
        # flow_activity_score=0.61, records=45 < 50 MIN_VOLUME → should NOT fire
        assert detect(sf_edge) == []


# ─── D2: HANDOFF_FRICTION ─────────────────────────────────────────────────────

class TestD2HandoffFriction:
    def test_fires_on_standard_org(self, sf_std):
        from discovery.detectors.handoff_friction import detect
        results = detect(sf_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "HANDOFF_FRICTION"
        assert r.metric_value == 1.6
        assert r.threshold == 1.5
        assert r.raw_evidence["owner_changes_90d"] == 480
        assert r.raw_evidence["total_cases_90d"] == 300

    def test_does_not_fire_at_threshold(self, sf_std):
        from discovery.detectors.handoff_friction import detect
        sf = {**sf_std, "case_metrics": {**sf_std["case_metrics"], "handoff_score": 1.5}}
        assert detect(sf) == []

    def test_does_not_fire_low_volume(self, sf_std):
        from discovery.detectors.handoff_friction import detect
        sf = {**sf_std, "case_metrics": {**sf_std["case_metrics"], "total_cases_90d": 49}}
        assert detect(sf) == []

    def test_top_categories_included(self, sf_std):
        from discovery.detectors.handoff_friction import detect
        results = detect(sf_std)
        # category_breakdown has one entry with handoff_score 4.0 > 1.5
        cats = results[0].raw_evidence["top_categories"]
        assert len(cats) >= 1
        assert cats[0]["handoff_score"] > 1.5


# ─── D3: APPROVAL_BOTTLENECK ─────────────────────────────────────────────────

class TestD3ApprovalBottleneck:
    def test_fires_combined_condition(self, sf_std):
        from discovery.detectors.approval_delay import detect
        results = detect(sf_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "APPROVAL_BOTTLENECK"
        assert r.metric_value == 5.0   # avg_delay_days
        assert r.threshold == 3.0
        assert r.raw_evidence["process_name"] == "Discount Approval"
        assert r.raw_evidence["bottleneck_score"] == 30.0

    def test_fires_severe_delay_alone(self, sf_std):
        from discovery.detectors.approval_delay import detect
        sf = dict(sf_std)
        sf["approval_processes"] = [{
            "process_name": "Severe", "pending_count": 3,
            "avg_delay_days": 8.0, "approver_count": 5,
            "bottleneck_score": 0.6  # below bottleneck threshold
        }]
        results = detect(sf)
        assert len(results) == 1  # fires on severe delay alone

    def test_does_not_fire_low_delay_low_bottleneck(self, sf_edge):
        from discovery.detectors.approval_delay import detect
        # edge: Low Bottleneck Process — delay=2.0, b_score=2.5 — neither fires
        results = detect(sf_edge)
        # Only Severe Delay Process (delay=8.0) should fire
        assert all(r.raw_evidence["process_name"] == "Severe Delay Process" for r in results)

    def test_multiple_processes_independent(self, sf_std):
        from discovery.detectors.approval_delay import detect
        sf = dict(sf_std)
        sf["approval_processes"] = [
            {"process_name": "Proc A", "pending_count": 20, "avg_delay_days": 4.0, "approver_count": 1, "bottleneck_score": 20.0},
            {"process_name": "Proc B", "pending_count": 5, "avg_delay_days": 1.0, "approver_count": 3, "bottleneck_score": 1.7},
        ]
        results = detect(sf)
        assert len(results) == 1
        assert results[0].raw_evidence["process_name"] == "Proc A"


# ─── D4: KNOWLEDGE_GAP ───────────────────────────────────────────────────────

class TestD4KnowledgeGap:
    def test_fires_on_standard_org(self, sf_std):
        from discovery.detectors.knowledge_gap import detect
        results = detect(sf_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "KNOWLEDGE_GAP"
        assert r.metric_value == 0.5
        assert r.threshold == 0.4
        assert r.raw_evidence["closed_cases_90d"] == 60
        assert r.raw_evidence["cases_with_kb_link"] == 30

    def test_does_not_fire_at_threshold(self, sf_std):
        from discovery.detectors.knowledge_gap import detect
        sf = {**sf_std, "case_metrics": {**sf_std["case_metrics"], "knowledge_gap_score": 0.40}}
        assert detect(sf) == []

    def test_does_not_fire_insufficient_closed_cases(self, sf_std):
        from discovery.detectors.knowledge_gap import detect
        sf = {**sf_std, "case_metrics": {**sf_std["case_metrics"], "closed_cases_90d": 29}}
        assert detect(sf) == []

    def test_does_not_fire_edge_low_volume(self, sf_edge):
        from discovery.detectors.knowledge_gap import detect
        # edge: closed=10 < 30 MIN_CLOSED
        assert detect(sf_edge) == []


# ─── D5: INTEGRATION_CONCENTRATION ───────────────────────────────────────────

class TestD5IntegrationConcentration:
    def test_fires_on_standard_org(self, sf_std):
        from discovery.detectors.integration_concentration import detect
        results = detect(sf_std)
        # ServiceNow cred has flow_reference_count=3 >= threshold
        assert len(results) >= 1
        fired_ids = [r.raw_evidence["credential_developer_name"] for r in results]
        assert "ServiceNow_Integration" in fired_ids

    def test_does_not_fire_below_threshold(self, sf_std):
        from discovery.detectors.integration_concentration import detect
        sf = dict(sf_std)
        sf["named_credentials"] = [
            {"credential_name": "Low", "credential_developer_name": "Low_Cred",
             "flow_reference_count": 2, "referencing_flow_ids": ["a","b"], "match_type": "none"}
        ]
        assert detect(sf) == []

    def test_does_not_fire_edge_cred(self, sf_edge):
        from discovery.detectors.integration_concentration import detect
        # edge: flow_reference_count=2 < 3
        assert detect(sf_edge) == []

    def test_metric_value_is_ref_count(self, sf_std):
        from discovery.detectors.integration_concentration import detect
        results = detect(sf_std)
        sn_result = next(r for r in results if r.raw_evidence["credential_developer_name"] == "ServiceNow_Integration")
        assert sn_result.metric_value == 3.0


# ─── D6: PERMISSION_BOTTLENECK ───────────────────────────────────────────────

class TestD6PermissionBottleneck:
    def test_fires_on_standard_org(self, sf_std):
        from discovery.detectors.permission_bottleneck import detect
        results = detect(sf_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "PERMISSION_BOTTLENECK"
        assert r.metric_value == 30.0
        assert r.threshold == 10.0
        assert r.raw_evidence["approver_count"] == 2
        assert r.raw_evidence["pending_count"] == 60

    def test_fires_independently_of_d3(self, sf_std):
        """D6 fires on concentration alone regardless of delay."""
        from discovery.detectors.permission_bottleneck import detect
        sf = dict(sf_std)
        sf["approval_processes"] = [{
            "process_name": "High Concentration No Delay",
            "pending_count": 50, "avg_delay_days": 1.0,
            "approver_count": 2, "bottleneck_score": 25.0
        }]
        results = detect(sf)
        assert len(results) == 1

    def test_does_not_fire_zero_approvers(self, sf_std):
        from discovery.detectors.permission_bottleneck import detect
        sf = dict(sf_std)
        sf["approval_processes"] = [{
            "process_name": "No Approvers", "pending_count": 100,
            "avg_delay_days": 5.0, "approver_count": 0, "bottleneck_score": 0.0
        }]
        assert detect(sf) == []

    def test_does_not_fire_low_bottleneck(self, sf_edge):
        from discovery.detectors.permission_bottleneck import detect
        # edge: Low Bottleneck Process b_score=2.5, Severe Delay b_score=3.0 — both below 10
        assert detect(sf_edge) == []


# ─── D7: CROSS_SYSTEM_ECHO ───────────────────────────────────────────────────

class TestD7CrossSystemEcho:
    def test_fires_sf_side(self, sf_std, sn_none, jira_std):
        from discovery.detectors.cross_system_echo import detect
        # sf_echo_score=0.25 > 0.15, sf_total_cases=300 > 30
        results = detect(sf_std, sn_none, jira_std)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == "CROSS_SYSTEM_ECHO"
        assert r.metric_value == 0.25
        assert r.threshold == 0.15

    def test_fires_sn_side(self, sf_std, sn_std, jira_std):
        from discovery.detectors.cross_system_echo import detect
        # Both SF (0.25) and SN (0.16) exceed threshold
        results = detect(sf_std, sn_std, jira_std)
        assert len(results) == 1
        # Dominant = SF (highest score 0.25)
        assert results[0].metric_value == 0.25
        # Evidence should have merged SN data too
        ev = results[0].raw_evidence
        assert ev["sn_echo_score"] == 0.16

    def test_fires_all_three_systems(self, sf_std, sn_std, jira_std):
        from discovery.detectors.cross_system_echo import detect
        results = detect(sf_std, sn_std, jira_std)
        ev = results[0].raw_evidence
        assert ev["sf_echo_score"] > 0
        assert ev["sn_echo_score"] > 0
        assert "jira_echo_score" in ev

    def test_does_not_fire_all_below_threshold(self, sf_edge, sn_none):
        from discovery.detectors.cross_system_echo import detect
        # sf_echo_score=0.11 < 0.15, sn=0.0
        assert detect(sf_edge, sn_none, {}) == []

    def test_does_not_fire_insufficient_volume(self):
        from discovery.detectors.cross_system_echo import detect
        sf = {"cross_system_references": {
            "sf_echo_count": 5, "sf_total_cases": 20,  # < 30 MIN_VOLUME
            "sf_echo_score": 0.25, "matched_patterns": ["INC-"]
        }, "case_metrics": {"total_cases_90d": 20}}
        assert detect(sf) == []

    def test_no_sn_or_jira_still_fires_on_sf(self, sf_std):
        from discovery.detectors.cross_system_echo import detect
        results = detect(sf_std, None, None)
        assert len(results) == 1

    def test_matched_patterns_merged(self, sf_std, sn_std, jira_std):
        from discovery.detectors.cross_system_echo import detect
        results = detect(sf_std, sn_std, jira_std)
        patterns = results[0].raw_evidence["matched_patterns"]
        assert len(patterns) >= 1
