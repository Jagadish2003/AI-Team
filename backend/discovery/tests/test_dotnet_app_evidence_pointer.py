"""
R17-A4 / T4 + T7 — EvidencePointer on every .NET-app operational signal (AC5).

Every signal must carry a valid R16-B1 EvidencePointer with
``source_system='dotnet_app'``, an artifact id, a timestamp, and
``origin='observed'`` — operational signals are directly measured, so they are
first-class observed evidence, never inferred (R17-A4 §3). Built through the
shared Evidence & Identity Spine, not a bespoke model.

Drives the ingestor over the deterministic offline fixture and asserts, for EVERY
emitted signal record — across both operational surfaces (diagnostics + logs), on
a first run AND an incremental run — that it carries a valid
:class:`EvidencePointer` from the shared spine:

  * source_system == 'dotnet_app'                         (distinguishes .NET evidence)
  * a non-empty source_artifact == the record's artifact id   (traceability)
  * a non-empty source_timestamp == the observation time      (when observed)
  * origin == 'observed', extraction_job_id is None           (not inferred)
  * EvidencePointer.from_dict(ptr).is_valid() is True         (spine-valid)
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.dotnet_app import DotNetAppIngestor, _encode_checkpoint
from discovery.ingest.dotnet_app_signals import (
    build_dotnet_app_corroboration_payload,
    build_evidence_pointer,
)

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


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


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org-1", since) for r in b.records]


def _assert_valid_observed_pointer(record):
    ep = record.get("evidence_pointer")
    aid = record["artifact_id"]
    assert ep is not None, f"missing evidence_pointer on {aid}"
    for field in _SPINE:
        assert ep.get(field), f"empty spine field {field!r} on {aid}"
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert ep["extraction_job_id"] is None               # observed → no job id
    assert ep["source_artifact"] == aid                  # traces back to this signal
    assert EvidencePointer.from_dict(ep).is_valid() is True


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — every record carries a valid observed pointer
# ─────────────────────────────────────────────────────────────────────────────
def test_every_record_has_a_valid_observed_pointer():
    records = _all_records()
    assert records
    for r in records:
        _assert_valid_observed_pointer(r)


def test_ac5_holds_for_both_surfaces():
    by_kind = {}
    for r in _all_records():
        by_kind.setdefault(r["artifact_kind"], []).append(r)
    assert set(by_kind) == {"metrics", "log"}
    for recs in by_kind.values():
        for r in recs:
            _assert_valid_observed_pointer(r)


def test_pointer_artifact_traces_to_the_exact_reading():
    for r in _all_records():
        ep = r["evidence_pointer"]
        # source_artifact == the record's artifact id == app_id:{metrics|log}:ref
        assert ep["source_artifact"] == r["artifact_id"]
        assert ep["source_artifact"].startswith(f"{r['app_id']}:{r['artifact_kind']}:")


def test_pointer_timestamp_is_the_observation_time():
    for r in _all_records():
        assert r["evidence_pointer"]["source_timestamp"] == r["observed_ts"]


def test_log_with_native_event_id_traces_to_that_event():
    # A native log event id is the most precise handle on the exact log event, so
    # it is preferred over the stream offset as the artifact reference (T4).
    rec = next(r for r in _records() if r.get("event_id") == "evt-orders-0042")
    assert rec["artifact_id"] == "orders-api:log:event:evt-orders-0042"
    assert rec["evidence_pointer"]["source_artifact"] == "orders-api:log:event:evt-orders-0042"


def test_observed_pointers_carry_no_extraction_job_id():
    for r in _all_records():
        assert r["evidence_pointer"].get("extraction_job_id") is None


def test_holds_on_incremental_run():
    since = Checkpoint.create("dotnet_app", "org-1", _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00", "metrics_seq": 1},
    }))
    records = _records(since)
    assert records
    for r in records:
        _assert_valid_observed_pointer(r)


def test_pointer_reuses_the_spine_dataclass():
    spine_fields = set(EvidencePointer.__dataclass_fields__)
    for r in _all_records():
        assert set(r["evidence_pointer"]).issubset(spine_fields)


# ─────────────────────────────────────────────────────────────────────────────
# build_evidence_pointer helper
# ─────────────────────────────────────────────────────────────────────────────
def test_build_evidence_pointer_shape():
    ep = build_evidence_pointer("orders-api", "metrics", "2026-06-10T08:00:00+00:00",
                                "2026-06-10T08:00:00+00:00")
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert ep["source_artifact"] == "orders-api:metrics:2026-06-10T08:00:00+00:00"
    assert ep["source_artifact_type"] == "record_id"
    assert ep["chunk_id"] is None
    assert ep["retrieval_result_id"] is None


def test_build_evidence_pointer_falls_back_to_now_when_ts_missing():
    ep = build_evidence_pointer("svc", "log", "7", None)
    assert ep["source_timestamp"]   # never empty — mandatory spine always populated


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
