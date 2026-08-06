"""2.0-B1 T1 — Trace graph engine tests.

Covers this subtask's acceptance criteria:
  AC1 — a finding expands to a complete chain (finding -> evidence -> source
        record); every hop carries origin, connector, run id, and timestamp.
  AC2 — a joined claim displays the join type and correlation window used;
        a claim whose join is outside window can never appear (regression
        against MSP-B7).

Plus the supporting plumbing this engine depends on:
  - cloud_ops_finding.build_corroboration only ever carries within-window
    correlation-window traces (the producing-pack half of AC2's guarantee).
  - track_a_adapter persists a pack's finding_contract onto the stored
    opportunity (without this, cloud_ops/security_ops findings would have no
    queryable chain at all after materialization).

DB-free throughout.
"""
from __future__ import annotations

from typing import Any, Dict, List

from discovery.packs import cloud_ops_finding as fc


def _window(join_type="event_incident", window_seconds=7200, delta_seconds=300.0,
            within_window=True, a_at="2026-01-01T12:00:00+00:00",
            b_at="2026-01-01T12:05:00+00:00") -> Dict[str, Any]:
    return {
        "join_type": join_type,
        "window_seconds": window_seconds,
        "delta_seconds": delta_seconds,
        "within_window": within_window,
        "a_at": a_at,
        "b_at": b_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# cloud_ops_finding.build_corroboration — the producing-pack half of AC2
# ─────────────────────────────────────────────────────────────────────────────

def test_build_corroboration_keeps_only_within_window_joins():
    within = _window(delta_seconds=300.0)
    outside = _window(delta_seconds=99999.0, within_window=False)
    corroboration = fc.build_corroboration(
        fc.STATUS_CORROBORATED,
        sources=["servicenow", "events"],
        label="Corroborated by recurring event signature (window-gated)",
        window_gated=True,
        correlation_windows=[within, outside],
    )
    assert corroboration["correlation_windows"] == [within]


def test_build_corroboration_caps_correlation_windows():
    windows = [_window(delta_seconds=float(i)) for i in range(10)]
    corroboration = fc.build_corroboration(
        fc.STATUS_CORROBORATED, sources=["events"], label="x",
        window_gated=True, correlation_windows=windows,
    )
    assert len(corroboration["correlation_windows"]) == fc._MAX_FINDING_CORRELATION_WINDOWS


def test_build_corroboration_defaults_to_empty_list():
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE, sources=["servicenow"], label=fc.SINGLE_SOURCE_LABEL,
    )
    assert corroboration["correlation_windows"] == []


def test_shared_ci_hotspot_detector_only_surfaces_within_window_join():
    """End-to-end through the real detector: an out-of-window join recorded on
    the event_signature row must never reach the emitted finding's corroboration."""
    from discovery.detectors import cloud_ops_shared_ci_hotspot as detector

    block = {
        "ci_graph": {"edges": [
            {"from": "svc-a-ci", "to": "shared-db"},
            {"from": "svc-b-ci", "to": "shared-db"},
            {"from": "svc-c-ci", "to": "shared-db"},
        ]},
        "service_incidents": [
            {"service": "svc-a", "ci": "svc-a-ci", "incident_count": 2, "incident_ids": ["INC1"]},
            {"service": "svc-b", "ci": "svc-b-ci", "incident_count": 1, "incident_ids": ["INC2"]},
            {"service": "svc-c", "ci": "svc-c-ci", "incident_count": 1, "incident_ids": ["INC3"]},
        ],
        "event_signatures": [
            {
                "signature": "sig-shared-db",
                "ci": "shared-db",
                "window_overlap": True,
                "correlation_windows": [
                    _window(delta_seconds=120.0, within_window=True),
                    _window(delta_seconds=54000.0, within_window=False),
                ],
            }
        ],
    }
    results = detector.detect(sn_data={"cloud_ops": block})
    assert len(results) == 1
    contract = results[0].raw_evidence["finding_contract"]
    windows = contract["corroboration"]["correlation_windows"]
    assert len(windows) == 1
    assert windows[0]["within_window"] is True
    assert windows[0]["delta_seconds"] == 120.0


