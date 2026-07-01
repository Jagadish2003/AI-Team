"""
R17-A4 / T1 — DotNetAppIngestor produces the operational signals corroboration
consumes.

Ground the corroboration tests in a real signal producer: the change-based
ingestor reads a configured .NET application's health/diagnostics + logs offline
against the deterministic fixture and yields provenance-stamped records. No DB is
needed — the checkpoint store is wired through the runner's in-memory seam and
telemetry is silenced.
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
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, o, c):
        return self.data.get((o, c))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _collect(org_id="org-1", store=None):
    collected: list = []
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        DotNetAppIngestor(), org_id,
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    return res, store, collected


def test_connector_identity():
    ing = DotNetAppIngestor()
    assert ing.connector_id == "dotnet_app"
    assert ing.reports_deletes is False


def test_first_run_reads_metrics_and_logs():
    _res, _store, collected = _collect()
    assert {r["artifact_kind"] for r in collected} == {"metrics", "log"}
    assert {r["app_id"] for r in collected} == {"orders-api", "inventory-svc"}
    assert len(collected) == 12          # 5 metric samples + 7 log entries


def test_records_produce_operational_signal():
    _res, _store, collected = _collect()
    # Signal is a WINDOW operation over the whole delta: the degrading orders
    # service fires, the healthy inventory service does not.
    signal = build_dotnet_app_signal(collected)
    assert signal["services"]["orders"]["fired"] is True
    assert signal["services"]["inventory"]["fired"] is False


def test_checkpoint_advances_and_is_opaque():
    res, store, _ = _collect()
    assert res.ok and res.checkpoint_advanced
    cp = store.read("org-1", "dotnet_app")
    decoded = _decode_checkpoint(cp.value)
    assert decoded["orders-api"] == {"log_offset": 5, "metrics_ts": FRESH_TS}


def test_idle_second_run_yields_empty_delta():
    res1, store, _ = _collect()
    assert res1.records > 0
    res2, _store, collected2 = _collect(store=store)
    assert collected2 == []
    assert res2.records == 0


def test_incremental_reads_only_new_records():
    store = Store()
    store.save(Checkpoint.create(
        "dotnet_app", "org-1",
        _encode_checkpoint({"orders-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"}}),
    ))
    _res, _store, collected = _collect(store=store)
    orders_logs = {r["log_offset"] for r in collected
                   if r["app_id"] == "orders-api" and r["artifact_kind"] == "log"}
    assert orders_logs == {3, 4, 5}
    # inventory-svc (absent from the cursor) is read from the beginning.
    assert any(r["app_id"] == "inventory-svc" for r in collected)


def test_records_are_operational_surface_only():
    _res, _store, collected = _collect()
    forbidden = {"source_code", "ast", "class_body", "repository", "file_path", "diff"}
    for r in collected:
        assert r["artifact_kind"] in ("metrics", "log")
        assert forbidden.isdisjoint(r.keys())


def test_yields_delta_batches_with_one_terminal():
    batches = list(DotNetAppIngestor().ingest_changes("org-1", None))
    assert batches and all(isinstance(b, DeltaBatch) for b in batches)
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
