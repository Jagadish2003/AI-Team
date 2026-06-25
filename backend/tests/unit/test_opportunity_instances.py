"""Unit tests for opportunity-instance building + identity (R16-B1 Part Two / T4).

These are DB-free: they exercise the pure ``build_opportunity_instance`` builder
and the deterministic identity it stamps. Storage round-trip / cross-run query is
covered in the contract suite (test_opportunity_instances_storage.py).

Acceptance criteria exercised here (the parts owned by T4):
  * AC6 — each built instance records opportunity_identity, run id, pack id, and
    pack version.
  * AC3 — the SAME finding in two runs yields the SAME opportunity_identity
    (only the run id / measures differ), so their instances group together.
  * AC4 — changing only confidence / score between runs does NOT change the
    identity, so the two instances still share one identity.
  * AC5 — a genuinely different finding (different detector / signal source)
    yields a DIFFERENT identity, so its instance is a separate series.
"""
from __future__ import annotations

from app.opportunity_instances import (
    build_opportunity_instance,
    stamp_opportunity_identities,
)
from database.models.opportunity_instances import DEFAULT_PACK_VERSION


def _raw_opp(**overrides):
    """A raw-runner-shaped opportunity dict (orgId/detector_id at top level)."""
    opp = {
        "id": "opp_001",
        "runId": "run_aaa",
        "orgId": "acme",
        "packId": "ncino",
        "packVersion": "1.0.0",
        "detector_id": "covenant_tracking_gap",
        "signal_source": "ncino",
        "impact": 4,
        "effort": 2,
        "confidence": "HIGH",
        "tier": "Strategic",
        "evidenceIds": ["ev1", "ev2", "ev3"],
        "description": "Covenant tracking gap across 12 accounts",
    }
    opp.update(overrides)
    return opp


# --------------------------------------------------------------------------- #
# AC6 — the instance records identity, run id, pack id, pack version
# --------------------------------------------------------------------------- #

def test_ac6_instance_records_identity_run_pack_and_version():
    inst = build_opportunity_instance(_raw_opp(), run_id="run_aaa")
    assert inst.opportunity_identity.startswith("opp_")
    assert inst.run_id == "run_aaa"
    assert inst.pack_id == "ncino"
    assert inst.pack_version == "1.0.0"
    assert inst.org_id == "acme"


def test_ac6_pack_version_falls_back_to_default_when_absent():
    opp = _raw_opp()
    opp.pop("packVersion")
    inst = build_opportunity_instance(opp, run_id="run_aaa")
    assert inst.pack_version == DEFAULT_PACK_VERSION


def test_ac6_pack_version_uses_opp_stamp_when_present():
    inst = build_opportunity_instance(_raw_opp(packVersion="2.3.1"), run_id="run_aaa")
    assert inst.pack_version == "2.3.1"


# --------------------------------------------------------------------------- #
# AC3 — same finding, two runs -> same identity, different run id
# --------------------------------------------------------------------------- #

def test_ac3_same_finding_two_runs_share_identity():
    a = build_opportunity_instance(_raw_opp(runId="run_a"), run_id="run_a")
    b = build_opportunity_instance(_raw_opp(runId="run_b"), run_id="run_b")
    assert a.opportunity_identity == b.opportunity_identity
    assert a.run_id != b.run_id


# --------------------------------------------------------------------------- #
# AC4 — changing only confidence / score does NOT change identity
# --------------------------------------------------------------------------- #

def test_ac4_changing_confidence_and_score_keeps_identity():
    run1 = build_opportunity_instance(
        _raw_opp(confidence="LOW", impact=2, effort=4), run_id="run_a"
    )
    run2 = build_opportunity_instance(
        _raw_opp(confidence="HIGH", impact=5, effort=1), run_id="run_b"
    )
    # Same underlying problem -> same identity, even though the measures moved.
    assert run1.opportunity_identity == run2.opportunity_identity
    # ...and the run-varying measures are captured per-instance.
    assert (run1.confidence, run1.impact, run1.effort) == ("LOW", 2, 4)
    assert (run2.confidence, run2.impact, run2.effort) == ("HIGH", 5, 1)


# --------------------------------------------------------------------------- #
# AC5 — a genuinely different finding -> different identity
# --------------------------------------------------------------------------- #

def test_ac5_different_detector_yields_different_identity():
    a = build_opportunity_instance(_raw_opp(detector_id="covenant_tracking_gap"), run_id="r")
    b = build_opportunity_instance(_raw_opp(detector_id="stale_loan_review"), run_id="r")
    assert a.opportunity_identity != b.opportunity_identity