# ─────────────────────────────────────────────────────────────────────────────
# track_a_adapter — persistence of the finding_contract onto the stored opp
# ─────────────────────────────────────────────────────────────────────────────

def test_track_a_adapter_persists_finding_contract():
    from discovery import track_a_adapter

    contract = fc.build_finding_contract(
        evidence={"service_count": 3, "incident_count": 4},
        confidence=fc.build_confidence(fc.CONFIDENCE_HIGH, capped=False, eligible_for_high=True),
        corroboration=fc.build_corroboration(
            fc.STATUS_CORROBORATED, sources=["servicenow", "events"],
            label="Corroborated", window_gated=True,
            correlation_windows=[_window()],
        ),
        source_trace=fc.build_source_trace(
            systems=["servicenow", "events"],
            artifacts=[
                {"type": "shared_ci", "id": "shared-db"},
                {"type": "event_signature", "id": "sig-shared-db"},
            ],
        ),
    )
    runner_payload = {
        "opportunities": [{
            "detector_id": "SHARED_CI_HOTSPOT",
            "packId": "cloud_ops",
            "packVersion": "1.0.0",
            "signal_source": "servicenow",
            "metric_value": 3.0,
            "threshold": 3.0,
            "impact": 4, "effort": 2, "confidence": "HIGH", "tier": "Strategic",
            "roadmap_stage": "quick_win",
            "evidenceIds": [],
            "evidence": [],
            "raw_evidence": {"finding_contract": contract},
            "score_debug": {},
        }],
    }
    stored = track_a_adapter.to_track_a_opportunities(runner_payload)
    assert len(stored) == 1
    assert stored[0]["findingContract"] == contract


def test_track_a_adapter_finding_contract_absent_for_non_cloud_ops_pack():
    from discovery import track_a_adapter

    runner_payload = {
        "opportunities": [{
            "detector_id": "HANDOFF_FRICTION",
            "packId": "service_cloud",
            "packVersion": "1.0.0",
            "signal_source": "salesforce",
            "metric_value": 1.0,
            "threshold": 1.0,
            "impact": 1, "effort": 1, "confidence": "LOW", "tier": "Strategic",
            "roadmap_stage": "quick_win",
            "evidenceIds": [],
            "evidence": [],
            "raw_evidence": {"some_metric": 1},
            "score_debug": {},
        }],
    }
    stored = track_a_adapter.to_track_a_opportunities(runner_payload)
    assert stored[0]["findingContract"] is None


# ─────────────────────────────────────────────────────────────────────────────
# trace_graph.build_finding_trace — AC1 (full chain + hop metadata)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_finding_trace_generic_pack_full_chain():
    from app.trace_graph import HOP_EVIDENCE, HOP_FINDING, HOP_SOURCE_RECORD, build_finding_trace

    opp = {
        "id": "opp_001",
        "title": "Repetitive automation opportunity",
        "packId": "service_cloud",
        "packVersion": "1.0.0",
        "evidenceIds": ["ev_sf_aaa"],
        "corroboration_rule_ids": ["COR-01"],
    }
    evidence_items = [{
        "id": "ev_sf_aaa",
        "source": "Salesforce",
        "tsLabel": "24 Jun 2026, 10:00",
        "provenanceType": "observed",
        "evidenceType": "Metric",
        "title": "High volume detected",
        "confidence": "HIGH",
        "packId": "service_cloud",
        "detectorId": "REPETITIVE_AUTOMATION",
    }]
    pointers = [{
        "source_system": "salesforce",
        "source_artifact": "ev_sf_aaa",
        "source_timestamp": "24 Jun 2026, 10:00",
        "origin": "observed",
        "extraction_job_id": None,
        "chunk_id": None,
        "retrieval_result_id": None,
        "detector_evidence_id": "ev_sf_aaa",
        "confidence": None,
    }]

    trace = build_finding_trace(
        opp, "run_001", evidence_items=evidence_items, pointers=pointers,
        run_completed_at="2026-06-24T10:05:00Z",
    )

    assert trace.opportunity_id == "opp_001"
    assert trace.run_id == "run_001"
    assert trace.complete is True
    assert trace.truncated is False
    assert [hop.hop_type for hop in trace.hops] == [HOP_FINDING, HOP_EVIDENCE, HOP_SOURCE_RECORD]

    finding_hop, evidence_hop, source_hop = trace.hops

    # AC1: every hop carries origin, connector, run id, timestamp (key present).
    for hop in trace.hops:
        d = hop.to_dict()
        for key in ("origin", "connector", "run_id", "timestamp", "hop_id", "from_hop_id"):
            assert key in d

    assert finding_hop.from_hop_id is None
    assert finding_hop.run_id == "run_001"
    assert finding_hop.origin == "observed"

    assert evidence_hop.from_hop_id == finding_hop.hop_id
    assert evidence_hop.origin == "observed"
    assert evidence_hop.connector == "salesforce"
    assert evidence_hop.run_id == "run_001"
    assert evidence_hop.timestamp == "24 Jun 2026, 10:00"

    assert source_hop.from_hop_id == evidence_hop.hop_id
    assert source_hop.origin == "observed"
    assert source_hop.connector == "salesforce"
    assert source_hop.run_id == "run_001"
    assert source_hop.timestamp == "24 Jun 2026, 10:00"
    assert source_hop.label == "ev_sf_aaa"


