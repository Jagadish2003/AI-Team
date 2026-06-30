"""
R17-A3 / T1 + T7 — JavaAppIngestor change-based ingestion (AC1, AC2, AC8).

Binds the acceptance criteria to the REAL ``JavaAppIngestor`` driven through the
shared change runner, offline against the deterministic ``java_app_sample.json``
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
from discovery.ingest.java_app import JavaAppIngestor, _decode_checkpoint, _encode_checkpoint


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
    ing = JavaAppIngestor()
    assert ing.connector_id == "java_app"
    # Operational artifacts are forward-only; the connector declares it cannot
    # detect deletes rather than faking tombstones (R16-A1 §5).
    assert ing.reports_deletes is False


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — reads health/diagnostics endpoints + logs, produces operational signal
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_reads_metrics_and_logs():
    _res, _store, collected = _collect(JavaAppIngestor(), "org-1")
    kinds = {r["artifact_kind"] for r in collected}
    assert kinds == {"metrics", "log"}
    # Both configured apps are read (payments-api + ledger-svc).
    assert {r["app_id"] for r in collected} == {"payments-api", "ledger-svc"}


def test_records_carry_operational_signal():
    _res, _store, collected = _collect(JavaAppIngestor(), "org-1")
    # Every record carries an extracted operational signal block (T2).
    assert all("signals" in r for r in collected)
    payments_logs = [
        r for r in collected if r["app_id"] == "payments-api" and r["artifact_kind"] == "log"
    ]
    assert payments_logs
    # The friction is visible in the produced signal: payments shows reasons.
    metric_rec = next(
        r for r in collected if r["app_id"] == "payments-api" and r["artifact_kind"] == "metrics"
    )
    assert metric_rec["signals"]["service"] == "payments"


def test_checkpoint_advances_and_is_opaque_json():
    res, store, _ = _collect(JavaAppIngestor(), "org-1")
    assert res.ok and res.checkpoint_advanced
    cp = store.read("org-1", "java_app")
    assert cp is not None
    decoded = _decode_checkpoint(cp.value)
    # Per-app cursor map with a log offset + metrics sample time.
    assert "payments-api" in decoded
    assert decoded["payments-api"]["log_offset"] >= 1
    assert decoded["payments-api"]["metrics_ts"]


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental: only new logs/samples; idle app yields empty/minimal delta
# ─────────────────────────────────────────────────────────────────────────────
def test_idle_application_yields_empty_delta_on_second_run():
    res1, store, _ = _collect(JavaAppIngestor(), "org-1")
    assert res1.records > 0
    res2, _store, collected2 = _collect(JavaAppIngestor(), "org-1", store=store)
    assert res2.ok
    assert collected2 == []        # nothing new
    assert res2.records == 0       # idle → empty delta


def test_incremental_reads_only_new_records():
    # Start mid-stream: payments-api already read up to log offset 2 and metrics
    # sample 08:00; ledger-svc never seen.
    store = Store()
    store.save(
        Checkpoint.create(
            "java_app",
            "org-1",
            _encode_checkpoint(
                {"payments-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:00:00+00:00"}}
            ),
        )
    )
    _res, _store, collected = _collect(JavaAppIngestor(), "org-1", store=store)
    pa_logs = {
        r["log_offset"] for r in collected
        if r["app_id"] == "payments-api" and r["artifact_kind"] == "log"
    }
    # Only payments-api log offsets strictly greater than 2 are re-read.
    assert pa_logs == {3, 4, 5}
    pa_metric_ts = {
        r["observed_ts"] for r in collected
        if r["app_id"] == "payments-api" and r["artifact_kind"] == "metrics"
    }
    assert "2026-06-10T08:00:00+00:00" not in pa_metric_ts   # already seen
    # ledger-svc (absent from the cursor) is read from the beginning.
    assert any(r["app_id"] == "ledger-svc" for r in collected)


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not-json") == {}
    assert _decode_checkpoint('{"v":1}') == {}   # no apps key


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — operational surface only, never source code
# ─────────────────────────────────────────────────────────────────────────────
def test_records_describe_operational_surface_not_source_code():
    _res, _store, collected = _collect(JavaAppIngestor(), "org-1")
    # Records are exclusively metric samples + log entries — the operational
    # surface. No record carries source-code / repository fields (1.8 phase).
    forbidden = {"source_code", "ast", "class_body", "repository", "file_path", "diff"}
    for r in collected:
        assert r["artifact_kind"] in ("metrics", "log")
        assert forbidden.isdisjoint(r.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Batches honour the change-runner contract
# ─────────────────────────────────────────────────────────────────────────────
def test_yields_delta_batches_with_terminal_flag():
    batches = list(JavaAppIngestor().ingest_changes("org-1", None))
    assert batches
    assert all(isinstance(b, DeltaBatch) for b in batches)
    # Exactly one terminal batch so the runner can advance the checkpoint.
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete is True
