"""
R17-A4 / T4 — .NET operational signals are OBSERVED evidence, shaped for
corroboration (AC5).

For a .NET signal to count as first-class support for a finding in another system,
it must be shaped so the corroboration engine can understand it: it must carry the
SOURCE SYSTEM, APPLICATION IDENTITY, SIGNAL TYPE, TIMESTAMP, CONFIDENCE-RELATED
DATA, and PROVENANCE — and the provenance must mark it as directly OBSERVED (from
runtime logs/diagnostics), never inferred. These tests pin exactly that, offline
against the deterministic fixture.
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.dotnet_app import DotNetAppIngestor
from discovery.ingest.dotnet_app_signals import (
    build_dotnet_app_corroboration_payload,
    build_evidence_pointer,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)


class _Store:
    def __init__(self):
        self.data = {}

    def read(self, o, c):
        return self.data.get((o, c))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _all_records(org_id="org-1"):
    collected = []
    store = _Store()
    change_runner.ingest_with_checkpoint(
        DotNetAppIngestor(), org_id,
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    return collected


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — every record carries a valid OBSERVED pointer with source_system=dotnet_app
# ─────────────────────────────────────────────────────────────────────────────
def test_every_record_has_a_valid_observed_pointer():
    records = _all_records()
    assert records
    for r in records:
        ep = r.get("evidence_pointer")
        assert ep is not None, f"missing evidence_pointer on {r['artifact_id']}"
        assert ep["source_system"] == "dotnet_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"]
        assert ep["source_timestamp"]
        assert EvidencePointer.from_dict(ep).is_valid() is True


def test_observed_pointers_carry_no_extraction_job_id():
    # Observed evidence is directly measured — it must NOT be tagged as inferred.
    for r in _all_records():
        assert r["evidence_pointer"].get("extraction_job_id") is None


def test_pointer_artifact_traces_to_app_and_surface():
    for r in _all_records():
        ep = r["evidence_pointer"]
        assert ep["source_artifact"].startswith(f"{r['app_id']}:{r['artifact_kind']}:")


def test_build_evidence_pointer_shape():
    ep = build_evidence_pointer("orders-api", "metrics", "2026-06-10T08:00:00+00:00",
                                "2026-06-10T08:00:00+00:00")
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert ep["source_artifact"] == "orders-api:metrics:2026-06-10T08:00:00+00:00"
    assert ep["source_artifact_type"] == "record_id"


def test_build_evidence_pointer_falls_back_to_now_when_ts_missing():
    ep = build_evidence_pointer("svc", "log", "7", None)
    assert ep["source_timestamp"]   # mandatory spine always populated


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — the corroboration payload carries everything the engine needs to reason
# ─────────────────────────────────────────────────────────────────────────────
def test_corroboration_payload_carries_the_engine_understandable_shape():
    payload = build_dotnet_app_corroboration_payload(_all_records())

    # Source system: the payload key the engine keys COR-10 off.
    assert "dotnet_app" in payload
    block = payload["dotnet_app"]

    friction = block["operational_friction"]
    # fired + confidence-related friction; timestamp for windowing; signal type
    # (reasons); application identity (the affected service).
    assert friction["fired"] is True
    assert friction["timestamp"]                         # timestamp
    assert "orders" in friction["services"]              # application identity
    assert friction["reasons"]                           # signal type(s)

    # Per-service rollup carries the confidence-related gauges + signal families.
    orders = block["services"]["orders"]
    assert orders["metrics"]["max_error_rate"] >= 0.05
    assert orders["metrics"]["latency_degraded"] is True
    assert orders["metrics"]["heap_pressure"] is True
    assert any(c["is_cluster"] for c in orders["exception_clusters"])


def test_empty_payload_is_still_well_shaped():
    payload = build_dotnet_app_corroboration_payload([])
    assert payload["dotnet_app"]["operational_friction"]["fired"] is False
