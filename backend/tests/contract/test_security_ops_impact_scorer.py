"""MSP-B12 Security Operations scorer calibration contract."""
from __future__ import annotations

import pytest

from discovery.models import DetectorResult
from discovery.packs import security_ops_scorer as scorer
from discovery.packs.security_ops_config import get_calibration


def _finding(*, severity="medium", signature="x", **evidence):
    evidence = {
        "effort_minutes": 600,
        "breadth": 4,
        "recurrence_stability": 0.75,
        "severity_band": severity,
        **evidence,
    }
    contract = {
        "evidence": dict(evidence),
        "confidence": {"level": "HIGH"},
        "corroboration": {"status": "corroborated"},
        "source_trace": {"systems": ["servicenow"], "artifacts": []},
    }
    return DetectorResult(
        detector_id="SECOPS_REMEDIATION_RECURRENCE",
        signal_source="servicenow",
        metric_value=4,
        threshold=3,
        raw_evidence={
            **evidence,
            "finding_ref": signature,
            "confidence": "HIGH",
            "corroborated": True,
            "corroboration_sources": ["servicenow", "cloud_events"],
            "finding_contract": contract,
        },
    )


@pytest.mark.parametrize(
    "band,expected",
    [("critical", 1.0), ("high", 0.75), ("medium", 0.5),
     ("low", 0.25), ("informational", 0.1)],
)
def test_every_supported_severity_band_uses_configured_weight(band, expected):
    finding = _finding(severity=band)
    entry = scorer.rank_security_ops_findings([finding])[id(finding)]
    assert entry["dimensions"]["severity_label"] == band
    assert entry["dimensions"]["severity_band"] == pytest.approx(expected)


def test_critical_ranks_above_informational_when_everything_else_is_equal():
    critical = _finding(severity="critical", signature="critical")
    informational = _finding(severity="informational", signature="informational")
    ranking = scorer.rank_security_ops_findings([informational, critical])
    assert ranking[id(critical)]["rank"] == 1
    assert ranking[id(critical)]["ops_impact_score"] > ranking[id(informational)]["ops_impact_score"]


@pytest.mark.parametrize("value,expected", [(-1, 0.0), (0, 0.0), (1, 1.0), (2, 1.0)])
def test_recurrence_stability_boundaries_are_clamped(value, expected):
    finding = _finding(recurrence_stability=value)
    entry = scorer.rank_security_ops_findings([finding])[id(finding)]
    assert entry["dimensions"]["recurrence_stability"] == expected


@pytest.mark.parametrize("severity", [None, "", "unknown", "not-a-band"])
def test_missing_or_unknown_severity_uses_documented_default(severity):
    finding = _finding(severity=severity)
    entry = scorer.rank_security_ops_findings([finding])[id(finding)]
    cal = get_calibration()
    assert entry["dimensions"]["severity_label"] == cal.severity_default
    assert entry["dimensions"]["severity_band"] == cal.severity_band[cal.severity_default]


def test_breadth_uses_only_queues_services_and_ci_classes():
    finding = _finding(
        breadth=None,
        service_count=2,
        queues=["Security", "IT", "Security"],
        ci_classes=["server", "database"],
        host_count=5000,
        vulnerability_count=9000,
    )
    assert scorer.breadth(finding) == 6


def test_result_is_stable_and_preserves_evidence_and_corroboration():
    finding = _finding(severity="high")
    first = scorer.score_security_ops(finding)
    second = scorer.score_security_ops(finding)
    assert first == second
    assert first["confidence"] == "HIGH"
    assert first["corroborated"] is True
    assert first["corroboration_sources"] == ["servicenow", "cloud_events"]
    assert first["score_debug"]["evidence_preserved"] is True
    assert first["score_debug"]["corroboration_preserved"] is True


def test_calibration_includes_all_values_kept_outside_detector_code():
    cal = get_calibration()
    assert set(scorer.DIMENSIONS) == set(cal.impact_weights)
    assert cal.severity_default == "medium"
    assert cal.normalization["impact_min"] == 1
    assert cal.normalization["impact_max"] == 10
    assert cal.score_tiers["strategic"]["min_score"] == 0.7
    assert cal.confidence_defaults["missing"] == "MEDIUM"
    assert cal.roadmap_stages["strategic"] == "strategic"
    assert cal.presentation == {"effort": 3, "effort_label": "Low-Med"}
