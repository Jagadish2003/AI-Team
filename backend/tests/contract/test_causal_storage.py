"""Unit tests for T6 — store_causal_hypothesis() (ENT-6 / T3-S16-A).

Covers the Definition of Done and the storage-side acceptance criteria:

  AC1 — Gate 1 fail (run_count < 10) is stored with preliminary=True.
  AC3 — Gate 2 fail is stored with preliminary=True; preliminary_reason kept.
  AC4 — Gate 3 fail (inferred step) is stored with preliminary=True.
  AC5 — all gates pass is stored with preliminary=False / reason None.

Plus the T6-specific guarantees:
  - all 15 columns populated; preliminary/preliminary_reason mirror the gate;
  - gate_run_count is the live count threaded out of T5 (never re-estimated);
  - the inferred column uses the SAME detector as Gate 3;
  - confidence is the deterministic composite, clamped to [0.5, 1.0];
  - causal.hypothesis_generated fires only after a successful commit, with the
    exact payload fields — and NOT at all when the write raises.

The DB write and telemetry sink are monkeypatched, so these tests are fast and
need no database. A real round-trip against the migrated schema lives in
tests/contract/test_causal_storage.py.
"""
from __future__ import annotations

import json

import pytest

from app.causal_engine import (
    GATE1_MIN_RUN_COUNT,
    CausalContext,
    EntityNode,
    GateResult,
    GraphNeighbourhood,
    compute_causal_confidence,
    store_causal_hypothesis,
    _evidence_links_from_context,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

_OBSERVED_CHAIN = [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "Commercial Credit team capacity was not scaled [OBSERVED via OwnerId].",
    "Covenant review queue backed up [OBSERVED: avg 23 days overdue].",
]

_INFERRED_CHAIN = [
    "Commercial Credit accounts for 62% of SLA breaches [OBSERVED: ServiceNow].",
    "The same team has the highest Jira open issue count [OBSERVED: Jira].",
    "[inferred: 0.6] Backlog pressure from Jira is reducing the team's capacity.",
]

_FALSIFIABILITY = (
    "If covenant review completion rate does not improve within 90 days when loan "
    "origination volume returns to the 90-day baseline, the hypothesis is wrong."
)

_PARSED = {"cause_chain": _OBSERVED_CHAIN, "falsifiability_condition": _FALSIFIABILITY}


def _node(entity_id, entity_type="process", resolution_status="resolved"):
    return EntityNode(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=entity_id,
        resolution_status=resolution_status,
        org_id="org1",
    )


def _context(entities=None, dependency_paths=None, temporal_support=None):
    ents = entities if entities is not None else [
        _node("e1", "process"),
        _node("e2", "person"),
        _node("e3", "process"),
    ]
    return CausalContext(
        graph_context=GraphNeighbourhood(entities=ents, edges=[]),
        dependency_paths=dependency_paths if dependency_paths is not None else [["e1", "e3"]],
        temporal_support=temporal_support if temporal_support is not None else {},
    )


@pytest.fixture(autouse=True)
def _no_db_source_systems(monkeypatch):
    """Keep unit tests DB-free: corroboration lookup returns [] unless overridden."""
    monkeypatch.setattr(
        "app.causal_engine._distinct_source_systems", lambda org_id, ids: []
    )


@pytest.fixture
def capture_insert(monkeypatch):
    """Capture the row dict passed to the INSERT (write succeeds, no real DB)."""
    captured: dict = {}

    def fake_insert(row):
        captured["row"] = row

    monkeypatch.setattr("app.causal_engine._insert_causal_hypothesis", fake_insert)
    return captured


@pytest.fixture
def capture_events(monkeypatch):
    """Spy on telemetry. _emit does `from app.telemetry import record_event`, so
    patching the attribute on app.telemetry is observed at call time."""
    events: list = []

    def fake_record(event_type, payload=None):
        events.append((event_type, dict(payload or {})))

    monkeypatch.setattr("app.telemetry.record_event", fake_record)
    return events


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — confirmed (all gates pass)
# ─────────────────────────────────────────────────────────────────────────────

def test_store_confirmed_returns_id_and_populates_all_columns(capture_insert, capture_events):
    gate = GateResult(False, None, 12)
    row_id = store_causal_hypothesis("org1", "opp1", "run1", _PARSED, gate, _context())

    assert isinstance(row_id, str) and row_id
    row = capture_insert["row"]

    # Every column is present and correctly populated.
    assert row["id"] == row_id
    assert row["org_id"] == "org1"
    assert row["opportunity_id"] == "opp1"
    assert row["run_id"] == "run1"
    assert json.loads(row["cause_chain"]) == _OBSERVED_CHAIN
    assert json.loads(row["evidence_links"]) == ["e1", "e2", "e3"]
    assert row["falsifiability_condition"] == _FALSIFIABILITY
    assert row["preliminary"] == 0
    assert row["preliminary_reason"] is None
    assert row["gate_run_count"] == 12
    assert row["generated_by"] == "llm"
    assert row["inferred"] == 0
    assert 0.5 <= row["confidence"] <= 1.0
    assert row["created_at"]


def test_confirmed_emits_generated_event_after_write(capture_insert, capture_events):
    gate = GateResult(False, None, 12)
    store_causal_hypothesis("org1", "opp1", "run1", _PARSED, gate, _context())

    assert len(capture_events) == 1
    event_type, payload = capture_events[0]
    assert event_type == "causal.hypothesis_generated"
    assert payload["preliminary"] is False
    assert payload["confidence"] == capture_insert["row"]["confidence"]
    assert payload["gate_run_count"] == 12
    assert payload["inferred"] is False


# ─────────────────────────────────────────────────────────────────────────────
# AC1 / AC3 / AC4 — preliminary rows mirror the gate result
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "reason,run_count",
    [
        ("gate1_insufficient_run_count: 7 of 10 runs completed", 7),    # AC1
        ("gate2_unresolved_entities: 2 entities require resolution", 12),  # AC3
        ("gate3_inferred_primary_step: step 3", 12),                    # AC4
    ],
)
def test_store_preliminary_mirrors_gate_reason(capture_insert, capture_events, reason, run_count):
    gate = GateResult(True, reason, run_count)
    store_causal_hypothesis("org1", "opp2", "run1", _PARSED, gate, _context())

    row = capture_insert["row"]
    assert row["preliminary"] == 1
    assert row["preliminary_reason"] == reason
    assert row["gate_run_count"] == run_count
    assert capture_events[0][1]["preliminary"] is True


