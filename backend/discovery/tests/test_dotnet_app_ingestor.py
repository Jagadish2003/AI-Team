"""
R17-A4 / T1 — contract & behaviour tests for the .NET application ingestor.

  AC1 — reads a configured .NET app's health/diagnostics endpoints AND its logs,
        producing operational signal (via the shared extraction).
  AC2 — DotNetAppIngestor implements ChangeBasedIngestor: incremental runs read
        only new data since the checkpoint; an idle deployment yields an empty
        delta; a first load is resumable (streamed as checkpointed batches).
  AC8 — operational surfaces only; no application source code.

Runs offline against the deterministic ``dotnet_app_sample.json`` fixture and
drives the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``)
via an in-memory checkpoint store.
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint
from discovery.ingest.dotnet_app import (
    DotNetAppIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
    build_dotnet_app_signal,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org1", since) for r in b.records]


_ORDERS_METRICS = [f"orders-api:metrics:2026-06-20T08:{m}:00+00:00" for m in ("00", "05", "10")]
_ORDERS_LOGS = [f"orders-api:log:{i}" for i in (1, 2, 3, 4, 5)]
_CATALOG_METRICS = [f"catalog-api:metrics:2026-06-20T08:{m}:00+00:00" for m in ("00", "05")]
_CATALOG_LOGS = [f"catalog-api:log:{i}" for i in (1, 2)]
_ALL = _ORDERS_METRICS + _ORDERS_LOGS + _CATALOG_METRICS + _CATALOG_LOGS


# ── contract / shape ──────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing = DotNetAppIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "dotnet_app"
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    records = _records()
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] == "created"
        assert r["source_system"] == "dotnet_app"
        assert r["artifact_kind"] in ("metrics", "log")


# ── AC1 — reads both surfaces and produces signal ──────────────────────────────
def test_ac1_reads_health_diagnostics_and_logs():
    records = _records()
    kinds = {r["artifact_kind"] for r in records}
    assert kinds == {"metrics", "log"}
    metric = next(r for r in records if r["artifact_kind"] == "metrics")
    # Health-check + runtime-metric readings are present (normalised).
    assert metric["health"] is not None
    assert metric["error_rate"] is not None
    assert metric["system_cpu_usage"] is not None
    log = next(r for r in records if r["artifact_kind"] == "log")
    assert "level" in log and "message" in log


def test_ac1_produces_operational_signal():
    signal = build_dotnet_app_signal(_records())
    friction = signal["operational_friction"]
    assert friction["fired"] is True
    assert "orders" in friction["services"]      # degraded service fires
    assert "catalog" not in friction["services"]  # healthy service does not
    assert "elevated error rate" in friction["reasons"]


# ── AC2 — incremental / idle / resumable ───────────────────────────────────────
def test_ac2_first_run_loads_all_and_advances_checkpoint():
    store = Store()
    seen: list = []
    res = _drive(DotNetAppIngestor(), "org1", store,
                 process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records))
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    cursors = _decode_checkpoint(store.read("org1", "dotnet_app").value)
    assert cursors == {
        "orders-api": {"log_offset": 5, "metrics_ts": "2026-06-20T08:10:00+00:00", "metrics_seq": 1},
        "catalog-api": {"log_offset": 2, "metrics_ts": "2026-06-20T08:05:00+00:00", "metrics_seq": 1},
    }


def test_ac2_incremental_returns_only_newer():
    since = Checkpoint.create("dotnet_app", "org1", _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-20T08:05:00+00:00", "metrics_seq": 1},
    }))
    ids = [r["artifact_id"] for r in _records(since)]
    assert sorted(ids) == sorted(
        ["orders-api:metrics:2026-06-20T08:10:00+00:00",
         "orders-api:log:3", "orders-api:log:4", "orders-api:log:5"]
        + _CATALOG_METRICS + _CATALOG_LOGS
    )
    assert "orders-api:log:1" not in ids
    assert "orders-api:metrics:2026-06-20T08:00:00+00:00" not in ids  # older
    assert "orders-api:metrics:2026-06-20T08:05:00+00:00" not in ids  # consumed (seq)


def test_ac2_idle_deployment_yields_empty_delta_echoing_position():
    store = Store()
    _drive(DotNetAppIngestor(), "org1", store)
    head = store.read("org1", "dotnet_app").value
    res = _drive(DotNetAppIngestor(), "org1", store)
    assert res.ok and res.records == 0
    assert store.read("org1", "dotnet_app").value == head


def test_ac2_idle_delta_is_single_empty_batch():
    since = Checkpoint.create("dotnet_app", "org1", _encode_checkpoint({
        "orders-api": {"log_offset": 5, "metrics_ts": "2026-06-20T08:10:00+00:00", "metrics_seq": 1},
        "catalog-api": {"log_offset": 2, "metrics_ts": "2026-06-20T08:05:00+00:00", "metrics_seq": 1},
    }))
    batches = list(DotNetAppIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].is_empty and batches[0].is_complete


def test_ac2_checkpoint_round_trips_idempotently():
    store = Store()
    _drive(DotNetAppIngestor(), "org1", store)
    cp = store.read("org1", "dotnet_app")
    assert all(b.is_empty for b in DotNetAppIngestor().ingest_changes("org1", cp))


def test_ac2_first_load_streams_resumable_batches():
    store = Store()
    res = _drive(DotNetAppIngestor(batch_size=1), "org1", store)
    assert res.ok and res.first_run and res.complete and res.checkpoint_advanced
    assert res.records == len(_ALL) and res.batches == len(_ALL)


def test_ac2_exactly_one_terminal_batch():
    batches = list(DotNetAppIngestor(batch_size=3).ingest_changes("org1", None))
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
    assert all(b.next_checkpoint for b in batches)


# ── AC8 — operational surfaces only, no source code ────────────────────────────
def test_ac8_only_operational_surfaces_emitted():
    for r in _records():
        assert r["artifact_kind"] in ("metrics", "log")
        assert set(r).isdisjoint({"source_code", "assembly", "il", "repo_path", "ast"})


def test_ac8_phase_one_boundary_documented():
    import discovery.ingest.dotnet_app as mod

    doc = (mod.__doc__ or "").lower()
    assert "source code" in doc
    assert "1.8" in doc
