from __future__ import annotations

import importlib
from dataclasses import fields


DETECTOR_MODULES = [
    "discovery.detectors.repetition",
    "discovery.detectors.handoff_friction",
    "discovery.detectors.approval_delay",
    "discovery.detectors.knowledge_gap",
    "discovery.detectors.integration_concentration",
    "discovery.detectors.permission_bottleneck",
    "discovery.detectors.cross_system_echo",
    "discovery.detectors.loan_origination_routing_friction",
    "discovery.detectors.covenant_tracking_gap",
    "discovery.detectors.checklist_bottleneck",
    "discovery.detectors.spreading_bottleneck",
    "discovery.detectors.approval_bottleneck",
    "discovery.detectors.application_stall",
    "discovery.detectors.benefit_election_deadline",
    "discovery.detectors.disbursement_overdue",
    "discovery.detectors.disability_review_bottleneck",
]


def test_detector_evaluation_dataclass_fields_match_section_4a():
    from app.temporal import DetectorEvaluation

    assert [field.name for field in fields(DetectorEvaluation)] == [
        "detector_id",
        "detector_cls",
        "signal_source",
        "metric_value",
        "threshold",
        "fired",
        "raw_evidence",
    ]


def test_snapshot_signals_importable_from_temporal():
    from app.temporal import snapshot_signals

    assert callable(snapshot_signals)


def test_runner_collects_one_evaluation_for_each_detector():
    from app.temporal import DetectorEvaluation
    from discovery.detectors import handoff_friction, knowledge_gap, repetition
    from discovery.runner import _run_detector_phase

    sf_data = {
        "case_metrics": {
            "total_cases_90d": 100,
            "closed_cases_90d": 100,
            "owner_changes_90d": 2,
            "handoff_score": 0.1,
            "knowledge_gap_score": 0.1,
            "cases_with_kb_link": 90,
        },
        "flow_inventory": {
            "flow_activity_score": 0.1,
            "flows": [],
        },
    }

    detectors = [repetition, handoff_friction, knowledge_gap]
    detector_results, all_evaluated = _run_detector_phase(detectors, sf_data, {}, {})

    assert detector_results == []
    assert len(all_evaluated) == len(detectors)
    assert all(isinstance(evaluation, DetectorEvaluation) for evaluation in all_evaluated)
    assert {evaluation.detector_id for evaluation in all_evaluated} == {
        "REPETITIVE_AUTOMATION",
        "HANDOFF_FRICTION",
        "KNOWLEDGE_GAP",
    }
    assert all(evaluation.fired is False for evaluation in all_evaluated)


def test_runner_uses_snapshot_signals_with_all_evaluated(monkeypatch):
    from app.temporal import DetectorEvaluation
    from discovery.runner import _snapshot_detector_evaluations

    class DemoDetector:
        SIGNAL_METRICS = []

    evaluation = DetectorEvaluation(
        detector_id="DEMO_DETECTOR",
        detector_cls=DemoDetector,
        signal_source="salesforce",
        metric_value=0.0,
        threshold=1.0,
        fired=False,
        raw_evidence={"sample_metric": 0},
    )
    captured = {}

    def fake_snapshot_signals(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("discovery.runner.snapshot_signals", fake_snapshot_signals)

    run_completed_at = _snapshot_detector_evaluations(
        org_id="org_1",
        run_id="run_1",
        pack_id="service_cloud",
        detector_results=[],
        all_evaluated=[evaluation],
    )

    assert captured["org_id"] == "org_1"
    assert captured["run_id"] == "run_1"
    assert captured["pack_id"] == "service_cloud"
    assert captured["detector_results"] == []
    assert captured["all_evaluated"] == [evaluation]
    assert captured["run_completed_at"] is run_completed_at


def test_all_detectors_expose_evaluate_and_signal_metrics():
    for module_name in DETECTOR_MODULES:
        module = importlib.import_module(module_name)
        assert callable(module.evaluate), module_name

        metrics = getattr(module, "SIGNAL_METRICS", None)
        assert isinstance(metrics, list), module_name
        assert len(metrics) <= 8, module_name
        assert len(metrics) == len(set(metrics)), module_name

        evaluation = module.evaluate({}, {}, {})
        assert isinstance(evaluation.detector_cls, type), module_name
        assert getattr(evaluation.detector_cls, "SIGNAL_METRICS", []) == metrics
        for metric_name in metrics:
            assert metric_name in evaluation.raw_evidence, f"{module_name}: {metric_name}"
            value = evaluation.raw_evidence[metric_name]
            assert isinstance(value, (int, float)), f"{module_name}: {metric_name}"
            assert not isinstance(value, bool), f"{module_name}: {metric_name}"
