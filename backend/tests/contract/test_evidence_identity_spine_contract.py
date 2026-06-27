"""
R16-B1 (T7) — Evidence & Identity Spine contract tests (document Section 6).

This story creates FOUNDATION behaviour: a provenance pointer on every artifact
and a stable cross-run opportunity identity. Later teams build directly on these
rules — full evidence trace (1.9), human feedback capture (1.9), intervention
modelling (1.9), outcome tracking (2.0), pack governance (1.9) — and must never
accidentally break them. These contract tests pin all eight acceptance criteria
of R16-B1 Section 6 directly against the shipped T1–T6 modules:

  AC1 — every entity, relationship, and enrichment artifact created during a run
        carries a valid EvidencePointer with the mandatory spine populated.
  AC2 — origin='inferred' with no extraction_job_id fails validation and is NOT
        persisted; observed artifacts validate without a job id.
  AC3 — the same unchanged finding across two runs yields the SAME
        opportunity_identity even though run timestamps differ.
  AC4 — changing only a finding's score/confidence between runs does NOT change
        its opportunity_identity.
  AC5 — a genuinely different finding (different entities, signal key, detector,
        org, or pack) yields a DIFFERENT opportunity_identity.
  AC6 — each opportunity_instance records opportunity_identity, run_id, pack_id,
        and pack_version.
  AC7 — evidence pointers are queryable from an opportunity back to the source
        artifacts that produced it (source_system + source_artifact + timestamp).
  AC8 — the extensible pointer fields (chunk_id, retrieval_result_id) are present
        and null in 1.6, ready for retrieval (1.8) without a schema change.

These tests are additive — they exercise the existing modules through their
public surfaces and change none of them. They use freshly-minted org/run ids so
they are hermetic against a shared, non-reset test database.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app import db
from app.provenance import EvidencePointer, INFERRED, OBSERVED


def _uid(prefix: str) -> str:
    """A collision-free id so each test is hermetic on a shared test DB."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ════════════════════════════════════════════════════════════════════════════
# AC1 — every entity / relationship / enrichment artifact carries a valid
#       EvidencePointer with the mandatory spine populated.
# ════════════════════════════════════════════════════════════════════════════

def test_ac1_created_entity_carries_valid_observed_pointer():
    """An entity created on the resolve/create write path carries an observed
    EvidencePointer; the caller's own metadata is preserved alongside it."""
    from app.entity_resolution import resolve_or_create_entity

    org = _uid("org")
    ent = resolve_or_create_entity(
        org_id=org,
        entity_type="system",
        display_name=_uid("System"),
        source_system="salesforce",
        source_record_id="001ABC",
        run_id=_uid("run"),
        metadata={"team": "credit"},
    )

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT metadata FROM entities WHERE id = %s", (str(ent.id),))
        row = cur.fetchone()
    finally:
        con.close()

    assert row is not None, "the entity must be persisted"
    md = row[0]
    md = json.loads(md) if isinstance(md, str) else md
    assert md.get("team") == "credit", "caller metadata must be preserved"

    ptr = md.get("evidence_pointer")
    assert ptr is not None, "AC1: a created entity must carry an EvidencePointer"
    assert EvidencePointer.from_dict(ptr).is_valid(), "AC1: the pointer must be valid"
    assert ptr["origin"] == OBSERVED, "an entity observed in a source is 'observed'"
    assert ptr["source_system"] == "salesforce"
    assert ptr["source_artifact"] == "001ABC"
    assert ptr["source_timestamp"], "AC1: the mandatory spine timestamp is populated"


def test_ac1_observed_relationship_edge_carries_valid_pointer():
    """A directly-observed edge carries an observed EvidencePointer."""
    from app.relationship_mapper import upsert_relationship

    rel = upsert_relationship(
        _uid("org"),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "routes_to",
        0.9,
        inferred=False,
        run_id=_uid("run"),
        evidence={"source": "salesforce", "field": "OwnerId"},
    )
    assert rel is not None, "an observed edge must persist"
    ptr = (rel.evidence or {}).get("evidence_pointer")
    assert ptr is not None, "AC1: a relationship edge must carry an EvidencePointer"
    assert EvidencePointer.from_dict(ptr).is_valid()
    assert ptr["origin"] == OBSERVED
    assert ptr["source_system"] == "salesforce"
    assert ptr["source_timestamp"], "mandatory spine timestamp populated"


def test_ac1_inferred_relationship_edge_carries_valid_pointer_with_job_id():
    """An inferred (co-firing) edge carries an inferred pointer that names the
    run as its extraction job — inferred content always names its origin."""
    from app.relationship_mapper import upsert_relationship

    run = _uid("run")
    rel = upsert_relationship(
        _uid("org"),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "depends_on",
        0.6,
        inferred=True,
        run_id=run,
        evidence={"detector_ids": ["d1", "d2"]},
    )
    assert rel is not None, "an inferred edge WITH a job id must persist"
    ptr = (rel.evidence or {}).get("evidence_pointer")
    assert EvidencePointer.from_dict(ptr).is_valid()
    assert ptr["origin"] == INFERRED
    assert ptr["extraction_job_id"] == run, "inferred edge must name its job"