# ─────────────────────────────────────────────────────────────────────────────
# inferred column == Gate 3 detection
# ─────────────────────────────────────────────────────────────────────────────

def test_inferred_column_true_when_chain_has_inferred_step(capture_insert, capture_events):
    parsed = {"cause_chain": _INFERRED_CHAIN, "falsifiability_condition": _FALSIFIABILITY}
    gate = GateResult(True, "gate3_inferred_primary_step: step 3", 12)
    store_causal_hypothesis("org1", "opp3", "run1", parsed, gate, _context())

    assert capture_insert["row"]["inferred"] == 1
    assert capture_events[0][1]["inferred"] is True


def test_inferred_column_false_for_observed_chain(capture_insert, capture_events):
    gate = GateResult(False, None, 12)
    store_causal_hypothesis("org1", "opp4", "run1", _PARSED, gate, _context())
    assert capture_insert["row"]["inferred"] == 0


def test_inferred_can_be_true_while_preliminary_false(capture_insert, capture_events):
    """Permanent metadata flag is distinct from the gate: a chain may carry an
    [inferred:] label yet still be confirmed (preliminary=False)."""
    parsed = {"cause_chain": _INFERRED_CHAIN, "falsifiability_condition": _FALSIFIABILITY}
    gate = GateResult(False, None, 12)  # gates passed, but the chain has an inferred step
    store_causal_hypothesis("org1", "opp4b", "run1", parsed, gate, _context())
    row = capture_insert["row"]
    assert row["preliminary"] == 0
    assert row["inferred"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# gate_run_count threaded from T5 (never re-estimated)
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_run_count_is_threaded_from_gate_result(capture_insert, capture_events):
    gate = GateResult(False, None, 15)
    store_causal_hypothesis("org1", "opp5", "run1", _PARSED, gate, _context())
    assert capture_insert["row"]["gate_run_count"] == 15
    assert capture_events[0][1]["gate_run_count"] == 15


# ─────────────────────────────────────────────────────────────────────────────
# temporal_support serialised
# ─────────────────────────────────────────────────────────────────────────────

def test_temporal_support_is_json_serialised(capture_insert, capture_events):
    ts = {"svc::e1::metric_value": {"trend": "rising", "anomaly": True, "context": "ctx", "run_count": 12}}
    gate = GateResult(False, None, 12)
    store_causal_hypothesis("org1", "opp8", "run1", _PARSED, gate, _context(temporal_support=ts))
    assert json.loads(capture_insert["row"]["temporal_support"]) == ts


# ─────────────────────────────────────────────────────────────────────────────
# Failed write: no telemetry, no crash (graceful degradation)
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_write_returns_none_and_emits_no_event(monkeypatch, capture_events):
    def boom(row):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr("app.causal_engine._insert_causal_hypothesis", boom)
    gate = GateResult(False, None, 12)

    result = store_causal_hypothesis("org1", "opp6", "run1", _PARSED, gate, _context())

    assert result is None              # degrades gracefully, does not raise
    assert capture_events == []        # no event fired on a failed write


# ─────────────────────────────────────────────────────────────────────────────
# Generated-event payload: exact fields + types (CausalHypothesisGeneratedPayload)
# ─────────────────────────────────────────────────────────────────────────────

def test_generated_event_payload_exact_fields(capture_insert, capture_events):
    gate = GateResult(False, None, 12)
    store_causal_hypothesis("org1", "opp7", "run1", _PARSED, gate, _context())

    event_type, payload = capture_events[0]
    assert event_type == "causal.hypothesis_generated"
    assert set(payload.keys()) == {
        "org_id", "run_id", "opportunity_id",
        "preliminary", "confidence", "gate_run_count", "inferred",
    }
    assert isinstance(payload["preliminary"], bool)
    assert isinstance(payload["confidence"], float)
    assert isinstance(payload["gate_run_count"], int)
    assert isinstance(payload["inferred"], bool)
    # PII guard: no hypothesis text leaks into telemetry.
    assert "cause_chain" not in payload
    assert "falsifiability_condition" not in payload


# ─────────────────────────────────────────────────────────────────────────────
# confidence composite (pure)
# ─────────────────────────────────────────────────────────────────────────────

def test_confidence_in_range_and_deterministic():
    ctx = _context()
    c1 = compute_causal_confidence(ctx, ["Salesforce"])
    c2 = compute_causal_confidence(ctx, ["Salesforce"])
    assert c1 == c2
    assert 0.5 <= c1 <= 1.0


def test_confidence_rises_with_corroboration():
    ctx = _context()
    low = compute_causal_confidence(ctx, [])
    high = compute_causal_confidence(ctx, ["Salesforce", "ServiceNow", "Jira"])
    assert 0.5 <= low < high <= 1.0


def test_confidence_rises_with_temporal_maturity():
    ents = [_node("e1", "process"), _node("e2", "process")]
    immature = CausalContext(GraphNeighbourhood(ents, []), [["e1", "e2"]], {})
    mature = CausalContext(
        GraphNeighbourhood(ents, []),
        [["e1", "e2"]],
        {"svc::e1::metric_value": {"run_count": 12}, "svc::e2::metric_value": {"run_count": 15}},
    )
    assert compute_causal_confidence(mature, []) > compute_causal_confidence(immature, [])


def test_confidence_rewards_shorter_dependency_paths():
    ents = [_node(f"e{i}", "process") for i in range(1, 5)]
    short = CausalContext(GraphNeighbourhood(ents, []), [["e1", "e2"]], {})          # 1 hop
    long = CausalContext(GraphNeighbourhood(ents, []), [["e1", "e2", "e3", "e4"]], {})  # 3 hops
    assert compute_causal_confidence(short, []) > compute_causal_confidence(long, [])


def test_confidence_clamped_at_full_strength():
    ents = [_node("e1", "process"), _node("e2", "process")]
    ctx = CausalContext(
        GraphNeighbourhood(ents, []),
        [["e1", "e2"]],  # 1 hop → depth 1.0
        {"svc::e1::metric_value": {"run_count": 20}, "svc::e2::metric_value": {"run_count": 20}},
    )
    c = compute_causal_confidence(ctx, ["Salesforce", "ServiceNow", "Jira", "nCino"])
    assert c == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# evidence_links derivation
# ─────────────────────────────────────────────────────────────────────────────

def test_evidence_links_sorted_and_deduplicated():
    ents = [_node("e3"), _node("e1", "person"), _node("e3")]
    ctx = CausalContext(GraphNeighbourhood(ents, []), [], {})
    assert _evidence_links_from_context(ctx) == ["e1", "e3"]
