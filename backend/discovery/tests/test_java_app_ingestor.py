"""
R17-A3 / T1 — contract & behaviour tests for the Java application ingestor.

Covers the acceptance criteria this subtask owns:

  AC1 — the ingestor reads a configured Java application's health/diagnostics
        endpoints (Spring Boot Actuator: health/metrics/info) AND its logs, and
        produces operational signal from them.
  AC2 — JavaAppIngestor implements ChangeBasedIngestor: an incremental run reads
        only new logs/samples since the checkpoint; an idle application yields an
        empty (or minimal) delta. A first run is a resumable, checkpointed load.

Runs offline against the deterministic ``java_app_sample.json`` fixture and drives
the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``)
via an in-memory checkpoint store, so the checkpoint lifecycle is exercised end
to end.
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.java_app import (
    JavaAppIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Pin offline mode so a stray INGEST_MODE=live in .env can't trigger network."""
    monkeypatch.setenv("INGEST_MODE", "offline")


# ── in-memory checkpoint store wired through the runner's injectable seam ─────
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


def _records(batches):
    return [r for b in batches for r in b.records]


# Fixture artifact identities (deterministic).
_PAY_LOGS = [f"payments-api:log:{i}" for i in (1, 2, 3, 4)]
_PAY_ACT = [
    "payments-api:actuator:2026-06-28T09:00:00+00:00",
    "payments-api:actuator:2026-06-28T10:00:00+00:00",
]
_INV_LOGS = ["inventory-api:log:1"]
_INV_ACT = ["inventory-api:actuator:2026-06-28T09:30:00+00:00"]
_ALL = _PAY_LOGS + _PAY_ACT + _INV_LOGS + _INV_ACT


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing = JavaAppIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "java_app"
    # Forward-only polling of logs/samples cannot observe deletions — declared.
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    """Records must carry artifact_id + change_kind so the shared runner can emit
    ingestion.artifact_changed events (handled downstream)."""
    records = _records(JavaAppIngestor().ingest_changes("org1", None))
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] == "created"
        assert r["source_system"] == "java_app"


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — reads BOTH operational surfaces and produces signal
# ─────────────────────────────────────────────────────────────────────────────
def test_ac1_reads_actuator_and_logs_surfaces():
    records = _records(JavaAppIngestor().ingest_changes("org1", None))
    surfaces = {r["surface"] for r in records}
    assert surfaces == {"actuator", "logs"}
    # Actuator records carry the three diagnostics endpoints' data.
    act = next(r for r in records if r["surface"] == "actuator")
    assert "health" in act and "metrics" in act and "info" in act
    assert act["health"].get("status")           # /health read
    assert act["metrics"]                          # /metrics read
    assert act["info"].get("app")                  # /info read


def test_ac1_produces_operational_signal_from_actuator():
    records = _records(JavaAppIngestor().ingest_changes("org1", None))
    # The degraded payments-api sample yields friction signal an agent could act on.
    down = next(
        r for r in records
        if r["artifact_id"] == "payments-api:actuator:2026-06-28T10:00:00+00:00"
    )
    sig = down["signals"]
    assert sig["unhealthy"] is True
    assert sig["has_friction"] is True
    for f in ("unhealthy", "error_rate_elevated", "latency_degraded",
              "heap_pressure", "cpu_pressure"):
        assert f in sig["friction"]
    # The healthy inventory sample shows no friction (signal discriminates).
    healthy = next(
        r for r in records
        if r["artifact_id"] == "inventory-api:actuator:2026-06-28T09:30:00+00:00"
    )
    assert healthy["signals"]["has_friction"] is False


def test_ac1_produces_operational_signal_from_logs():
    records = _records(JavaAppIngestor().ingest_changes("org1", None))
    err = next(r for r in records if r["artifact_id"] == "payments-api:log:3")
    assert err["signals"]["is_error"] is True
    assert err["signals"]["has_exception"] is True
    assert err["signals"]["exception_type"] == "java.net.SocketTimeoutException"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental: first run loads all, incremental only newer, idle empty
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_loads_all_and_advances_checkpoint():
    store = Store()
    seen: list = []
    res = _drive(
        JavaAppIngestor(), "org1", store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    cursors = _decode_checkpoint(store.read("org1", "java_app").value)
    assert cursors == {
        "payments-api": {"log_offset": 4, "sample_ts": "2026-06-28T10:00:00+00:00"},
        "inventory-api": {"log_offset": 1, "sample_ts": "2026-06-28T09:30:00+00:00"},
    }


def test_ac2_incremental_returns_only_newer_than_checkpoint():
    since = Checkpoint.create("java_app", "org1", _encode_checkpoint({
        # mid payments-api (after log 2 / first sample); inventory absent => full.
        "payments-api": {"log_offset": 2, "sample_ts": "2026-06-28T09:00:00+00:00"},
    }))
    ids = [r["artifact_id"] for r in _records(JavaAppIngestor().ingest_changes("org1", since))]
    assert sorted(ids) == sorted(
        ["payments-api:log:3", "payments-api:log:4",
         "payments-api:actuator:2026-06-28T10:00:00+00:00"] + _INV_LOGS + _INV_ACT
    )
    # Already-seen items are not re-read.
    assert "payments-api:log:1" not in ids
    assert "payments-api:log:2" not in ids                       # equal offset
    assert "payments-api:actuator:2026-06-28T09:00:00+00:00" not in ids  # equal ts


def test_ac2_idle_application_yields_empty_delta_echoing_position():
    store = Store()
    _drive(JavaAppIngestor(), "org1", store)
    head = store.read("org1", "java_app").value

    # Second run: nothing new anywhere → empty delta, position does not regress.
    res = _drive(JavaAppIngestor(), "org1", store)
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "java_app").value == head


def test_ac2_idle_delta_is_single_empty_batch():
    since = Checkpoint.create("java_app", "org1", _encode_checkpoint({
        "payments-api": {"log_offset": 4, "sample_ts": "2026-06-28T10:00:00+00:00"},
        "inventory-api": {"log_offset": 1, "sample_ts": "2026-06-28T09:30:00+00:00"},
    }))
    batches = list(JavaAppIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].is_empty
    assert batches[0].is_complete
    # Echoes the incoming position (round-trips without loss).
    assert _decode_checkpoint(batches[0].next_checkpoint) == _decode_checkpoint(since.value)


def test_ac2_checkpoint_round_trips_idempotently():
    store = Store()
    _drive(JavaAppIngestor(), "org1", store)
    cp = store.read("org1", "java_app")
    # Feed the persisted checkpoint straight back as `since` → no new work.
    assert all(b.is_empty for b in JavaAppIngestor().ingest_changes("org1", cp))


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — first load is resumable: streamed as small checkpointed batches
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_load_streams_resumable_batches():
    store = Store()
    res = _drive(JavaAppIngestor(batch_size=1), "org1", store)
    assert res.ok and res.first_run and res.complete and res.checkpoint_advanced
    # 8 fixture artifacts → 8 single-record batches.
    assert res.records == len(_ALL)
    assert res.batches == len(_ALL)


def test_ac2_exactly_one_terminal_batch():
    batches = list(JavaAppIngestor(batch_size=3).ingest_changes("org1", None))
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
    # Every emitted checkpoint is a non-empty opaque string (runner can persist it).
    assert all(b.next_checkpoint for b in batches)