def test_ac1_enrichment_artifact_carries_valid_inferred_pointer():
    """A generated enrichment narrative carries an inferred pointer and records
    the evidence ids it was grounded in."""
    from app.llm_enrichment import _attach_enrichment_provenance

    artifact = {"aiSummary": "..."}
    _attach_enrichment_provenance(
        artifact,
        run_id="RUN_77",
        opp={"id": "opp_1", "evidenceIds": ["e1", "e2"]},
        source_timestamp="2026-06-24T10:00:00+00:00",
    )
    ptr = artifact["evidence_pointer"]
    assert EvidencePointer.from_dict(ptr).is_valid()
    assert ptr["origin"] == INFERRED, "a generated narrative is inferred, never observed"
    assert ptr["extraction_job_id"] == "RUN_77"
    assert artifact["grounding_evidence_ids"] == ["e1", "e2"], (
        "AC1: the narrative records the evidence it was grounded in"
    )


@pytest.mark.parametrize("blank_field", ["source_system", "source_artifact",
                                         "source_timestamp", "origin"])
def test_ac1_missing_any_mandatory_spine_field_fails_validation(blank_field):
    """A pointer missing any mandatory spine field is invalid — the spine is the
    floor every artifact must clear."""
    fields = dict(source_system="salesforce", source_artifact="a",
                  source_timestamp="2026-06-24T00:00:00+00:00", origin=OBSERVED)
    assert EvidencePointer(**fields).is_valid(), "a full spine is valid"
    fields[blank_field] = ""
    assert not EvidencePointer(**fields).is_valid(), f"blank {blank_field} must fail"


# ════════════════════════════════════════════════════════════════════════════
# AC2 — the inferred-artifact rule: origin='inferred' with no extraction_job_id
#       fails validation and is NOT persisted; observed validates without a job.
# ════════════════════════════════════════════════════════════════════════════

def test_ac2_observed_validates_without_job_id():
    p = EvidencePointer.observed(
        source_system="jira", source_artifact="PROJ-1",
        source_timestamp="2026-06-24T00:00:00+00:00",
    )
    assert p.extraction_job_id is None
    assert p.is_valid(), "observed artifacts validate without a job id"


def test_ac2_inferred_without_job_id_fails_validation():
    missing = EvidencePointer.inferred(
        source_system="agentiq", source_artifact="opp_1",
        extraction_job_id=None, source_timestamp="2026-06-24T00:00:00+00:00",
    )
    assert not missing.is_valid()
    blank = EvidencePointer.inferred(
        source_system="agentiq", source_artifact="opp_1",
        extraction_job_id="", source_timestamp="2026-06-24T00:00:00+00:00",
    )
    assert not blank.is_valid(), "a blank job id cannot defeat the rule"


def test_ac2_inferred_with_job_id_is_valid():
    p = EvidencePointer.inferred(
        source_system="agentiq", source_artifact="opp_1",
        extraction_job_id="RUN_9", source_timestamp="2026-06-24T00:00:00+00:00",
    )
    assert p.is_valid()


def test_ac2_invalid_inferred_relationship_is_not_persisted():
    """An inferred edge with no extraction_job_id fails validation and never
    reaches the table — inferred content can never masquerade as observed truth."""
    from app.relationship_mapper import upsert_relationship

    org = _uid("org")
    from_id = str(uuid.uuid4())
    rel = upsert_relationship(
        org,
        from_id,
        str(uuid.uuid4()),
        "routes_to",
        0.6,
        inferred=True,
        run_id="",  # inferred + no job id => invalid pointer
        evidence={"detector_ids": ["d1"]},
    )
    assert rel is None, "an invalid inferred edge must be refused"

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT count(*) FROM entity_relationships "
            "WHERE org_id = %s AND from_entity_id = %s",
            (org, from_id),
        )
        count = cur.fetchone()[0]
    finally:
        con.close()
    assert count == 0, "no row may exist for a refused inferred edge"


def test_ac2_invalid_inferred_enrichment_pointer_is_omitted():
    """Enrichment with no run/job id omits the pointer rather than surfacing
    inferred output as if it were observed truth."""
    from app.llm_enrichment import _attach_enrichment_provenance

    artifact = {}
    _attach_enrichment_provenance(artifact, run_id="", opp={"id": "opp_1", "evidenceIds": []})
    assert "evidence_pointer" not in artifact


