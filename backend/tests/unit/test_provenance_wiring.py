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


def test_entity_metadata_carries_valid_observed_pointer():
    md = _with_observed_evidence(
        None, source_system="salesforce", source_artifact="001X", confidence=0.9
    )
    ptr = md["evidence_pointer"]
    assert ptr["origin"] == "observed"
    assert ptr["source_system"] == "salesforce"
    assert ptr["source_artifact"] == "001X"
    assert ptr["extraction_job_id"] is None  # observed needs no job id
    assert EvidencePointer.from_dict(ptr).is_valid()


def test_entity_pointer_preserves_existing_metadata():
    md = _with_observed_evidence(
        {"source": "crm", "field": "Owner"},
        source_system="salesforce",
        source_artifact="001X",
        confidence=1.0,
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
