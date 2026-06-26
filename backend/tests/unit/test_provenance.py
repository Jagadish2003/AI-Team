"""Unit tests for the Evidence Pointer provenance contract (R16-B1, Part One / T1).

Covers the model-level acceptance criteria that belong to T1:

* AC2 — an ``origin='inferred'`` pointer with no ``extraction_job_id`` fails
  validation (so it is never persisted); observed pointers validate without a job id.
* AC8 — the extensible fields (``chunk_id``, ``retrieval_result_id``,
  ``detector_evidence_id``, ``confidence``) are present in the structure and null in
  1.6, ready for retrieval (1.8) without a schema change.

Plus the Section 1 ``is_valid()`` rules: every mandatory spine field is required, and
the origin must be one of the two known values.
"""
import dataclasses

import pytest

from app.provenance import (
    INFERRED,
    OBSERVED,
    EvidencePointer,
    utc_now_iso,
)


def _observed_kwargs(**overrides):
    base = dict(
        source_system="salesforce",
        source_artifact="0061t00000abcDEF",
        source_timestamp="2026-06-24T10:00:00+00:00",
        origin=OBSERVED,
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Mandatory spine (Section 1)
# --------------------------------------------------------------------------- #

def test_observed_pointer_with_full_spine_is_valid():
    ptr = EvidencePointer(**_observed_kwargs())
    assert ptr.is_valid() is True


@pytest.mark.parametrize(
    "missing_field",
    ["source_system", "source_artifact", "source_timestamp", "origin"],
)
def test_missing_any_mandatory_spine_field_is_invalid(missing_field):
    """If any mandatory spine field is missing/empty, the pointer is invalid."""
    ptr = EvidencePointer(**_observed_kwargs(**{missing_field: ""}))
    assert ptr.is_valid() is False


def test_unknown_origin_value_is_invalid():
    ptr = EvidencePointer(**_observed_kwargs(origin="guessed"))
    assert ptr.is_valid() is False


# --------------------------------------------------------------------------- #
# AC2 — observed vs inferred and the extraction_job_id rule
# --------------------------------------------------------------------------- #

def test_ac2_observed_validates_without_job_id():
    ptr = EvidencePointer.observed(
        source_system="jira",
        source_artifact="PROJ-123",
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    assert ptr.origin == OBSERVED
    assert ptr.extraction_job_id is None
    assert ptr.is_valid() is True


def test_ac2_inferred_without_job_id_fails_validation():
    """origin='inferred' with no extraction_job_id must fail validation (not persisted)."""
    ptr = EvidencePointer(
        source_system="agentiq",
        source_artifact="entity:acme-corp",
        source_timestamp="2026-06-24T10:00:00+00:00",
        origin=INFERRED,
    )
    assert ptr.is_valid() is False


def test_ac2_inferred_empty_job_id_fails_validation():
    """An empty/blank job id must not satisfy the inferred rule."""
    ptr = EvidencePointer(
        source_system="agentiq",
        source_artifact="entity:acme-corp",
        source_timestamp="2026-06-24T10:00:00+00:00",
        origin=INFERRED,
        extraction_job_id="",
    )
    assert ptr.is_valid() is False


def test_ac2_inferred_with_job_id_is_valid():
    ptr = EvidencePointer.inferred(
        source_system="agentiq",
        source_artifact="entity:acme-corp",
        extraction_job_id="job-2026-06-24-001",
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    assert ptr.origin == INFERRED
    assert ptr.extraction_job_id == "job-2026-06-24-001"
    assert ptr.is_valid() is True


def test_ac2_inferred_factory_normalises_blank_job_id_to_invalid():
    """The inferred() factory normalises a blank job id to None -> invalid pointer."""
    ptr = EvidencePointer.inferred(
        source_system="agentiq",
        source_artifact="entity:acme-corp",
        extraction_job_id="",
    )
    assert ptr.extraction_job_id is None
    assert ptr.is_valid() is False


# --------------------------------------------------------------------------- #
# AC8 — extensible detail fields present and null in 1.6
# --------------------------------------------------------------------------- #

def test_ac8_extensible_fields_present_and_null_by_default():
    ptr = EvidencePointer.observed(
        source_system="salesforce",
        source_artifact="0061t00000abcDEF",
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    assert ptr.chunk_id is None
    assert ptr.retrieval_result_id is None
    assert ptr.detector_evidence_id is None
    assert ptr.confidence is None


def test_ac8_extensible_fields_present_in_serialised_structure():
    """The fields exist in the serialised shape, so retrieval (1.8) fills them
    without a schema change."""
    ptr = EvidencePointer.observed(
        source_system="salesforce",
        source_artifact="0061t00000abcDEF",
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    data = ptr.to_dict()
    for field_name in ("chunk_id", "retrieval_result_id", "detector_evidence_id", "confidence"):
        assert field_name in data
        assert data[field_name] is None


def test_ac8_extensible_fields_can_be_populated_without_schema_change():
    ptr = EvidencePointer(
        source_system="salesforce",
        source_artifact="0061t00000abcDEF",
        source_timestamp="2026-06-24T10:00:00+00:00",
        origin=OBSERVED,
        chunk_id="chunk-42",
        retrieval_result_id="rr-7",
        detector_evidence_id="det-9",
        confidence=0.91,
    )
    assert ptr.is_valid() is True
    assert ptr.to_dict()["chunk_id"] == "chunk-42"
    assert ptr.confidence == 0.91


# --------------------------------------------------------------------------- #
# dict round-trip + factory defaults
# --------------------------------------------------------------------------- #

def test_to_dict_includes_full_spine_and_detail():
    ptr = EvidencePointer.inferred(
        source_system="agentiq",
        source_artifact="rel:acme-uses-jira",
        extraction_job_id="job-1",
        confidence=0.5,
    )
    data = ptr.to_dict()
    expected_keys = {f.name for f in dataclasses.fields(EvidencePointer)}
    assert set(data) == expected_keys


def test_from_dict_round_trips_and_ignores_unknown_keys():
    ptr = EvidencePointer.observed(
        source_system="jira",
        source_artifact="PROJ-9",
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    data = ptr.to_dict()
    data["unexpected_future_field"] = "ignored"
    restored = EvidencePointer.from_dict(data)
    assert restored == ptr
    assert restored.is_valid() is True


def test_factories_default_timestamp_to_utc_now():
    ptr = EvidencePointer.observed(source_system="slack", source_artifact="msg-1")
    assert ptr.source_timestamp  # non-empty
    assert ptr.is_valid() is True


def test_utc_now_iso_is_timezone_aware():
    assert "+00:00" in utc_now_iso() or utc_now_iso().endswith("Z")
