"""
R17-A4 / T1 + T7 — DotNetAppIngestor change-based ingestion (AC1, AC2, AC8).

Binds the acceptance criteria to the REAL ``DotNetAppIngestor`` driven through the
shared change runner, offline against the deterministic ``dotnet_app_sample.json``
fixture. No DB is needed: the checkpoint store is wired through the runner's
in-memory seam and telemetry is captured by monkeypatch.

  * AC1 — the ingestor reads a configured app's health/diagnostics endpoints and
    logs and produces operational signal from them.
  * AC2 — DotNetAppIngestor implements ChangeBasedIngestor: incremental runs read
    only new logs/samples since the checkpoint; an idle application yields an
    empty/minimal delta; a first load is resumable (streamed as checkpointed
    batches).
  * AC8 — operational surfaces only: records describe metric samples and log
    entries, never application source code.
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.dotnet_app import (
    DotNetAppIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)
from discovery.ingest.dotnet_app_signals import build_dotnet_app_signal

FRESH_TS = "2026-06-10T08:10:00+00:00"

# Deterministic fixture artifact identities (dotnet_app_sample.json).
_ORDERS_METRICS = [f"orders-api:metrics:2026-06-10T08:{m}:00+00:00" for m in ("00", "05", "10")]
_ORDERS_LOGS = [f"orders-api:log:{i}" for i in (1, 2, 3, 4, 5)]
_INVENTORY_METRICS = [f"inventory-svc:metrics:2026-06-10T08:{m}:00+00:00" for m in ("00", "05")]
_INVENTORY_LOGS = [f"inventory-svc:log:{i}" for i in (1, 2)]
_ALL = _ORDERS_METRICS + _ORDERS_LOGS + _INVENTORY_METRICS + _INVENTORY_LOGS


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


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org-1", since) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# Contract / identity
# ─────────────────────────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing = DotNetAppIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "dotnet_app"
    assert ing.source_system == "dotnet_app"
    # Operational artifacts are forward-only; the connector declares it cannot
    # detect deletes rather than faking tombstones (R16-A1 §5).
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    records = _records()
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] == "created"
        assert r["source_system"] == "dotnet_app"
        assert r["artifact_kind"] in ("metrics", "log")


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
    # Health-check + runtime-metric readings are present (normalised).
    assert metric["health"] is not None
    assert metric["error_rate"] is not None
    assert metric["cpu_usage"] is not None
    log = next(r for r in collected if r["artifact_kind"] == "log")
    assert "level" in log and "message" in log


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
    # And the run-level friction rollup names the degraded service only.
    friction = signal["operational_friction"]
    assert friction["fired"] is True
    assert "orders" in friction["services"]
    assert "inventory" not in friction["services"]
    assert "elevated error rate" in friction["reasons"]


def test_checkpoint_advances_and_is_opaque_json():
    res, store, _ = _collect(DotNetAppIngestor(), "org-1")
    assert res.ok and res.checkpoint_advanced
    cp = store.read("org-1", "dotnet_app")
    assert cp is not None
    decoded = _decode_checkpoint(cp.value)
    assert decoded["orders-api"] == {"log_offset": 5, "metrics_ts": FRESH_TS, "metrics_seq": 1}
    assert decoded["inventory-svc"] == {
        "log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00", "metrics_seq": 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental: only new logs/samples; idle app yields empty/minimal delta
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_reads_all_available_operational_data():
    _res, _store, collected = _collect(DotNetAppIngestor(), "org-1")
    assert len(collected) == len(_ALL)   # 5 metric samples + 7 log entries
    assert sorted(r["artifact_id"] for r in collected) == sorted(_ALL)


def test_idle_application_yields_empty_delta_on_second_run():
    res1, store, _ = _collect(DotNetAppIngestor(), "org-1")
    assert res1.records > 0
    head = store.read("org-1", "dotnet_app").value
    res2, _store, collected2 = _collect(DotNetAppIngestor(), "org-1", store=store)
    assert res2.ok
    assert collected2 == []
    assert res2.records == 0
    # The echoed position is unchanged — the checkpoint never regresses.
    assert store.read("org-1", "dotnet_app").value == head


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


def test_incremental_sequence_aware_same_timestamp_consumed():
    # A cursor carrying metrics_seq treats the boundary-timestamp sample as
    # already consumed — it is not re-read, and only strictly newer data returns.
    since = Checkpoint.create("dotnet_app", "org-1", _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00", "metrics_seq": 1},
    }))
    ids = [r["artifact_id"] for r in _records(since)]
    assert sorted(ids) == sorted(
        [f"orders-api:metrics:{FRESH_TS}", "orders-api:log:3", "orders-api:log:4", "orders-api:log:5"]
        + _INVENTORY_METRICS + _INVENTORY_LOGS
    )
    assert "orders-api:log:1" not in ids
    assert "orders-api:metrics:2026-06-10T08:00:00+00:00" not in ids  # older
    assert "orders-api:metrics:2026-06-10T08:05:00+00:00" not in ids  # consumed (seq)


def test_idle_delta_is_single_empty_batch_echoing_position():
    since = Checkpoint.create("dotnet_app", "org-1", _encode_checkpoint({
        "orders-api": {"log_offset": 5, "metrics_ts": FRESH_TS, "metrics_seq": 1},
        "inventory-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00", "metrics_seq": 1},
    }))
    batches = list(DotNetAppIngestor().ingest_changes("org-1", since))
    assert len(batches) == 1
    assert batches[0].is_empty and batches[0].is_complete


def test_checkpoint_round_trips_idempotently():
    store = Store()
    _drive(DotNetAppIngestor(), "org-1", store=store)
    cp = store.read("org-1", "dotnet_app")
    assert all(b.is_empty for b in DotNetAppIngestor().ingest_changes("org-1", cp))


def test_first_load_streams_resumable_batches():
    store = Store()
    res, _ = _drive(DotNetAppIngestor(batch_size=1), "org-1", store=store)
    assert res.ok and res.first_run and res.complete and res.checkpoint_advanced
    assert res.records == len(_ALL) and res.batches == len(_ALL)


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
    forbidden = {"source_code", "ast", "class_body", "repository", "file_path", "diff",
                 "assembly", "il", "repo_path"}
    for r in collected:
        assert r["artifact_kind"] in ("metrics", "log")
        assert forbidden.isdisjoint(r.keys())


def test_ac8_phase_one_boundary_documented():
    import discovery.ingest.dotnet_app as mod

    doc = (mod.__doc__ or "").lower()
    assert "source code" in doc
    assert "1.8" in doc


# ─────────────────────────────────────────────────────────────────────────────
# Batches honour the change-runner contract
# ─────────────────────────────────────────────────────────────────────────────
def test_yields_delta_batches_with_terminal_flag():
    batches = list(DotNetAppIngestor(batch_size=3).ingest_changes("org-1", None))
    assert batches
    assert all(isinstance(b, DeltaBatch) for b in batches)
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
    assert all(b.next_checkpoint for b in batches)