# ════════════════════════════════════════════════════════════════════════════
# AC3 / AC4 / AC5 — stable, deterministic opportunity identity.
# ════════════════════════════════════════════════════════════════════════════

def _identity(org="org-x", pack="service_cloud", signal="sla_breach",
              entities=("system:salesforce",)):
    from discovery.opportunity_identity import compute_opportunity_identity

    return compute_opportunity_identity(
        org_id=org, pack_id=pack, signal_key=signal,
        primary_entity_ids=list(entities),
    )


def test_ac3_same_finding_across_two_runs_yields_same_identity():
    """Two runs over unchanged data — the run timestamp is not an identity input,
    so the same finding resolves to the same id."""
    assert _identity() == _identity()


def test_ac3_identity_independent_of_entity_discovery_order():
    ordered = _identity(entities=("system:salesforce", "process:sla_breach"))
    reordered = _identity(entities=("process:sla_breach", "system:salesforce"))
    assert ordered == reordered, "identity must not depend on entity order"


def test_ac4_changing_only_score_or_confidence_does_not_change_identity():
    """Through the real instance-building path: two runs of one finding differing
    ONLY in score/confidence must share one identity. This is the subtlest rule
    in the story — get it wrong and nothing can be tracked over time."""
    from app.opportunity_instances import build_opportunity_instance

    base = {
        "id": "opp_1", "orgId": "org-x", "packId": "service_cloud",
        "detector_id": "sla_breach", "signal_source": "salesforce",
    }
    run1 = build_opportunity_instance(dict(base, confidence="LOW", score=10.0), "RUN_1")
    run2 = build_opportunity_instance(dict(base, confidence="HIGH", score=99.0), "RUN_2")
    assert run1.opportunity_identity == run2.opportunity_identity
    # And the run-varying measures genuinely differ between the two instances.
    assert (run1.confidence, run1.score) != (run2.confidence, run2.score)


def test_ac5_different_entities_yield_different_identity():
    assert _identity(entities=("system:salesforce",)) != _identity(entities=("system:jira",))


def test_ac5_different_signal_key_yields_different_identity():
    assert _identity(signal="sla_breach") != _identity(signal="reassignment_loop")


def test_ac5_different_pack_yields_different_identity():
    assert _identity(pack="service_cloud") != _identity(pack="ncino")


def test_ac5_different_org_yields_different_identity():
    assert _identity(org="org-a") != _identity(org="org-b")


# ════════════════════════════════════════════════════════════════════════════
# AC6 — each opportunity_instance records identity, run_id, pack_id, pack_version.
# ════════════════════════════════════════════════════════════════════════════

def _opportunity_instances_table_ready() -> bool:
    """True if the opportunity_instances table is usable in this environment.

    CI provisions it via migration 0019; a restricted local role that cannot
    CREATE the table makes the persistence round-trip un-runnable here, so those
    tests skip with a clear reason (the structure test below still runs, and CI
    asserts the full round-trip)."""
    from app.opportunity_instances import ensure_opportunity_instances_table

    ensure_opportunity_instances_table()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT to_regclass('public.opportunity_instances')")
        return cur.fetchone()[0] is not None
    finally:
        con.close()


def test_ac6_built_instance_is_self_describing():
    """The instance carries identity + run + pack + version regardless of whether
    the table is provisioned — the self-describing contract AC6 requires."""
    from app.opportunity_instances import build_opportunity_instance

    opp = {
        "id": "opp_1", "orgId": "org-x", "packId": "service_cloud",
        "packVersion": "1.0.0", "detector_id": "sla_breach",
        "signal_source": "salesforce", "confidence": "HIGH", "score": 50.0,
    }
    inst = build_opportunity_instance(opp, "RUN_42", org_id="org-x")
    assert inst.opportunity_identity, "identity present"
    assert inst.run_id == "RUN_42"
    assert inst.pack_id == "service_cloud"
    assert inst.pack_version == "1.0.0"


def test_ac6_persisted_instance_round_trips_identity_run_pack_version():
    if not _opportunity_instances_table_ready():
        pytest.skip("opportunity_instances table not provisioned here "
                    "(needs migration 0019 / CREATE privilege); asserted in CI")
    from app.opportunity_instances import (
        build_opportunity_instance,
        get_instances_by_identity,
        record_opportunity_instances,
    )

    org = _uid("org")
    run = _uid("run")
    opp = {
        "id": "opp_1", "orgId": org, "packId": "service_cloud",
        "packVersion": "1.4.2", "detector_id": "sla_breach",
        "signal_source": "salesforce", "confidence": "HIGH", "score": 50.0,
        "evidenceIds": ["e1"],
    }
    written = record_opportunity_instances(run, [opp], org_id=org)
    assert written == 1

    identity = build_opportunity_instance(opp, run, org_id=org).opportunity_identity
    rows = get_instances_by_identity(identity, org_id=org)
    assert len(rows) == 1
    inst = rows[0]
    assert inst.opportunity_identity == identity
    assert inst.run_id == run
    assert inst.pack_id == "service_cloud"
    assert inst.pack_version == "1.4.2", "AC6: the pack VERSION is stamped, not just the id"


