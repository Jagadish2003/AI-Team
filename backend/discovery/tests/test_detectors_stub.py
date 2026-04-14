"""
Stub tests for all seven detectors.
Each returns [] until SF-2.5 implements the logic.
These tests confirm the module structure is importable and callable.
"""
import os
os.environ["INGEST_MODE"] = "offline"

from discovery.detectors import (
    repetition, handoff_friction, approval_delay,
    knowledge_gap, integration_concentration,
    permission_bottleneck, cross_system_echo,
)
from discovery.ingest.salesforce import ingest as sf_ingest
from discovery.ingest.servicenow import ingest as sn_ingest
from discovery.ingest.jira import ingest as jira_ingest


def _data():
    return sf_ingest(), sn_ingest(), jira_ingest()


def test_repetition_callable():
    sf, sn, jira = _data()
    result = repetition.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_handoff_friction_callable():
    sf, sn, jira = _data()
    result = handoff_friction.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_approval_delay_callable():
    sf, sn, jira = _data()
    result = approval_delay.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_knowledge_gap_callable():
    sf, sn, jira = _data()
    result = knowledge_gap.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_integration_concentration_callable():
    sf, sn, jira = _data()
    result = integration_concentration.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_permission_bottleneck_callable():
    sf, sn, jira = _data()
    result = permission_bottleneck.detect(sf, sn, jira)
    assert isinstance(result, list)

def test_cross_system_echo_callable():
    sf, sn, jira = _data()
    result = cross_system_echo.detect(sf, sn, jira)
    assert isinstance(result, list)
