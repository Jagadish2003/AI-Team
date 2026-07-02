"""
R17-A4 / T4 + T5 — Contract tests: .NET operational signals into corroboration.

Proves the R17-A4 corroboration slice at the contract layer (the ``contract-tests``
CI gate), exercising the REAL .NET ingestor / signal shaping / corroboration engine
OFFLINE against the deterministic fixture, with in-memory checkpoint and telemetry
seams — no database, no live credentials.

Acceptance criteria covered by this task:
  AC5  every .NET signal carries a valid OBSERVED EvidencePointer
       (source_system='dotnet_app', artifact id, timestamp, origin='observed'),
       and the corroboration payload is shaped so the engine can understand it.
  AC6  a .NET-app operational signal corroborates a finding in another connected
       system and contributes to confidence — the SAME cross-system corroboration
       approach as every other source (no separate .NET confidence model).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor10_dotnet_app_operational,
    evaluate_corroboration,
)
from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import change_runner, clear_live_connectors
from discovery.ingest.dotnet_app import DotNetAppIngestor
from discovery.ingest.dotnet_app_signals import (
    build_dotnet_app_corroboration_payload,
    build_evidence_pointer,
)

ORG = "org-a4-cor"
DETECTOR = "ORDERS_PROCESS_FRICTION"
RUN_TS = datetime(2026, 6, 20, tzinfo=timezone.utc)
FRESH_TS = "2026-06-10T08:10:00+00:00"
STALE_TS = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)
    clear_live_connectors()
    yield
    clear_live_connectors()


def _ingest(org_id=ORG):
    collected: list = []
    store: dict = {}
    change_runner.ingest_with_checkpoint(
        DotNetAppIngestor(), org_id,
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=lambda o, c: store.get((o, c)),
        save_checkpoint=lambda cp: store.__setitem__((cp.org_id, cp.connector_id), cp),
    )
    return collected


def _dotnet_block(fired=True, ts=FRESH_TS, services=("orders",)):
    return {"dotnet_app": {"operational_friction": {
        "fired": fired, "timestamp": ts, "services": list(services),
        "reasons": ["elevated error rate", "latency degradation"],
    }}}


def _run_data(connected, **systems):
    data = {"connected_systems": list(connected)}
    data.update(systems)
    return data


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — OBSERVED provenance + a corroboration-ready signal shape
# ═════════════════════════════════════════════════════════════════════════════
def test_ac5_every_record_carries_a_valid_observed_pointer():
    records = _ingest()
    assert records
    for r in records:
        ep = r["evidence_pointer"]
        assert ep["source_system"] == "dotnet_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"] and ep["source_timestamp"]
        assert EvidencePointer.from_dict(ep).is_valid() is True
        assert ep.get("extraction_job_id") is None      # observed, never inferred


def test_ac5_builder_produces_observed_dotnet_pointer():
    ep = build_evidence_pointer("orders-api", "metrics", FRESH_TS, FRESH_TS)
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert EvidencePointer.from_dict(ep).is_valid() is True


def test_ac5_corroboration_payload_is_engine_understandable():
    block = build_dotnet_app_corroboration_payload(_ingest())["dotnet_app"]
    friction = block["operational_friction"]
    # source system (key) + application identity + signal type + timestamp + fired.
    assert friction["fired"] is True
    assert "orders" in friction["services"]
    assert friction["reasons"]
    assert friction["timestamp"]


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — .NET signal corroborates another system and contributes to confidence
# ═════════════════════════════════════════════════════════════════════════════
def test_ac6_cor10_fires_for_fresh_friction_and_respects_window():
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block()), RUN_TS) is True
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block(ts=STALE_TS)), RUN_TS) is False
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block(fired=False)), RUN_TS) is False


def test_ac6_real_signal_elevates_with_another_system():
    payload = build_dotnet_app_corroboration_payload(_ingest())
    rd = build_corroboration_run_data(
        systems={"salesforce", "dotnet_app"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert "COR-10" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"


def test_ac6_corroborates_a_servicenow_incident_spike():
    # Ticket system AND running application both show the same problem → HIGH.
    rd = _run_data(
        ["servicenow", "dotnet_app"],
        servicenow={"incidents": [{"state": "Open", "sys_created_on": FRESH_TS, "detector_ids": [DETECTOR]}]},
        **_dotnet_block(),
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert {"COR-01", "COR-10"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"


def test_ac6_single_source_dotnet_does_not_self_corroborate():
    # No separate .NET confidence model: a lone .NET source stays MEDIUM (COR-08).
    rd = _run_data(["dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


def test_ac6_elevation_never_downgrades_a_scorer_baseline():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"
