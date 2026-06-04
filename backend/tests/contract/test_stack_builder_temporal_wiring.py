from __future__ import annotations

from typing import Any

from app import routes_sprint4_t1 as route
from app.llm_enrichment import KV_LLM_ENRICHMENT


def test_stack_builder_compute_flow_writes_temporal_fields(monkeypatch):
    """T3-S11-A T7: active compute flow applies temporal enrichment."""
    stored: dict[str, Any] = {
        KV_LLM_ENRICHMENT: {
            "perOpportunity": {
                "opp_001": {"oppId": "opp_001", "aiSummary": "Existing summary"}
            }
        }
    }
    calls: dict[str, Any] = {}

    def fake_run_kv_get(key, run_id, default=None):
        return stored.get(key, default)

    def fake_run_kv_set(key, run_id, value):
        stored[key] = value

    def fake_calculate_baselines():
        calls["baseline_calculated"] = True

    def fake_enrich(run_id, org_id, pack_id, opps):
        calls["enrich_args"] = (run_id, org_id, pack_id)
        opps[0].update(
            {
                "baseline_context": "Stable - within normal range",
                "trend_direction": "stable",
                "anomaly_score": None,
                "is_anomalous": False,
                "first_deviation": False,
                "baseline_mean": 2.2,
                "run_count": 3,
            }
        )
        return opps

    def fake_record_event(event_type, payload):
        calls["event"] = (event_type, payload)

    monkeypatch.setattr(route.db, "run_kv_get", fake_run_kv_get)
    monkeypatch.setattr(route.db, "run_kv_set", fake_run_kv_set)
    monkeypatch.setattr("app.jobs.baseline_calculator.calculate_baselines", fake_calculate_baselines)
    monkeypatch.setattr(
        "app.temporal_enrichment.enrich_opportunities_with_temporal_context",
        fake_enrich,
    )
    monkeypatch.setattr("app.telemetry.record_event", fake_record_event)
    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id_optional", lambda: None)

    route._apply_temporal_enrichment(
        "run_123",
        {"id": "run_123", "orgId": "demo-org"},
        "service_cloud",
        [{"id": "opp_001", "_debug": {"detector_id": "REPETITIVE_AUTOMATION", "metric_value": 2.2}}],
    )

    enriched = stored[KV_LLM_ENRICHMENT]["perOpportunity"]["opp_001"]
    assert calls["baseline_calculated"] is True
    assert calls["enrich_args"] == ("run_123", "demo-org", "service_cloud")
    assert calls["event"] == (
        "temporal.enrichment_completed",
        {"run_id": "run_123"},
    )
    assert enriched["baseline_context"] == "Stable - within normal range"
    assert enriched["trend_direction"] == "stable"
    assert enriched["run_count"] == 3

