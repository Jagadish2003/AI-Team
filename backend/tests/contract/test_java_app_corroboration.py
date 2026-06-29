"""
R17-A3 / T5 — contract tests for Java application runtime corroboration (COR-09).

Covers the acceptance criterion assigned to this subtask:

  AC5 — A Java-app operational signal can corroborate a finding in another system
        (e.g. an error-rate rise corroborating a ServiceNow incident spike for the
        same service) and contribute to confidence.

Verifies the Java signal plugs into the EXISTING cross-system corroboration model
(no separate Java confidence path), that only OBSERVED signal corroborates
(observed-vs-inferred preserved), the 30-day window is enforced, and that the
mapper produces the engine-consumable record shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor09_java_app_runtime,
    evaluate_corroboration,
)
from discovery.ingest.java_app_signals import (
    build_java_app_corroboration_payload,
    build_java_app_corroboration_records,
    build_java_app_signal,
)
from discovery.packs.corroboration_rules import CORROBORATION_RULES, is_elevating_rule

RUN_TS = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
COVENANT = "COVENANT_TRACKING_GAP"
SERVICE = "payments-service"


# ─────────────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────────────
def _within_ts() -> str:
    return (RUN_TS - timedelta(days=5)).isoformat()


def _stale_ts() -> str:
    return (RUN_TS - timedelta(days=40)).isoformat()


def _future_ts() -> str:
    return (RUN_TS + timedelta(hours=2)).isoformat()


def _java_signal(
    *,
    signal_type="error_rate_rise",
    service=SERVICE,
    origin="observed",
    timestamp=None,
    detector_ids=None,
    confidence="HIGH",
    pointer_origin=None,
):
    rec = {
        "source_system": "java_app",
        "service": service,
        "signal_type": signal_type,
        "confidence": confidence,
        "origin": origin,
        "timestamp": timestamp or _within_ts(),
        "evidence": {"baseline_value": 0.002, "current_value": 0.06, "change_pct": 2900.0},
        "evidence_pointer": {
            "source_system": "java_app",
            "origin": pointer_origin or origin,
        },
    }
    if detector_ids is not None:
        rec["detector_ids"] = detector_ids
    return rec


def _run_data(*, systems=("salesforce", "java_app"), java_signals=None, servicenow_incidents=None):
    rd = {"connected_systems": list(systems)}
    if java_signals is not None:
        rd["java_app"] = {"signals": java_signals}
    if servicenow_incidents is not None:
        rd["servicenow"] = {"incidents": servicenow_incidents}
    return rd


def _evaluate(detector_id=COVENANT, **rd_kwargs):
    return evaluate_corroboration(
        detector_id=detector_id,
        pack_id="ncino",
        run_data=_run_data(**rd_kwargs),
        run_timestamp=RUN_TS,
        org_id="demo-org",
    )


def _inline_metrics():
    """Two samples producing an error-rate rise + an unhealthy service, in window."""
    return [
        {"service": SERVICE, "timestamp": (RUN_TS - timedelta(hours=2)).isoformat(),
         "health": "UP", "error_rate": 0.002, "latency_p95_ms": 100, "throughput_rpm": 5000},
        {"service": SERVICE, "timestamp": (RUN_TS - timedelta(hours=1)).isoformat(),
         "health": "DOWN", "error_rate": 0.06, "latency_p95_ms": 110, "throughput_rpm": 4900},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────
def test_cor09_registered_and_elevating():
    assert "COR-09" in CORROBORATION_RULES
    rule = CORROBORATION_RULES["COR-09"]
    assert rule.elevates is True
    assert rule.elevation_target == "HIGH"
    assert is_elevating_rule("COR-09") is True


# ─────────────────────────────────────────────────────────────────────────────
# check_cor09 — observed / window / linkage
# ─────────────────────────────────────────────────────────────────────────────
def test_cor09_fires_for_observed_in_window_linked_signal():
    rd = _run_data(java_signals=[_java_signal(detector_ids=[COVENANT])])
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is True


def test_cor09_fires_for_prefiltered_signal_without_detector_ids():
    # No detector_ids → pre-filtered relevant (same convention as ServiceNow/Jira).
    rd = _run_data(java_signals=[_java_signal()])
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is True


def test_cor09_inferred_signal_does_not_corroborate():
    """Observed-vs-inferred preserved: an inferred Java signal never corroborates."""
    rd = _run_data(java_signals=[_java_signal(origin="inferred", detector_ids=[COVENANT])])
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is False


def test_cor09_inferred_via_evidence_pointer_does_not_corroborate():
    rd = _run_data(
        java_signals=[{
            "source_system": "java_app", "service": SERVICE, "signal_type": "error_rate_rise",
            "timestamp": _within_ts(), "detector_ids": [COVENANT],
            "evidence_pointer": {"origin": "inferred"},
        }]
    )
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is False


def test_cor09_stale_signal_outside_window_does_not_corroborate():
    rd = _run_data(java_signals=[_java_signal(timestamp=_stale_ts(), detector_ids=[COVENANT])])
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is False


def test_cor09_future_signal_does_not_corroborate():
    rd = _run_data(java_signals=[_java_signal(timestamp=_future_ts(), detector_ids=[COVENANT])])
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is False


def test_cor09_no_java_block_does_not_corroborate():
    rd = {"connected_systems": ["salesforce", "servicenow"]}
    assert check_cor09_java_app_runtime(COVENANT, rd, RUN_TS) is False


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — Java runtime signal elevates through the existing cross-system model
# ─────────────────────────────────────────────────────────────────────────────
def test_java_app_runtime_signal_elevates_medium_to_high():
    result = _evaluate(java_signals=[_java_signal(detector_ids=[COVENANT])])
    assert "COR-09" in result.rule_ids
    assert "Java application (runtime signal)" in result.corroboration_sources
    assert result.elevated_confidence == "HIGH"
    assert result.confidence_elevated is True
    assert result.corroboration_label == "Corroborated by Java application runtime signals"


def test_java_app_corroborates_servicenow_incident_spike():
    """The headline AC5 example: ServiceNow incidents + Java error-rate rise on the
    same service support each other → both rules fire, HIGH, both sources shown."""
    result = _evaluate(
        systems=("salesforce", "servicenow", "java_app"),
        servicenow_incidents=[{"detector_ids": [COVENANT], "state": "Open", "sys_created_on": _within_ts()}],
        java_signals=[_java_signal(signal_type="error_rate_rise", detector_ids=[COVENANT])],
    )
    assert "COR-01" in result.rule_ids
    assert "COR-09" in result.rule_ids
    assert "ServiceNow" in result.corroboration_sources
    assert "Java application (runtime signal)" in result.corroboration_sources
    assert result.elevated_confidence == "HIGH"


def test_inferred_java_signal_alone_does_not_elevate():
    result = _evaluate(java_signals=[_java_signal(origin="inferred", detector_ids=[COVENANT])])
    assert "COR-09" not in result.rule_ids
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


def test_java_app_only_connected_is_single_source_no_elevation():
    result = _evaluate(
        systems=("java_app",),
        java_signals=[_java_signal(detector_ids=[COVENANT])],
    )
    # COR-08 single-source short-circuit: a lone system cannot self-corroborate.
    assert result.rule_ids == []
    assert result.elevated_confidence == "MEDIUM"


def test_apply_confidence_elevates_but_never_downgrades():
    result = _evaluate(java_signals=[_java_signal(detector_ids=[COVENANT])])
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    # Never downgrades a scorer that already assigned HIGH.
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# Mapper — engine-consumable record shape
# ─────────────────────────────────────────────────────────────────────────────
def test_mapper_produces_required_record_shape():
    signal = build_java_app_signal(metric_samples=_inline_metrics(), application_id=SERVICE)
    records = build_java_app_corroboration_records(signal, detector_ids=[COVENANT])
    assert records
    for r in records:
        assert r["source_system"] == "java_app"
        assert r["service"] == SERVICE                 # app/service identity
        assert r["signal_type"]                         # signal type
        assert r["confidence"] in ("HIGH", "MEDIUM")    # confidence
        assert r["origin"] == "observed"                # observed evidence
        assert r["timestamp"]                           # timestamp
        assert isinstance(r["evidence"], dict)          # supporting evidence
        assert r["detector_ids"] == [COVENANT]          # linkage


def test_mapper_omits_detector_ids_when_not_supplied():
    signal = build_java_app_signal(metric_samples=_inline_metrics(), application_id=SERVICE)
    records = build_java_app_corroboration_records(signal)
    assert records
    assert all("detector_ids" not in r for r in records)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end run-data wiring (build payload -> build_corroboration_run_data -> evaluate)
# ─────────────────────────────────────────────────────────────────────────────
def test_run_data_builder_wires_java_app_block_to_high():
    payload_block = build_java_app_corroboration_payload(
        metric_samples=_inline_metrics(),
        application_id=SERVICE,
        detector_ids=[COVENANT],
    )
    run_data = build_corroboration_run_data(
        systems={"salesforce", "java_app"},
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload_block],
    )
    assert "java_app" in run_data
    assert run_data["java_app"]["signals"]

    result = evaluate_corroboration(
        detector_id=COVENANT,
        pack_id="ncino",
        run_data=run_data,
        run_timestamp=RUN_TS,
        org_id="demo-org",
    )
    assert "COR-09" in result.rule_ids
    assert result.elevated_confidence == "HIGH"


def test_run_data_builder_ignores_java_block_when_not_connected():
    """A java_app block in payloads must not corroborate unless java_app is connected."""
    payload_block = build_java_app_corroboration_payload(
        metric_samples=_inline_metrics(), application_id=SERVICE, detector_ids=[COVENANT]
    )
    run_data = build_corroboration_run_data(
        systems={"salesforce", "servicenow"},  # java_app NOT connected
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload_block],
    )
    assert "java_app" not in run_data
