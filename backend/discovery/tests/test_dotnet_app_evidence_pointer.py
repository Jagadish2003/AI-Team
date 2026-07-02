"""
R17-A4 / T4 (AC5) — every .NET-app signal carries valid OBSERVED provenance.

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
from discovery.ingest.base import Checkpoint
from discovery.ingest.dotnet_app import DotNetAppIngestor, _encode_checkpoint

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org1", since) for r in b.records]


def _assert_valid_observed_pointer(record):
    ep = record.get("evidence_pointer")
    aid = record["artifact_id"]
    assert ep is not None, f"missing evidence_pointer on {aid}"
    for field in _SPINE:
        assert ep.get(field), f"empty spine field {field!r} on {aid}"
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert ep["extraction_job_id"] is None
    assert ep["source_artifact"] == aid                  # traces back to this signal
    assert EvidencePointer.from_dict(ep).is_valid() is True


def test_ac5_every_signal_has_valid_observed_provenance():
    records = _records()
    assert records
    for r in records:
        _assert_valid_observed_pointer(r)


def test_ac5_holds_for_both_surfaces():
    by_kind = {}
    for r in _records():
        by_kind.setdefault(r["artifact_kind"], []).append(r)
    assert set(by_kind) == {"metrics", "log"}
    for recs in by_kind.values():
        for r in recs:
            _assert_valid_observed_pointer(r)


def test_ac5_holds_on_incremental_run():
    since = Checkpoint.create("dotnet_app", "org1", _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-20T08:05:00+00:00", "metrics_seq": 1},
    }))
    records = _records(since)
    assert records
    for r in records:
        _assert_valid_observed_pointer(r)


def test_metric_pointer_timestamp_is_the_sample_time():
    m = next(r for r in _records() if r["artifact_kind"] == "metrics")
    assert m["evidence_pointer"]["source_timestamp"] == m["observed_ts"]


def test_log_pointer_timestamp_is_the_log_time():
    log = next(r for r in _records() if r["artifact_kind"] == "log")
    assert log["evidence_pointer"]["source_timestamp"] == log["observed_ts"]


def test_log_with_native_event_id_traces_to_that_event():
    rec = next(r for r in _records() if r.get("event_id") == "evt-orders-0042")
    assert rec["artifact_id"] == "orders-api:log:event:evt-orders-0042"
    assert rec["evidence_pointer"]["source_artifact"] == "orders-api:log:event:evt-orders-0042"


def test_provenance_reuses_the_spine_dataclass():
    spine_fields = set(EvidencePointer.__dataclass_fields__)
    for r in _records():
        assert set(r["evidence_pointer"]).issubset(spine_fields)
