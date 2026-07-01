"""R17-A2 / AT-461 (T4) — EvidencePointer on every SharePoint signal.

AC5: Every SharePoint signal carries a valid EvidencePointer (R16-B1) with
``source_system='sharepoint'``, a document/item id, a timestamp, and
``origin='observed'``. No SharePoint signal may enter the system without a fully
populated, valid EvidencePointer, so any finding is traceable back to its exact
source driveItem.

These tests exercise both the per-item builder
(:func:`discovery.ingest.sharepoint._build_evidence_pointer`) and the end-to-end
ingestor output (:class:`discovery.ingest.sharepoint.SharePointIngestor`), and
assert the pointers validate against the canonical R16-B1 model
(:class:`app.provenance.EvidencePointer`). Mirrors
``test_confluence_evidence_pointer.py`` and ``test_teams_evidence_pointer.py`` (the
paired connectors' T4 tests) and runs offline against the deterministic
``sharepoint_sample.json`` fixture.
"""
from __future__ import annotations

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest.sharepoint import SharePointIngestor, _build_evidence_pointer

# The mandatory R16-B1 spine fields every pointer must populate.
_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real SharePointIngestor reads the deterministic fixture."""
    monkeypatch.setenv("INGEST_MODE", "offline")


def _all_records():
    return [r for b in SharePointIngestor().ingest_changes("org1", None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# _build_evidence_pointer — unit
# ─────────────────────────────────────────────────────────────────────────────
def test_build_evidence_pointer_has_observed_spine():
    ptr = _build_evidence_pointer("S-eng", "b-docs", "f200", "2026-06-10T09:10:00Z")

    assert ptr["source_system"] == "sharepoint"
    assert ptr["source_artifact"] == "S-eng/b-docs:f200"
    assert ptr["origin"] == OBSERVED
    # source_timestamp is the item's own change moment, carried through verbatim.
    assert ptr["source_timestamp"] == "2026-06-10T09:10:00Z"
    # Observed → no extraction job required, and the artifact is a stable id.
    assert ptr["extraction_job_id"] is None
    assert ptr["source_artifact_type"] == "record_id"


def test_build_evidence_pointer_is_valid_observed_pointer():
    ptr = _build_evidence_pointer("S-eng", "b-docs", "f200", "2026-06-10T09:10:00Z")
    assert EvidencePointer.from_dict(ptr).is_valid() is True


def test_build_evidence_pointer_carries_extensible_fields_null():
    """R16-B1: extensible detail fields ship present-but-null in 1.6 (AC8 of B1)."""
    ptr = _build_evidence_pointer("S-eng", "b-docs", "f200", "2026-06-10T09:10:00Z")
    for field in ("chunk_id", "retrieval_result_id", "detector_evidence_id", "confidence"):
        assert field in ptr and ptr[field] is None


def test_build_evidence_pointer_missing_timestamp_still_valid():
    """A missing/garbled timestamp must never yield an invalid pointer — the spine
    still populates (falls back to 'now') so the signal is never dropped for
    provenance."""
    ptr = _build_evidence_pointer("S-eng", "b-docs", "f200", None)
    assert ptr["source_system"] == "sharepoint"
    assert ptr["origin"] == OBSERVED
    assert ptr["source_timestamp"]  # non-empty fallback
    assert EvidencePointer.from_dict(ptr).is_valid() is True


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end — every emitted record carries a valid observed pointer (AC5)
# ─────────────────────────────────────────────────────────────────────────────
def test_every_record_carries_evidence_pointer():
    records = _all_records()
    assert records  # the fixture yields signals

    for r in records:
        ptr = r.get("evidence_pointer")
        assert ptr is not None, f"record {r.get('artifact_id')} has no evidence_pointer"

        # Mandatory spine populated.
        for field in _SPINE:
            assert ptr.get(field), f"{field} missing/empty on {r['artifact_id']}"

        # AC5 exact values.
        assert ptr["source_system"] == "sharepoint"
        assert ptr["origin"] == OBSERVED
        # item id == the record's own artifact identity (site/drive:item).
        assert ptr["source_artifact"] == r["artifact_id"]
        # timestamp is the item's own change moment (last-modified, else created).
        assert ptr["source_timestamp"] == (r["last_modified_at"] or r["created_at"])

        # Observed signal → never carries an inference job.
        assert ptr["extraction_job_id"] is None


def test_every_record_pointer_validates_against_canonical_model():
    """No SharePoint signal enters the system with an invalid EvidencePointer."""
    for r in _all_records():
        assert EvidencePointer.from_dict(r["evidence_pointer"]).is_valid() is True


def test_pointer_is_observed_never_inferred():
    """SharePoint reach-phase signal is read directly — never inferred (provenance
    side of 'LLM proposes, never authors truth')."""
    for r in _all_records():
        assert r["evidence_pointer"]["origin"] == OBSERVED


def test_evidence_pointer_does_not_pollute_signals_block():
    """The provenance pointer is a top-level record field — it must not leak into
    the reach-phase ``signals`` block (which stays exactly cross_references +
    activity)."""
    for r in _all_records():
        assert set(r["signals"].keys()) == {"cross_references", "activity"}
        assert "evidence_pointer" not in r["signals"]
