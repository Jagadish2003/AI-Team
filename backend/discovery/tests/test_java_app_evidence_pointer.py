"""
R17-A3 / T4 + T7 — EvidencePointer on every Java-app operational signal (AC4).

Every signal must carry a valid R16-B1 EvidencePointer with
``source_system='java_app'``, an artifact id, a timestamp, and
``origin='observed'`` — operational signals are directly measured, so they are
first-class observed evidence, never inferred (R17-A3 §3). These tests run
offline against the deterministic fixture; the checkpoint store is in-memory and
telemetry is silenced, so no DB is needed.
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.java_app import JavaAppIngestor
from discovery.ingest.java_app_signals import build_evidence_pointer


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
        JavaAppIngestor(), org_id,
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    return collected


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — every record carries a valid observed pointer
# ─────────────────────────────────────────────────────────────────────────────
def test_every_record_has_a_valid_observed_pointer():
    records = _all_records()
    assert records
    for r in records:
        ep = r.get("evidence_pointer")
        assert ep is not None, f"missing evidence_pointer on {r['artifact_id']}"
        assert ep["source_system"] == "java_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"]                       # non-empty artifact id
        assert ep["source_timestamp"]                      # non-empty timestamp
        # The mandatory spine validates and an observed pointer needs no job id.
        assert EvidencePointer.from_dict(ep).is_valid() is True


def test_pointer_artifact_matches_record_kind():
    records = _all_records()
    for r in records:
        ep = r["evidence_pointer"]
        # source_artifact encodes app_id:{metrics|log}:ref so a signal traces back
        # to the exact reading it was measured from.
        assert ep["source_artifact"].startswith(f"{r['app_id']}:{r['artifact_kind']}:")


def test_observed_pointers_carry_no_extraction_job_id():
    # Observed evidence is directly measured — it must NOT be tagged as inferred.
    for r in _all_records():
        assert r["evidence_pointer"].get("extraction_job_id") is None


# ─────────────────────────────────────────────────────────────────────────────
# build_evidence_pointer helper
# ─────────────────────────────────────────────────────────────────────────────
def test_build_evidence_pointer_shape():
    ep = build_evidence_pointer("payments-api", "metrics", "2026-06-10T08:00:00+00:00",
                                "2026-06-10T08:00:00+00:00")
    assert ep["source_system"] == "java_app"
    assert ep["origin"] == OBSERVED
    assert ep["source_artifact"] == "payments-api:metrics:2026-06-10T08:00:00+00:00"
    assert ep["source_artifact_type"] == "record_id"
    # Extensible 1.6 detail fields present-but-null (AC8 of R16-B1).
    assert ep["chunk_id"] is None
    assert ep["retrieval_result_id"] is None


def test_build_evidence_pointer_falls_back_to_now_when_ts_missing():
    ep = build_evidence_pointer("svc", "log", "7", None)
    assert ep["source_timestamp"]   # never empty — mandatory spine always populated
