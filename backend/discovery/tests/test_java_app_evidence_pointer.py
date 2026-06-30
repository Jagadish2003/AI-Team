"""
R17-A3 / T4 (AC4) — every Java-app signal carries valid OBSERVED provenance.

Drives the ingestor over the deterministic offline fixture and asserts, for
EVERY emitted signal record — across both operational surfaces (Actuator + logs),
on a first run AND on an incremental run — that it carries a valid
:class:`EvidencePointer` from the shared spine:

  * source_system == 'java_app'                 (distinguishes Java evidence)
  * a non-empty source_artifact == the record's artifact id   (traceability)
  * a non-empty source_timestamp == the observation time      (when observed)
  * origin == 'observed', extraction_job_id is None           (not inferred)
  * EvidencePointer.from_dict(ptr).is_valid() is True         (spine-valid)

Offline / deterministic.
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest.base import Checkpoint
from discovery.ingest.java_app import JavaAppIngestor, _encode_checkpoint

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Pin offline mode so a stray INGEST_MODE=live can't trigger a network read."""
    monkeypatch.setenv("INGEST_MODE", "offline")


def _records(since=None):
    return [r for b in JavaAppIngestor().ingest_changes("org1", since) for r in b.records]


def _assert_valid_observed_pointer(record):
    ptr = record.get("evidence_pointer")
    aid = record["artifact_id"]
    assert ptr is not None, f"missing evidence_pointer on {aid}"
    for field in _SPINE:
        assert ptr.get(field), f"empty spine field {field!r} on {aid}"
    assert ptr["source_system"] == "java_app"
    assert ptr["origin"] == OBSERVED
    assert ptr["extraction_job_id"] is None          # observed → no job id
    assert ptr["source_artifact"] == aid             # traces back to this signal
    assert EvidencePointer.from_dict(ptr).is_valid() is True


# ── AC4: every signal, both surfaces, first run ──────────────────────────────
def test_ac4_every_signal_has_valid_observed_provenance():
    records = _records()
    assert records
    for r in records:
        _assert_valid_observed_pointer(r)


def test_ac4_holds_for_both_surfaces():
    by_surface = {}
    for r in _records():
        by_surface.setdefault(r["surface"], []).append(r)
    assert set(by_surface) == {"actuator", "logs"}      # both surfaces present
    for surface_records in by_surface.values():
        for r in surface_records:
            _assert_valid_observed_pointer(r)


# ── AC4: provenance also correct on an incremental delta ─────────────────────
def test_ac4_holds_on_incremental_run():
    since = Checkpoint.create("java_app", "org1", _encode_checkpoint({
        "payments-api": {"log_offset": 2, "sample_ts": "2026-06-28T09:00:00+00:00"},
    }))
    records = _records(since)
    assert records  # there ARE newer signals
    for r in records:
        _assert_valid_observed_pointer(r)


# ── the artifact id points at the right operational source ───────────────────
def test_log_pointer_timestamp_is_the_log_time():
    log = next(r for r in _records() if r["surface"] == "logs")
    assert log["evidence_pointer"]["source_timestamp"] == log["ts"]


def test_actuator_pointer_timestamp_is_the_sample_time():
    act = next(r for r in _records() if r["surface"] == "actuator")
    assert act["evidence_pointer"]["source_timestamp"] == act["observed_at"]


def test_log_with_native_event_id_traces_to_that_event():
    # The fixture's payments-api offset-4 log carries event_id 'evt-paym-0042';
    # its artifact id (and pointer) reference the event, not just the position.
    rec = next(r for r in _records() if r.get("event_id") == "evt-paym-0042")
    assert rec["artifact_id"] == "payments-api:log:event:evt-paym-0042"
    assert rec["evidence_pointer"]["source_artifact"] == "payments-api:log:event:evt-paym-0042"


def test_actuator_artifact_references_app_endpoint_and_sample_time():
    act = next(
        r for r in _records()
        if r["artifact_id"] == "payments-api:actuator:2026-06-28T10:00:00+00:00"
    )
    ptr = act["evidence_pointer"]
    assert ptr["source_artifact"] == "payments-api:actuator:2026-06-28T10:00:00+00:00"
    assert ptr["source_system"] == "java_app"


# ── provenance does not leak into the operational signal block ───────────────
def test_provenance_is_top_level_not_nested_in_signals():
    for r in _records():
        assert "evidence_pointer" not in r["signals"]


# ── reuses the spine model, not a bespoke one ────────────────────────────────
def test_pointer_shape_matches_the_spine_dataclass():
    spine_fields = set(EvidencePointer.__dataclass_fields__)
    for r in _records():
        # Every key in the emitted pointer is a real spine field (no foreign keys).
        assert set(r["evidence_pointer"]).issubset(spine_fields)
