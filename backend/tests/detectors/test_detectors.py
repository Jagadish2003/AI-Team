import pytest
from discovery.detectors import (
    approval_delay,
    cross_system_echo,
    handoff_friction,
    integration_concentration,
    knowledge_gap,
    permission_bottleneck,
    repetition
)

# ==========================================
# 1. APPROVAL DELAY (Threshold: Age > 3.0)
# ==========================================
def test_approval_delay_fires():
    data = {"salesforce_approval_pending": [{"process_name": "P1", "step_name": "Legal", "pending_count": 5, "avg_age_days": 4.5}]}
    res = approval_delay.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 4.5
    assert res[0].raw_evidence["step"] == "Legal" # DoD: raw_evidence populated

def test_approval_delay_does_not_fire():
    data = {"salesforce_approval_pending": [{"process_name": "P1", "step_name": "Legal", "pending_count": 5, "avg_age_days": 2.0}]}
    assert approval_delay.detect(data) == []

def test_approval_delay_empty_input():
    assert approval_delay.detect({}) == [] # DoD: Edge-case handles empty input

# ==========================================
# 2. CROSS SYSTEM ECHO (Threshold: Score > 0.15)
# ==========================================
def test_cross_system_echo_fires():
    data = {
        "salesforce_cross_system_references": {"match_count": 200, "total_count": 1000, "echo_score": 0.20},
        "servicenow_cross_system_references": {"echo_score": 0.10}
    }
    res = cross_system_echo.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 0.20
    assert "match_count" in res[0].raw_evidence

def test_cross_system_echo_does_not_fire():
    data = {"salesforce_cross_system_references": {"match_count": 100, "total_count": 1000, "echo_score": 0.10}}
    assert cross_system_echo.detect(data) == []

def test_cross_system_echo_empty_input():
    assert cross_system_echo.detect({}) == []

# ==========================================
# 3. HANDOFF FRICTION (Threshold: Reassignments > 1.5, Vol >= 50)
# ==========================================
def test_handoff_friction_fires():
    data = {"salesforce_case_metrics": [{"category": "IT", "volume": 100, "avg_reassignments": 2.5}]}
    res = handoff_friction.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 2.5
    assert res[0].raw_evidence["category"] == "IT"

def test_handoff_friction_does_not_fire():
    # High volume but low reassignments
    data = {"salesforce_case_metrics": [{"category": "IT", "volume": 100, "avg_reassignments": 1.0}]}
    assert handoff_friction.detect(data) == []

def test_handoff_friction_empty_input():
    assert handoff_friction.detect({}) == []

# ==========================================
# 4. INTEGRATION CONCENTRATION (Threshold: Refs >= 3)
# ==========================================
def test_integration_concentration_fires():
    data = {"salesforce_named_credentials": [{"name": "SAP", "endpoint": "api.sap", "flow_reference_count": 5}]}
    res = integration_concentration.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 5.0
    assert res[0].raw_evidence["named_credential"] == "SAP"

def test_integration_concentration_does_not_fire():
    data = {"salesforce_named_credentials": [{"name": "SAP", "endpoint": "api.sap", "flow_reference_count": 1}]}
    assert integration_concentration.detect(data) == []

def test_integration_concentration_empty_input():
    assert integration_concentration.detect({}) == []

# ==========================================
# 5. KNOWLEDGE GAP (Threshold: Gap > 0.4, Closed >= 30)
# ==========================================
def test_knowledge_gap_fires():
    # 100 closed, 20 with KB -> gap is 0.8
    data = {"salesforce_knowledge_coverage": [{"category": "Billing", "closed_count": 100, "with_kb_count": 20}]}
    res = knowledge_gap.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 0.8
    assert res[0].raw_evidence["gap_score"] == 0.8

def test_knowledge_gap_does_not_fire():
    # 100 closed, 90 with KB -> gap is 0.1
    data = {"salesforce_knowledge_coverage": [{"category": "Billing", "closed_count": 100, "with_kb_count": 90}]}
    assert knowledge_gap.detect(data) == []

def test_knowledge_gap_empty_input():
    assert knowledge_gap.detect({}) == []

# ==========================================
# 6. PERMISSION BOTTLENECK (Threshold: Score > 10.0)
# ==========================================
def test_permission_bottleneck_fires():
    data = {"salesforce_permission_bottlenecks": [{"step_name": "Admin Approval", "bottleneck_score": 15.0}]}
    res = permission_bottleneck.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 15.0
    assert res[0].raw_evidence["step_name"] == "Admin Approval"

def test_permission_bottleneck_does_not_fire():
    data = {"salesforce_permission_bottlenecks": [{"step_name": "Admin Approval", "bottleneck_score": 5.0}]}
    assert permission_bottleneck.detect(data) == []

def test_permission_bottleneck_empty_input():
    assert permission_bottleneck.detect({}) == []

# ==========================================
# 7. REPETITION (Threshold: Score > 0.6, Elements < 15)
# ==========================================
def test_repetition_fires():
    data = {"salesforce_flow_inventory": [{
        "id": "f1", "name": "Auto Status", "is_active": True, "type": "record-triggered",
        "element_count": 5, "flow_activity_score": 0.9, "trigger_object": "Case"
    }]}
    res = repetition.detect(data)
    assert len(res) == 1
    assert res[0].metric_value == 0.9
    assert res[0].raw_evidence["flowName"] == "Auto Status"

def test_repetition_does_not_fire():
    # Score is too low
    data = {"salesforce_flow_inventory": [{
        "id": "f1", "name": "Auto Status", "is_active": True, "type": "record-triggered",
        "element_count": 5, "flow_activity_score": 0.2, "trigger_object": "Case"
    }]}
    assert repetition.detect(data) == []

def test_repetition_empty_input():
    assert repetition.detect({}) == []
