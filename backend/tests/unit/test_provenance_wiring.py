"""Unit tests for R16-B1 T2 — provenance wired into the artifact write paths.

These exercise the pointer-construction the three write paths use, WITHOUT a
database (the graph layer is PostgreSQL; these tests stay pure):

  * entity_resolution._with_observed_evidence  — entities carry an observed pointer
  * relationship_mapper._relationship_pointer   — observed/inferred edge pointers
  * relationship_mapper.upsert_relationship      — rejects an invalid inferred edge
    BEFORE touching the DB (AC2: not persisted)
  * llm_enrichment._attach_enrichment_provenance — inferred narrative pointer +
    grounding evidence link

Together they cover AC1 (every entity / relationship / enrichment artifact carries
a valid EvidencePointer with the mandatory spine) and AC2 (inferred without an
extraction_job_id fails validation; observed validates without one).
"""
from __future__ import annotations

from app.entity_resolution import _with_observed_evidence
from app.llm_enrichment import _attach_enrichment_provenance
from app.provenance import EvidencePointer
from app.relationship_mapper import _relationship_pointer, upsert_relationship


# ── entities: observed pointer on the create path (AC1) ──────────────────────


def test_entity_metadata_carries_valid_observed_pointer(monkeypatch):
    # source_timestamp is the run's observation time, not utc_now() at resolution
    # (R16-B1 review). Stub the run lookup so this stays DB-free and deterministic.
    import app.entity_resolution as er
    monkeypatch.setattr(er.db, "get_run", lambda rid: {"startedAt": "2026-06-24T10:00:00+00:00"})

    md = _with_observed_evidence(
        None, source_system="salesforce", source_record_id="001X",
        canonical_name="acme corp", confidence=0.9, run_id="run_1",
    )
    ptr = md["evidence_pointer"]
    assert ptr["origin"] == "observed"
    assert ptr["source_system"] == "salesforce"
    # A stable source_record_id is used verbatim and flagged as such.
    assert ptr["source_artifact"] == "001X"
    assert ptr["source_artifact_type"] == "record_id"
    # source_timestamp is the run's observation time, NOT the resolution wall clock.
    assert ptr["source_timestamp"] == "2026-06-24T10:00:00+00:00"
    assert ptr["extraction_job_id"] is None  # observed needs no job id
    assert EvidencePointer.from_dict(ptr).is_valid()


def test_entity_pointer_falls_back_to_canonical_name_and_flags_it(monkeypatch):
    # No stable source_record_id -> falls back to the canonical name, and marks
    # source_artifact_type so a consumer knows the artifact is NOT a stable id.
    import app.entity_resolution as er
    monkeypatch.setattr(er.db, "get_run", lambda rid: {"startedAt": "2026-06-24T10:00:00+00:00"})

    md = _with_observed_evidence(
        None, source_system="agentiq", source_record_id=None,
        canonical_name="pr_review_bottleneck", confidence=0.8, run_id="run_2",
    )
    ptr = md["evidence_pointer"]
    assert ptr["source_artifact"] == "pr_review_bottleneck"
    assert ptr["source_artifact_type"] == "canonical_name"
    assert EvidencePointer.from_dict(ptr).is_valid()


def test_entity_pointer_preserves_existing_metadata(monkeypatch):
    import app.entity_resolution as er
    monkeypatch.setattr(er.db, "get_run", lambda rid: {"startedAt": "2026-06-24T10:00:00+00:00"})

    md = _with_observed_evidence(
        {"source": "crm", "field": "Owner"},
        source_system="salesforce",
        source_record_id="001X",
        canonical_name="acme corp",
        confidence=1.0,
        run_id="run_3",
    )
    assert md["source"] == "crm" and md["field"] == "Owner"
    assert "evidence_pointer" in md


# ── relationships: observed vs inferred pointers (AC1) ───────────────────────


def test_observed_edge_pointer_uses_source_and_validates_without_job_id():
    p = _relationship_pointer(
        inferred=False,
        run_id="run_1",
        evidence={"source": "salesforce", "field": "OwnerId"},
        from_id="a",
        to_id="b",
        relationship_type="owns",
        confidence=0.9,
    )
    assert p.origin == "observed"
    assert p.source_system == "salesforce"
    assert p.source_artifact == "OwnerId"
    assert p.extraction_job_id is None
    assert p.is_valid()


def test_inferred_edge_pointer_names_the_run_as_job():
    p = _relationship_pointer(
        inferred=True,
        run_id="run_1",
        evidence={"detector_ids": ["d2", "d1"], "rationale": "co-firing"},
        from_id="a",
        to_id="b",
        relationship_type="depends_on",
        confidence=0.6,
    )
    assert p.origin == "inferred"
    assert p.extraction_job_id == "run_1"
    assert p.source_artifact  # derived from detector ids / fallback
    assert p.is_valid()


def test_inferred_edge_pointer_without_run_id_is_invalid():
    p = _relationship_pointer(
        inferred=True,
        run_id="",
        evidence={},
        from_id="a",
        to_id="b",
        relationship_type="depends_on",
        confidence=0.6,
    )
    assert p.is_valid() is False


# ── relationships: AC2 enforcement — invalid inferred edge is NOT persisted ──


def test_upsert_relationship_rejects_inferred_edge_without_job_id_without_db():
    # run_id="" makes the inferred pointer invalid; upsert must refuse and return
    # None BEFORE opening any DB connection (so this runs with no PostgreSQL).
    result = upsert_relationship(
        org_id="org_1",
        from_entity_id="a",
        to_entity_id="b",
        relationship_type="depends_on",
        confidence=0.6,
        inferred=True,
        run_id="",
        evidence={"detector_ids": ["d1", "d2"]},
    )
    assert result is None


# ── enrichment: inferred narrative pointer + grounding link (AC1) ────────────


def test_enrichment_artifact_gets_inferred_pointer_and_grounding_ids():
    artifact = {"aiSummary": "..."}
    _attach_enrichment_provenance(
        artifact, run_id="run_9", opp={"id": "opp_1", "evidenceIds": ["e1", "e2"]}
    )
    ptr = artifact["evidence_pointer"]
    assert ptr["origin"] == "inferred"
    assert ptr["extraction_job_id"] == "run_9"
    assert EvidencePointer.from_dict(ptr).is_valid()
    # The narrative is linked back to the evidence that grounded it.
    assert artifact["grounding_evidence_ids"] == ["e1", "e2"]


def test_enrichment_without_run_id_omits_pointer_but_keeps_grounding():
    artifact = {}
    _attach_enrichment_provenance(artifact, run_id="", opp={"id": "opp_1", "evidenceIds": []})
    # Invalid inferred provenance is omitted rather than surfaced as truth.
    assert "evidence_pointer" not in artifact
    assert artifact["grounding_evidence_ids"] == []
