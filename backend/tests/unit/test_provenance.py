"""Unit tests for R16-B1 Part One — the EvidencePointer model (provenance.py).

Covers the validation rule that AC2 hinges on (inferred content MUST name an
extraction_job_id; observed content validates without one), the mandatory spine,
and the present-but-null extensible fields (AC8).
"""
from __future__ import annotations

from app.provenance import INFERRED, OBSERVED, EvidencePointer


# ── mandatory spine + origin rule (AC2) ──────────────────────────────────────


def test_observed_pointer_is_valid_without_job_id():
    # AC2: observed artifacts validate without an extraction_job_id.
    p = EvidencePointer(
        source_system="salesforce",
        source_artifact="001X000001",
        source_timestamp="2026-01-01T00:00:00+00:00",
        origin=OBSERVED,
    )
    assert p.is_valid() is True
    assert p.extraction_job_id is None


def test_inferred_pointer_without_job_id_is_invalid():
    # AC2: inferred content with no extraction_job_id fails validation.
    p = EvidencePointer(
        source_system="agentiq",
        source_artifact="d1+d2",
        source_timestamp="2026-01-01T00:00:00+00:00",
        origin=INFERRED,
        extraction_job_id=None,
    )
    assert p.is_valid() is False


def test_inferred_pointer_with_job_id_is_valid():
    p = EvidencePointer(
        source_system="agentiq",
        source_artifact="d1+d2",
        source_timestamp="2026-01-01T00:00:00+00:00",
        origin=INFERRED,
        extraction_job_id="run_123",
    )
    assert p.is_valid() is True


def test_missing_any_spine_field_is_invalid():
    base = dict(
        source_system="salesforce",
        source_artifact="001",
        source_timestamp="2026-01-01T00:00:00+00:00",
        origin=OBSERVED,
    )
    for field_name in ("source_system", "source_artifact", "source_timestamp", "origin"):
        broken = dict(base)
        broken[field_name] = ""
        assert EvidencePointer(**broken).is_valid() is False, field_name


def test_unknown_origin_is_invalid():
    p = EvidencePointer(
        source_system="salesforce",
        source_artifact="001",
        source_timestamp="2026-01-01T00:00:00+00:00",
        origin="guessed",
    )
    assert p.is_valid() is False


# ── extensible detail present-but-null (AC8) ──────────────────────────────────


def test_extensible_fields_present_and_null_by_default():
    d = EvidencePointer.observed(source_system="jira", source_artifact="PROJ-1").to_dict()
    for key in ("chunk_id", "retrieval_result_id", "detector_evidence_id"):
        assert key in d, f"{key} missing from serialized pointer"
        assert d[key] is None, f"{key} should be null in 1.6"


# ── factories ─────────────────────────────────────────────────────────────────


def test_observed_factory_defaults_origin_and_timestamp():
    p = EvidencePointer.observed(source_system="jira", source_artifact="PROJ-1", confidence=0.8)
    assert p.origin == OBSERVED
    assert p.source_timestamp  # auto-filled UTC stamp
    assert p.confidence == 0.8
    assert p.is_valid()


def test_inferred_factory_empty_job_id_normalises_to_invalid():
    # An empty extraction_job_id must not slip through as a job id.
    p = EvidencePointer.inferred(source_system="agentiq", source_artifact="x", extraction_job_id="")
    assert p.extraction_job_id is None
    assert p.is_valid() is False


def test_to_dict_from_dict_round_trip_ignores_unknown_keys():
    p = EvidencePointer.inferred(
        source_system="agentiq", source_artifact="x", extraction_job_id="run_1"
    )
    d = p.to_dict()
    d["future_field"] = "ignored"  # forward-compat: unknown keys dropped
    restored = EvidencePointer.from_dict(d)
    assert restored.origin == INFERRED
    assert restored.extraction_job_id == "run_1"
    assert restored.is_valid()
