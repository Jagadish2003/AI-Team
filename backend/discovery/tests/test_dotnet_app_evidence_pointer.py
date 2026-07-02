"""
R17-A4 / T1 (AC5) — EvidencePointer on every .NET-app operational signal.

Every signal must carry a valid R16-B1 EvidencePointer with
``source_system='dotnet_app'``, an artifact id, a timestamp, and
``origin='observed'`` — operational signals are directly measured, so they are
first-class observed evidence, never inferred (R17-A4 §3). Built through the
shared Evidence & Identity Spine, not a bespoke model. Offline / deterministic.
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest.base import Checkpoint
from discovery.ingest.dotnet_app import DotNetAppIngestor, _encode_checkpoint


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org1", since) for r in b.records]


def test_every_record_has_a_valid_observed_pointer():
    records = _records()
    assert records
    for r in records:
        ep = r.get("evidence_pointer")
        assert ep is not None, f"missing evidence_pointer on {r['artifact_id']}"
        assert ep["source_system"] == "dotnet_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"]
        assert ep["source_timestamp"]
        assert ep["extraction_job_id"] is None       # observed → no job id
        assert EvidencePointer.from_dict(ep).is_valid() is True


def test_pointer_artifact_traces_to_the_exact_reading():
    for r in _records():
        ep = r["evidence_pointer"]
        # source_artifact == the record's artifact id == app_id:{metrics|log}:ref
        assert ep["source_artifact"] == r["artifact_id"]
        assert ep["source_artifact"].startswith(f"{r['app_id']}:{r['artifact_kind']}:")


def test_pointer_timestamp_is_the_observation_time():
    for r in _records():
        assert r["evidence_pointer"]["source_timestamp"] == r["observed_ts"]


def test_holds_on_incremental_run():
    since = Checkpoint.create("dotnet_app", "org1", _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-20T08:05:00+00:00", "metrics_seq": 1},
    }))
    records = _records(since)
    assert records
    for r in records:
        ep = r["evidence_pointer"]
        assert ep["source_system"] == "dotnet_app" and ep["origin"] == OBSERVED
        assert EvidencePointer.from_dict(ep).is_valid() is True


def test_pointer_reuses_the_spine_dataclass():
    spine_fields = set(EvidencePointer.__dataclass_fields__)
    for r in _records():
        assert set(r["evidence_pointer"]).issubset(spine_fields)