def test_ac6_same_identity_across_runs_forms_time_series():
    """Many instances over time share one identity — the before/after series
    outcome tracking (2.0) compares. Stamping only score/confidence differently
    between runs must still collapse to one identity with two instances."""
    if not _opportunity_instances_table_ready():
        pytest.skip("opportunity_instances table not provisioned here; asserted in CI")
    from app.opportunity_instances import (
        build_opportunity_instance,
        get_instances_by_identity,
        record_opportunity_instances,
    )

    org = _uid("org")
    base = {
        "id": "opp_1", "orgId": org, "packId": "service_cloud", "packVersion": "1.0.0",
        "detector_id": "sla_breach", "signal_source": "salesforce",
    }
    run_a, run_b = _uid("run"), _uid("run")
    record_opportunity_instances(run_a, [dict(base, confidence="LOW", score=10.0)], org_id=org)
    record_opportunity_instances(run_b, [dict(base, confidence="HIGH", score=99.0)], org_id=org)

    identity = build_opportunity_instance(base, run_a, org_id=org).opportunity_identity
    rows = get_instances_by_identity(identity, org_id=org)
    assert len(rows) == 2, "two runs of one finding form a two-point time series"
    assert {r.run_id for r in rows} == {run_a, run_b}


# ════════════════════════════════════════════════════════════════════════════
# AC7 — evidence pointers are queryable from an opportunity back to its sources.
# ════════════════════════════════════════════════════════════════════════════

def test_ac7_pointers_queryable_back_to_source_artifacts():
    """Store the run's pointer index, then walk one opportunity back to the source
    artifacts that produced it (source_system + source_artifact + timestamp)."""
    from app.evidence_pointers import (
        get_evidence_pointers_for_opportunity,
        store_evidence_pointers,
    )

    run = _uid("run")
    opps = [{
        "id": "opp_1",
        "evidenceIds": ["ev_sf_1"],
        "_debug": {"detector_id": "HANDOFF_FRICTION", "signal_source": "salesforce"},
    }]
    evidence = [{"id": "ev_sf_1", "source": "Salesforce", "tsLabel": "24 Jun 2026, 10:00"}]

    stored = store_evidence_pointers(run, opps, evidence=evidence,
                                     run_completed_at="2026-06-24T10:05:00Z")
    assert stored == 1

    pointers = get_evidence_pointers_for_opportunity(run, "opp_1")
    assert pointers, "AC7: an opportunity must be queryable back to its sources"
    p = pointers[0]
    assert p["source_system"] == "salesforce"
    assert p["source_artifact"] == "ev_sf_1"
    assert p["source_timestamp"] == "24 Jun 2026, 10:00"
    # AC8 exercised here too — extensible fields present and null in 1.6.
    assert "chunk_id" in p and p["chunk_id"] is None
    assert "retrieval_result_id" in p and p["retrieval_result_id"] is None


def test_ac7_unknown_opportunity_returns_empty_trail():
    """No stored pointers => empty trail (never an error) — mirrors the route's
    'available: false' contract."""
    from app.evidence_pointers import get_evidence_pointers_for_opportunity

    assert get_evidence_pointers_for_opportunity(_uid("run"), "opp_absent") == []


# ════════════════════════════════════════════════════════════════════════════
# AC8 — the extensible pointer fields are present and null in 1.6, ready for
#       retrieval (1.8) to populate without a schema change.
# ════════════════════════════════════════════════════════════════════════════

def test_ac8_extensible_fields_present_and_null_on_serialised_pointer():
    p = EvidencePointer.observed(
        source_system="salesforce", source_artifact="a",
        source_timestamp="2026-06-24T00:00:00+00:00",
    )
    d = p.to_dict()
    for field in ("chunk_id", "retrieval_result_id", "detector_evidence_id"):
        assert field in d, f"AC8: {field} must be present in the serialised structure"
        assert d[field] is None, f"AC8: {field} must be null in 1.6"


def test_ac8_extensible_fields_populatable_without_schema_change():
    """Retrieval (1.8) fills the same structure in — no migration, no retrofit."""
    p = EvidencePointer.from_dict({
        "source_system": "salesforce", "source_artifact": "a",
        "source_timestamp": "2026-06-24T00:00:00+00:00", "origin": "observed",
        "chunk_id": "chunk_42", "retrieval_result_id": "rr_7",
    })
    assert p.chunk_id == "chunk_42"
    assert p.retrieval_result_id == "rr_7"
    assert p.is_valid()
