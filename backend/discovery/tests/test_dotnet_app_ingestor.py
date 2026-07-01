"""
R17-A4 / T1 + T7 — DotNetAppIngestor change-based ingestion (AC1, AC2, AC8).

Binds the acceptance criteria to the REAL ``DotNetAppIngestor`` driven through the
shared change runner, offline against the deterministic ``dotnet_app_sample.json``
fixture. No DB is needed: the checkpoint store is wired through the runner's
in-memory seam and telemetry is captured by monkeypatch.

  * AC1 — the ingestor reads a configured app's health/diagnostics endpoints and
    logs and produces operational signal from them.
  * AC2 — incremental runs read only new logs/samples since the checkpoint; an
    idle application yields an empty/minimal delta.
  * AC8 — operational surfaces only: records describe metric samples and log
    entries, never application source code.
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint, DeltaBatch
from discovery.ingest.dotnet_app import (
    DotNetAppIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)
from discovery.ingest.dotnet_app_signals import build_dotnet_app_signal

FRESH_TS = "2026-06-10T08:10:00+00:00"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real ingestor reads the deterministic fixture."""
    monkeypatch.setenv("INGEST_MODE", "offline")


@pytest.fixture(autouse=True)
def _silence_telemetry(monkeypatch):
    """Swallow artifact_changed emission so these tests need no DB."""
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store=None, **kw):
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )
    return res, store


def _collect(ingestor, org_id, store=None):
    collected: list = []
    res, store = _drive(
        ingestor, org_id, store=store, process_batch=lambda b: collected.extend(b.records)
    )
    return res, store, collected


# ─────────────────────────────────────────────────────────────────────────────
# Contract / identity
# ─────────────────────────────────────────────────────────────────────────────
def test_connector_id_and_delete_capability():
    ing = DotNetAppIngestor()
    assert ing.connector_id == "dotnet_app"
    assert ing.source_system == "dotnet_app"
    # Operational artifacts are forward-only; the connector declares it cannot
    # detect deletes rather than faking tombstones (R16-A1 §5).
    assert ing.reports_deletes is False


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — reads health/diagnostics endpoints + logs, produces operational signal
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_reads_metrics_and_logs():
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1")
    kinds = {r["artifact_kind"] for r in collected}
    assert kinds == {"metrics", "log"}
    assert {r["app_id"] for r in collected} == {"orders-api", "inventory-svc"}
    # The surfaces read are exactly the configured target endpoints (not discovered).
    metric = next(r for r in collected if r["artifact_kind"] == "metrics" and r["app_id"] == "orders-api")
    assert metric["diagnostics_url"] == "https://orders.internal.example/diagnostics"


def test_records_carry_operational_signal():
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1")
    # Signal is a WINDOW operation over the whole delta (T2), not per single
    # sample: the degrading orders service fires and the healthy inventory does not.
    signal = build_dotnet_app_signal(collected)
    assert signal["services"]["orders"]["fired"] is True
    assert signal["services"]["inventory"]["fired"] is False
    # The friction is derived from the four operational signal families.
    m = signal["services"]["orders"]["metrics"]
    assert m["max_error_rate"] >= 0.05
    assert m["latency_degraded"] is True
    assert m["throughput_declined"] is True
    assert m["heap_pressure"] is True
    assert any(c["is_cluster"] for c in signal["services"]["orders"]["exception_clusters"])


def test_checkpoint_advances_and_is_opaque_json():
    res, store, _ = _collect(DotNetAppIngestor(), "org-1")
    assert res.ok and res.checkpoint_advanced
    cp = store.read("org-1", "dotnet_app")
    assert cp is not None
    decoded = _decode_checkpoint(cp.value)
    assert decoded["orders-api"] == {"log_offset": 5, "metrics_ts": FRESH_TS, "metrics_seq": 1}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental: only new logs/samples; idle app yields empty/minimal delta
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_reads_all_available_operational_data():
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1")
    assert len(collected) == 12          # 5 metric samples + 7 log entries


def test_idle_application_yields_empty_delta_on_second_run():
    res1, store, _ = _collect(DotNetAppIngestor(), "org-1")
    assert res1.records > 0
    res2, _store, collected2 = _collect(DotNetAppIngestor(), "org-1", store=store)
    assert res2.ok
    assert collected2 == []
    assert res2.records == 0


def test_incremental_reads_only_new_records():
    # Start mid-stream: orders-api already read up to log offset 2 and metrics
    # sample 08:05; inventory-svc never seen.
    store = Store()
    store.save(
        Checkpoint.create(
            "dotnet_app", "org-1",
            _encode_checkpoint(
                {"orders-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"}}
            ),
        )
    )
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1", store=store)
    orders_logs = {
        r["log_offset"] for r in collected
        if r["app_id"] == "orders-api" and r["artifact_kind"] == "log"
    }
    assert orders_logs == {3, 4, 5}
    orders_metric_ts = {
        r["observed_ts"] for r in collected
        if r["app_id"] == "orders-api" and r["artifact_kind"] == "metrics"
    }
    assert orders_metric_ts == {FRESH_TS}                # strictly > 08:05
    # inventory-svc (absent from the cursor) is read from the beginning.
    assert any(r["app_id"] == "inventory-svc" for r in collected)


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not-json") == {}
    assert _decode_checkpoint('{"v":1}') == {}   # no apps key


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — operational surface only, never source code
# ─────────────────────────────────────────────────────────────────────────────
def test_records_describe_operational_surface_not_source_code():
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1")
    forbidden = {"source_code", "ast", "class_body", "repository", "file_path", "diff"}
    for r in collected:
        assert r["artifact_kind"] in ("metrics", "log")
        assert forbidden.isdisjoint(r.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Batches honour the change-runner contract
# ─────────────────────────────────────────────────────────────────────────────
def test_yields_delta_batches_with_terminal_flag():
    batches = list(DotNetAppIngestor().ingest_changes("org-1", None))
    assert batches
    assert all(isinstance(b, DeltaBatch) for b in batches)
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
