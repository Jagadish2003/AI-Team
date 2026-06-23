"""Contract tests for T6 — store_causal_hypothesis() persistence (ENT-6 / T3-S16-A).

A real round-trip against the migrated causal_hypotheses schema (the conftest
runs alembic upgrade head + seed into an isolated temp DB). Proves the
parameterised INSERT matches the locked T1 DDL and that every column reads back
correctly for both the confirmed and preliminary cases.
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.causal_engine import (
    CausalContext,
    EntityNode,
    GateResult,
    GraphNeighbourhood,
    store_causal_hypothesis,
)

_FALSIFIABILITY = (
    "If covenant review completion rate does not improve within 90 days when loan "
    "origination volume returns to the 90-day baseline, the hypothesis is wrong."
)

_CHAIN = [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "Commercial Credit team capacity was not scaled [OBSERVED via OwnerId].",
    "Covenant review queue backed up [OBSERVED: avg 23 days overdue].",
]

_PARSED = {"cause_chain": _CHAIN, "falsifiability_condition": _FALSIFIABILITY}


def _context():
    ents = [
        EntityNode("ce1", "process", "Covenant Review", "resolved", "default"),
        EntityNode("ce2", "person", "Sarah Chen", "resolved", "default"),
        EntityNode("ce3", "process", "Credit Review", "resolved", "default"),
    ]
    return CausalContext(
        graph_context=GraphNeighbourhood(entities=ents, edges=[]),
        dependency_paths=[["ce1", "ce3"]],
        temporal_support={"svc::ce1::metric_value": {"trend": "rising", "run_count": 12}},
    )


def _read_row(row_id: str) -> dict | None:
    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM causal_hypotheses WHERE id = %s", (row_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_store_confirmed_hypothesis_round_trip():
    gate = GateResult(False, None, 12)
    row_id = store_causal_hypothesis("default", "copp1", "crun1", _PARSED, gate, _context())

    assert isinstance(row_id, str) and row_id
    row = _read_row(row_id)
    assert row is not None

    assert row["org_id"] == "default"
    assert row["opportunity_id"] == "copp1"
    assert row["run_id"] == "crun1"
    assert json.loads(row["cause_chain"]) == _CHAIN
    assert json.loads(row["evidence_links"]) == ["ce1", "ce2", "ce3"]
    assert json.loads(row["temporal_support"]) == {
        "svc::ce1::metric_value": {"trend": "rising", "run_count": 12}
    }
    assert row["falsifiability_condition"] == _FALSIFIABILITY
    assert row["preliminary"] is False
    assert row["preliminary_reason"] is None
    assert row["gate_run_count"] == 12
    assert row["generated_by"] == "llm"
    assert row["inferred"] is False
    assert 0.5 <= row["confidence"] <= 1.0
    assert row["created_at"]


def test_store_preliminary_hypothesis_round_trip():
    reason = "gate1_insufficient_run_count: 7 of 10 runs completed"
    gate = GateResult(True, reason, 7)
    row_id = store_causal_hypothesis("default", "copp2", "crun1", _PARSED, gate, _context())

    row = _read_row(row_id)
    assert row is not None
    assert row["preliminary"] is True
    assert row["preliminary_reason"] == reason
    assert row["gate_run_count"] == 7


def test_store_inferred_chain_sets_inferred_column():
    parsed = {
        "cause_chain": [
            "Commercial Credit accounts for 62% of SLA breaches [OBSERVED].",
            "[inferred: 0.6] Backlog pressure from Jira is reducing capacity.",
        ],
        "falsifiability_condition": _FALSIFIABILITY,
    }
    gate = GateResult(True, "gate3_inferred_primary_step: step 2", 12)
    row_id = store_causal_hypothesis("default", "copp3", "crun1", parsed, gate, _context())

    row = _read_row(row_id)
    assert row is not None
    assert row["inferred"] is True


def test_generated_event_persisted_after_commit():
    """causal.hypothesis_generated is written to telemetry_events on success."""
    gate = GateResult(False, None, 11)
    row_id = store_causal_hypothesis("default", "copp4", "crun1", _PARSED, gate, _context())
    assert row_id

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT payload FROM telemetry_events "
            "WHERE event_type = 'causal.hypothesis_generated' AND run_id = 'crun1'"
        ).fetchall()
    finally:
        conn.close()

    payloads = [json.loads(r["payload"]) for r in rows]
    match = [p for p in payloads if p.get("opportunity_id") == "copp4"]
    assert match, "expected a causal.hypothesis_generated telemetry row for copp4"
    assert match[0]["gate_run_count"] == 11
    assert match[0]["preliminary"] is False
