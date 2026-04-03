import json
from pathlib import Path

from backend.discovery.detectors import (
    repetition, handoff_friction, approval_delay, knowledge_gap,
    integration_concentration, permission_bottleneck, cross_system_echo
)

FX = Path(__file__).parent / "fixtures"

def load(name: str):
    return json.loads((FX / name).read_text(encoding="utf-8"))

def test_repetition():
    data = load("synthetic_org_standard.json")
    res = repetition.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "REPETITIVE_AUTOMATION"
    assert r.signal_source == "Salesforce.FlowVersionView"
    assert r.metric_name == "flow_activity_score"
    assert r.metric_value > 0.6
    assert r.raw_evidence["flowName"] == "Auto-set status"

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_flow_inventory"][0]["records_per_day_proxy"] = 0.1
    assert repetition.detect(data2) == []

    assert repetition.detect(load("synthetic_org_edge_cases.json")) == []

def test_handoff_friction():
    data = load("synthetic_org_standard.json")
    res = handoff_friction.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "HANDOFF_FRICTION"
    assert r.signal_source == "Salesforce.CaseHistory"
    assert r.metric_name == "avg_reassignments"
    assert abs(r.metric_value - 2.8) < 1e-9
    assert r.raw_evidence["category"] == "Technical Support"
    assert int(r.raw_evidence["volume"]) == 847

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_case_metrics"][0]["avg_reassignments"] = 1.0
    assert handoff_friction.detect(data2) == []
    assert handoff_friction.detect(load("synthetic_org_edge_cases.json")) == []

def test_approval_delay():
    data = load("synthetic_org_standard.json")
    res = approval_delay.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "APPROVAL_BOTTLENECK"
    assert r.signal_source == "Salesforce.ProcessInstance"
    assert r.metric_name == "avg_pending_age_days"
    assert r.metric_value > 3.0
    assert r.raw_evidence["step"] == "Legal Review"

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_approval_pending"][0]["avg_age_days"] = 2.0
    assert approval_delay.detect(data2) == []
    assert approval_delay.detect(load("synthetic_org_edge_cases.json")) == []

def test_knowledge_gap():
    data = load("synthetic_org_standard.json")
    res = knowledge_gap.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "KNOWLEDGE_GAP"
    assert r.signal_source == "Salesforce.CaseArticle"
    assert r.metric_name == "knowledge_gap_score"
    assert r.metric_value > 0.4
    assert r.raw_evidence["category"] == "Billing"

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_knowledge_coverage"][0]["with_kb_count"] = 480
    assert knowledge_gap.detect(data2) == []
    assert knowledge_gap.detect(load("synthetic_org_edge_cases.json")) == []

def test_integration_concentration():
    data = load("synthetic_org_standard.json")
    res = integration_concentration.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "INTEGRATION_CONCENTRATION"
    assert r.signal_source == "Salesforce.NamedCredential"
    assert r.metric_name == "flow_reference_count"
    assert int(r.metric_value) == 6
    assert r.raw_evidence["named_credential"] == "SAP_ERP"

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_named_credentials"][0]["flow_reference_count"] = 2
    assert integration_concentration.detect(data2) == []
    assert integration_concentration.detect(load("synthetic_org_edge_cases.json")) == []

def test_permission_bottleneck():
    data = load("synthetic_org_standard.json")
    res = permission_bottleneck.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "PERMISSION_BOTTLENECK"
    assert r.signal_source == "Salesforce.ProcessNode"
    assert r.metric_name == "bottleneck_score"
    assert r.metric_value > 10.0
    assert r.raw_evidence["step_name"] == "Legal Review"

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_permission_bottlenecks"][0]["bottleneck_score"] = 8
    assert permission_bottleneck.detect(data2) == []
    assert permission_bottleneck.detect(load("synthetic_org_edge_cases.json")) == []

def test_cross_system_echo():
    data = load("synthetic_org_standard.json")
    res = cross_system_echo.detect(data)
    assert len(res) == 1
    r = res[0]
    assert r.detector_id == "CROSS_SYSTEM_ECHO"
    assert r.signal_source == "Salesforce.Case"
    assert r.metric_name == "echo_score"
    assert r.metric_value > 0.15
    assert int(r.raw_evidence["match_count"]) == 198

    data2 = load("synthetic_org_standard.json")
    data2["salesforce_cross_system_references"]["echo_score"] = 0.01
    assert cross_system_echo.detect(data2) == []
    assert cross_system_echo.detect(load("synthetic_org_edge_cases.json")) == []
