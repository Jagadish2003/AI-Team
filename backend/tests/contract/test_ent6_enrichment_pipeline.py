"""Contract test for ENT-6 runtime wiring through LLM enrichment."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import db
from app.llm_enrichment import run_llm_enrichment
from app.relationship_mapper import upsert_relationship
from app.temporal import ensure_signal_snapshots_table
from database.models.entities import Entity


def _db_path() -> str:
    return os.environ["DB_PATH"]


def _insert_entity(
    org_id: str,
    run_id: str,
    display_name: str,
    *,
    entity_type: str = "process",
    source_record_id: str | None = None,
) -> str:
    entity = Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower(),
        display_name=display_name,
        source_system="agentiq",
        source_record_id=source_record_id or display_name,
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=3,
    )
    row = entity.to_db_row()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (
                :id, :org_id, :entity_type, :canonical_name, :display_name,
                :source_system, :source_record_id, :resolution_confidence,
                :resolution_status, :first_seen_run_id, :last_seen_run_id,
                :run_count, :metadata, :created_at, :updated_at
            )
            """,
            row,
        )
        conn.commit()
    return row["id"]


def _insert_signal_history(org_id: str, detector_id: str, pack_id: str = "svc") -> None:
    ensure_signal_snapshots_table()
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signal_key = f"{pack_id}::{detector_id}::metric_value"
    rows = []
    for index in range(10):
        value = 20 + index
        rows.append(
            (
                str(uuid4()),
                org_id,
                f"hist-run-{index}",
                pack_id,
                detector_id,
                signal_key,
                "metric_value",
                float(value),
                15.0,
                1,
                "test",
                (captured_at + timedelta(days=index)).isoformat(),
                None,
                None,
                None,
                None,
            )
        )
    with sqlite3.connect(_db_path()) as conn:
        conn.executemany(
            """
            INSERT INTO signal_snapshots (
                id, org_id, run_id, pack_id, detector_id, signal_key,
                metric_name, metric_value, threshold, fired, signal_source,
                captured_at, baseline_mean, baseline_stddev,
                baseline_window_days, baseline_calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()


def _latest_causal_row(org_id: str, opportunity_id: str) -> dict | None:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM causal_hypotheses
            WHERE org_id = ? AND opportunity_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (org_id, opportunity_id),
        ).fetchone()
    return dict(row) if row else None


def test_llm_enrichment_stores_valid_causal_hypothesis(monkeypatch):
    org_id = f"org-ent6-pipeline-{uuid4().hex[:8]}"
    run_id = f"run-ent6-pipeline-{uuid4().hex[:8]}"
    opp_id = "opp-ent6-pipeline"
    pack_id = "svc"

    db.upsert_run(run_id, {"id": run_id, "org_id": org_id})

    detector_a = _insert_entity(org_id, run_id, "detector_a")
    detector_b = _insert_entity(org_id, run_id, "detector_b")
    detector_c = _insert_entity(org_id, run_id, "detector_c")
    upsert_relationship(
        org_id=org_id,
        from_entity_id=detector_a,
        to_entity_id=detector_b,
        relationship_type="depends_on",
        confidence=0.9,
        inferred=False,
        run_id=run_id,
        evidence={"source": "test"},
    )
    upsert_relationship(
        org_id=org_id,
        from_entity_id=detector_b,
        to_entity_id=detector_c,
        relationship_type="routes_to",
        confidence=0.9,
        inferred=False,
        run_id=run_id,
        evidence={"source": "test"},
    )
    _insert_signal_history(org_id, "detector_a", pack_id)

    # The UI-visible KV intentionally omits the newly-created process entities
    # below the display run-count threshold. ENT-6 must still seed from DB rows
    # last seen in the run so causal context can reach their relationship edges.
    graph_entities = [
        {
            "entity_id": str(uuid4()),
            "entity_type": "person",
            "display_name": "Visible User",
            "source_system": "salesforce",
            "resolution_confidence": 1.0,
            "resolution_status": "resolved",
            "run_count": 3,
        },
        {
            "entity_id": str(uuid4()),
            "entity_type": "team",
            "display_name": "Visible Team",
            "source_system": "jira",
            "resolution_confidence": 1.0,
            "resolution_status": "resolved",
            "run_count": 3,
        },
        {
            "entity_id": str(uuid4()),
            "entity_type": "system",
            "display_name": "Visible System",
            "source_system": "servicenow",
            "resolution_confidence": 1.0,
            "resolution_status": "resolved",
            "run_count": 3,
        },
    ]
    db.run_kv_set("entities", run_id, graph_entities)

    llm_payload = {
        "aiSummary": "Queue depth is rising across the approval process.",
        "aiWhyBullets": [
            "[OBSERVED] detector_a has 10 runs of signal history.",
            "[OBSERVED] detector_b is connected by observed process relationships.",
            "[OBSERVED] detector_c is downstream in the process graph.",
        ],
        "aiRisks": ["Cycle time may continue to breach the threshold."],
        "aiSuggestedNextSteps": ["Validate the approval handoff owner."],
        "cause_chain": [
            "queue depth rose 40% above baseline.",
            "approval capacity did not increase across 10 runs.",
            "cycle time breached the threshold for 3 runs.",
        ],
        "falsifiability_condition": (
            "If queue depth stays below 10 for 3 consecutive runs while cycle "
            "time remains stable, this hypothesis is wrong."
        ),
    }

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.llm_enrichment._call_claude",
        lambda prompt, max_tokens: json.dumps(llm_payload),
    )

    result = run_llm_enrichment(
        run_id=run_id,
        opps=[
            {
                "id": opp_id,
                "title": "Approval bottleneck",
                "aiRationale": "Deterministic rationale",
                "_debug": {
                    "detector_id": "detector_a",
                    "metric_value": 29,
                    "threshold": 15,
                },
            }
        ],
        evidence=[],
        sources_analyzed={},
        pack_id=pack_id,
        org_id=org_id,
    )

    per_opp = result["perOpportunity"][opp_id]
    assert per_opp["llmGenerated"] is True
    assert "_causal_llm_response" not in per_opp

    row = _latest_causal_row(org_id, opp_id)
    assert row is not None
    assert json.loads(row["cause_chain"]) == llm_payload["cause_chain"]
    assert row["falsifiability_condition"] == llm_payload["falsifiability_condition"]
    assert row["preliminary"] == 0
    assert row["preliminary_reason"] is None
    assert row["gate_run_count"] == 10