def test_ac5_different_signal_source_yields_different_identity():
    a = build_opportunity_instance(_raw_opp(signal_source="ncino"), run_id="r")
    b = build_opportunity_instance(_raw_opp(signal_source="salesforce"), run_id="r")
    assert a.opportunity_identity != b.opportunity_identity


# --------------------------------------------------------------------------- #
# Run-specific observation values are captured
# --------------------------------------------------------------------------- #

def test_run_specific_values_are_captured():
    inst = build_opportunity_instance(_raw_opp(), run_id="run_aaa")
    assert inst.impact == 4
    assert inst.effort == 2
    assert inst.confidence == "HIGH"
    assert inst.tier == "Strategic"
    assert inst.evidence_ids == ["ev1", "ev2", "ev3"]
    assert inst.evidence_count == 3
    assert inst.narrative == "Covenant tracking gap across 12 accounts"


def test_narrative_prefers_ai_rationale_then_description_then_title():
    inst = build_opportunity_instance(
        _raw_opp(aiRationale="AI says X", description="desc", title="t"), run_id="r"
    )
    assert inst.narrative == "AI says X"


# --------------------------------------------------------------------------- #
# T3 compatibility + Track A shape
# --------------------------------------------------------------------------- #

def test_uses_prestamped_identity_when_present():
    """When T3's runner wiring already stamped opportunity_identity, reuse it
    verbatim rather than recomputing."""
    inst = build_opportunity_instance(
        _raw_opp(opportunity_identity="opp_preexisting123"), run_id="r"
    )
    assert inst.opportunity_identity == "opp_preexisting123"


def test_reads_track_a_shape_detector_from_debug():
    """The Track A stored opp keeps detector_id/signal_source under _debug."""
    track_a_opp = {
        "id": "opp_005",
        "packId": "ncino",
        "impact": 3,
        "effort": 2,
        "confidence": "MEDIUM",
        "tier": "Quick Win",
        "evidenceIds": ["e1"],
        "_debug": {"detector_id": "covenant_tracking_gap", "signal_source": "ncino"},
    }
    inst = build_opportunity_instance(track_a_opp, run_id="r", org_id="acme")
    assert inst.detector_id == "covenant_tracking_gap"
    assert inst.signal_source == "ncino"
    assert inst.opportunity_identity.startswith("opp_")


def test_org_id_argument_overrides_opp_org():
    inst = build_opportunity_instance(_raw_opp(orgId="from_opp"), run_id="r", org_id="explicit")
    assert inst.org_id == "explicit"


# --------------------------------------------------------------------------- #
# stamp_opportunity_identities — "store identity on every opportunity" (§2)
# --------------------------------------------------------------------------- #

def test_stamp_adds_identity_to_each_opportunity_in_place():
    opps = [_raw_opp(detector_id="d1"), _raw_opp(detector_id="d2")]
    n = stamp_opportunity_identities(opps, run_id="r", org_id="acme")
    assert n == 2
    assert all(o["opportunity_identity"].startswith("opp_") for o in opps)
    # different findings -> different stamped identities
    assert opps[0]["opportunity_identity"] != opps[1]["opportunity_identity"]


def test_stamp_is_deterministic_and_idempotent():
    opp = _raw_opp()
    stamp_opportunity_identities([opp], run_id="r1", org_id="acme")
    first = opp["opportunity_identity"]
    # Re-stamping on a later run yields the SAME identity (run-invariant).
    stamp_opportunity_identities([opp], run_id="r2", org_id="acme")
    assert opp["opportunity_identity"] == first


def test_stamp_skips_malformed_opp_without_raising():
    # An opp with no detector_id / pack cannot yield an identity — skipped, not raised.
    bad = {"id": "opp_x"}
    n = stamp_opportunity_identities([bad], run_id="r", org_id="acme")
    assert n == 0
    assert "opportunity_identity" not in bad


def test_to_db_row_round_trips_through_from_db_row():
    from database.models.opportunity_instances import OpportunityInstance

    inst = build_opportunity_instance(_raw_opp(), run_id="run_aaa")
    row = inst.to_db_row()
    restored = OpportunityInstance.from_db_row(row)
    assert restored.opportunity_identity == inst.opportunity_identity
    assert restored.evidence_ids == inst.evidence_ids
    assert restored.evidence_count == inst.evidence_count
    assert restored.pack_version == inst.pack_version
