"""
Tests for DetectorResult dataclass.
These pass even before any detector logic is implemented.
"""
import pytest
from discovery.models import DetectorResult


def test_detector_result_valid():
    dr = DetectorResult(
        detector_id="HANDOFF_FRICTION",
        signal_source="salesforce",
        metric_value=1.6,
        threshold=1.5,
        raw_evidence={"owner_changes_90d": 480, "total_cases_90d": 300, "handoff_score": 1.6},
    )
    assert dr.detector_id == "HANDOFF_FRICTION"
    assert dr.metric_value == 1.6


def test_detector_result_requires_numeric_evidence():
    with pytest.raises(ValueError, match="numeric"):
        DetectorResult(
            detector_id="TEST",
            signal_source="salesforce",
            metric_value=1.0,
            threshold=0.5,
            raw_evidence={"label": "no numbers here"},
        )


def test_detector_result_requires_dict_evidence():
    with pytest.raises((ValueError, TypeError)):
        DetectorResult(
            detector_id="TEST",
            signal_source="salesforce",
            metric_value=1.0,
            threshold=0.5,
            raw_evidence="not a dict",
        )