def test_build_finding_trace_marks_inferred_evidence_and_finding_root():
    from app.trace_graph import ORIGIN_INFERRED, build_finding_trace

    opp = {"id": "opp_002", "evidenceIds": ["ev_x"]}
    evidence_items = [{
        "id": "ev_x", "source": "Jira", "tsLabel": "t", "provenanceType": "inferred",
    }]
    trace = build_finding_trace(opp, "run_002", evidence_items=evidence_items)
    finding_hop, evidence_hop = trace.hops
    assert evidence_hop.origin == ORIGIN_INFERRED
    # A finding with any inferred evidence is itself labelled inferred — never
    # presented as pure ground truth when part of its basis is not.
    assert finding_hop.origin == ORIGIN_INFERRED


def test_build_finding_trace_cloud_ops_source_trace_artifacts():
    """Cloud_ops findings have no build_evidence() output (evidenceIds is
    empty) — the chain must still reach source records via the persisted
    finding_contract's source_trace artifacts (the persistence fix this
    subtask depends on)."""
    from app.trace_graph import HOP_SOURCE_RECORD, build_finding_trace

    contract = fc.build_finding_contract(
        evidence={"service_count": 3},
        confidence=fc.build_confidence(fc.CONFIDENCE_HIGH, capped=False, eligible_for_high=True),
        corroboration=fc.build_corroboration(
            fc.STATUS_CORROBORATED, sources=["servicenow", "events"],
            label="Corroborated", window_gated=True,
            correlation_windows=[_window(delta_seconds=90.0)],
        ),
        source_trace=fc.build_source_trace(
            systems=["servicenow", "events"],
            artifacts=[
                {"type": "shared_ci", "id": "shared-db"},
                {"type": "incident", "id": "INC1", "service": "svc-a"},
                {"type": "event_signature", "id": "sig-shared-db"},
            ],
        ),
    )
    opp = {
        "id": "opp_003", "packId": "cloud_ops", "evidenceIds": [], "findingContract": contract,
    }
    trace = build_finding_trace(opp, "run_003")

    source_hops = [h for h in trace.hops if h.hop_type == HOP_SOURCE_RECORD]
    assert len(source_hops) == 3
    assert trace.complete is True
    incident_hop = next(h for h in source_hops if h.detail["artifact_type"] == "incident")
    assert incident_hop.connector == "servicenow"
    event_hop = next(h for h in source_hops if h.detail["artifact_type"] == "event_signature")
    assert event_hop.connector == "events"


