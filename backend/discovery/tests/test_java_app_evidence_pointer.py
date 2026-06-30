"""
R17-A3 / T1 (doc Section 3, R16-B1) — provenance on every Java-app signal.

The operational signal a Java application yields is directly measured, so it is
first-class OBSERVED evidence. This pins that every record carries a valid
EvidencePointer back to its source:

  * source_system == 'java_app'
  * source_artifact == the record's artifact id (the sampled endpoint / log line)
  * source_timestamp present (the sample / log timestamp)
  * origin == 'observed'  (no extraction_job_id — it is not inferred)

(Provenance is foundational to producing a usable signal, so T1 attaches it; the
broader provenance task is AC4. Offline / deterministic.)
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest.java_app import JavaAppIngestor

_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def _all_records():
    return [r for b in JavaAppIngestor().ingest_changes("org1", None) for r in b.records]


def test_every_record_has_a_valid_observed_evidence_pointer():
    records = _all_records()
    assert records
    for r in records:
        ptr = r.get("evidence_pointer")
        assert ptr is not None, f"missing evidence_pointer on {r['artifact_id']}"
        for field in _SPINE:
            assert ptr.get(field), f"empty {field} on {r['artifact_id']}"
        assert ptr["source_system"] == "java_app"
        assert ptr["origin"] == OBSERVED
        assert ptr["source_artifact"] == r["artifact_id"]
        # Observed pointers need no extraction job and must validate.
        assert ptr["extraction_job_id"] is None
        assert EvidencePointer.from_dict(ptr).is_valid() is True


def test_source_timestamp_tracks_the_observation_time():
    records = _all_records()
    log = next(r for r in records if r["surface"] == "logs")
    assert log["evidence_pointer"]["source_timestamp"] == log["ts"]
    act = next(r for r in records if r["surface"] == "actuator")
    assert act["evidence_pointer"]["source_timestamp"] == act["observed_at"]


def test_evidence_pointer_is_top_level_not_nested_in_signals():
    # Provenance must not leak into the operational signal block.
    for r in _all_records():
        assert "evidence_pointer" not in r["signals"]