# ─────────────────────────────────────────────────────────────────────────────
# trace_graph.build_finding_trace — AC2 (join type/window + out-of-window block)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_finding_trace_surfaces_join_type_and_window():
    from app.trace_graph import build_finding_trace

    contract = fc.build_finding_contract(
        evidence={"recurrence_count": 5},
        confidence=fc.build_confidence(fc.CONFIDENCE_HIGH, capped=False, eligible_for_high=True),
        corroboration=fc.build_corroboration(
            fc.STATUS_CORROBORATED, sources=["servicenow", "events"],
            label="Corroborated by recurring event signature (window-gated)",
            window_gated=True,
            correlation_windows=[_window(join_type="event_incident", window_seconds=7200, delta_seconds=300.0)],
        ),
        source_trace=fc.build_source_trace(
            systems=["servicenow", "events"],
            artifacts=[
                {"type": "recurrence_signature", "id": "sig-1"},
                {"type": "event_signature", "id": "sig-1"},
            ],
        ),
    )
    opp = {"id": "opp_004", "packId": "cloud_ops", "evidenceIds": [], "findingContract": contract}
    trace = build_finding_trace(opp, "run_004")

    assert len(trace.joins) == 1
    join = trace.joins[0]
    assert join.join_type == "event_incident"
    assert join.window_seconds == 7200
    assert join.delta_seconds == 300.0
    assert join.within_window is True
    # The join attaches to the event_signature hop — the claim it corroborates.
    event_hop = next(h for h in trace.hops if h.detail.get("artifact_type") == "event_signature")
    assert join.hop_id == event_hop.hop_id


def test_build_finding_trace_out_of_window_join_never_appears():
    """AC2 regression: even if a caller hands this module an UNFILTERED MSP-B7
    trace list (bypassing cloud_ops_finding's own filter), an out-of-window
    join must never surface in the trace's joins."""
    from app.trace_graph import build_finding_trace

    # Simulate a contract whose corroboration carries a raw, unfiltered window
    # list (as if _filter_correlation_windows had not run) — the trace engine
    # must apply its own independent filter.
    contract = {
        "evidence": {"recurrence_count": 5},
        "confidence": {"level": "HIGH", "capped": False, "eligible_for_high": True, "cap_reason": "", "note": ""},
        "corroboration": {
            "status": fc.STATUS_CORROBORATED,
            "sources": ["servicenow", "events"],
            "label": "Corroborated",
            "window_gated": True,
            "rule_ids": [],
            "correlation_windows": [
                _window(delta_seconds=120.0, within_window=True),
                _window(delta_seconds=88000.0, within_window=False),
            ],
        },
        "source_trace": {
            "systems": ["servicenow", "events"],
            "artifacts": [{"type": "event_signature", "id": "sig-1"}],
        },
    }
    opp = {"id": "opp_005", "packId": "cloud_ops", "evidenceIds": [], "findingContract": contract}
    trace = build_finding_trace(opp, "run_005")

    assert len(trace.joins) == 1
    assert trace.joins[0].within_window is True
    assert trace.joins[0].delta_seconds == 120.0
    assert all(j.within_window for j in trace.joins)


def test_build_finding_trace_no_corroboration_windows_yields_no_joins():
    from app.trace_graph import build_finding_trace

    opp = {"id": "opp_006", "evidenceIds": []}
    trace = build_finding_trace(opp, "run_006")
    assert trace.joins == []
    assert trace.complete is False  # only the finding root — no chain reached


# ─────────────────────────────────────────────────────────────────────────────
# Never raises — degrade gracefully on malformed input
# ─────────────────────────────────────────────────────────────────────────────

def test_build_finding_trace_never_raises_on_malformed_input():
    from app.trace_graph import build_finding_trace

    trace = build_finding_trace(
        {"id": "opp_bad", "evidenceIds": ["missing"], "findingContract": "not-a-dict"},
        "run_bad",
        evidence_items=[{"id": "wrong_id"}],
        pointers=[{"not": "a valid pointer"}],
    )
    assert trace.opportunity_id == "opp_bad"
    assert trace.hops  # at least the finding root

    trace2 = build_finding_trace(None, "run_bad2")  # type: ignore[arg-type]
    assert trace2.hops == []
    assert trace2.complete is False
