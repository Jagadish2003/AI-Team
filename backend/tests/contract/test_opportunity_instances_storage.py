"""Contract tests for opportunity_instance storage (R16-B1 Part Two / T4).

Exercises the real PostgreSQL table created by migration 0017 (the contract
suite runs ``alembic upgrade head`` in conftest), proving the per-run instances
persist and can be queried back into the cross-run time series outcome tracking
(2.0) will compare.

Acceptance criteria exercised:
  * AC3 — the same finding observed in two runs stores two instances that share
    one opportunity_identity (queryable as one series).
  * AC4 — changing only confidence/score between runs keeps the identity, so both
    runs remain in the same series.
  * AC5 — a genuinely different finding stores a separate identity series.
  * AC6 — each stored instance row records opportunity_identity, run id, pack id,
    and pack version.
  * The storage supports querying every instance sharing one identity.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.opportunity_instances import (
    ensure_opportunity_instances_table,
    get_instances_by_identity,
    get_instances_for_run,
    record_opportunity_instances,
)


@pytest.fixture()
def isolated_org():
    """A unique org id so this test's identities never collide with other rows.

    Cleans up its own opportunity_instances rows afterwards so repeated runs of
    the non-resettable shared DB stay hermetic.
    """
    org = f"t4org_{uuid.uuid4().hex[:8]}"
    ensure_opportunity_instances_table()
    yield org
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM opportunity_instances WHERE org_id = %s", (org,))
        con.commit()
    finally:
        con.close()


def _opp(detector_id="covenant_tracking_gap", **overrides):
    opp = {
        "id": "opp_001",
        "packId": "ncino",
        "packVersion": "1.4.2",
        "detector_id": detector_id,
        "signal_source": "ncino",
        "impact": 4,
        "effort": 2,
        "confidence": "HIGH",
        "tier": "Strategic",
        "evidenceIds": ["ev1", "ev2"],
        "description": "narrative text",
    }
    opp.update(overrides)
    return opp


def test_ac3_ac4_same_finding_across_runs_shares_one_identity(isolated_org):
    org = isolated_org
    # Run A: confidence LOW. Run B: same finding, confidence HIGH + different score.
    record_opportunity_instances("run_a", [_opp(confidence="LOW", impact=2)], org_id=org)
    record_opportunity_instances("run_b", [_opp(confidence="HIGH", impact=5)], org_id=org)

    # Identity is deterministic from the (org, pack, detector, entities) signature.
    from discovery.opportunity_identity import (
        compute_opportunity_identity,
        primary_entity_keys_for_detector,
    )

    identity = compute_opportunity_identity(
        org_id=org,
        pack_id="ncino",
        signal_key="covenant_tracking_gap",
        primary_entity_ids=primary_entity_keys_for_detector("covenant_tracking_gap", "ncino"),
    )

    series = get_instances_by_identity(identity, org_id=org)
    # AC3: two runs of the same finding -> two instances under one identity.
    assert len(series) == 2
    assert {i.run_id for i in series} == {"run_a", "run_b"}
    assert {i.opportunity_identity for i in series} == {identity}
    # AC4: the identity held even though confidence/score changed between runs.
    assert {i.confidence for i in series} == {"LOW", "HIGH"}


def test_ac5_different_finding_is_a_separate_identity_series(isolated_org):
    org = isolated_org
    record_opportunity_instances("run_a", [_opp(detector_id="covenant_tracking_gap")], org_id=org)
    record_opportunity_instances("run_a", [_opp(detector_id="stale_loan_review")], org_id=org)

    from discovery.opportunity_identity import (
        compute_opportunity_identity,
        primary_entity_keys_for_detector,
    )

    def identity_for(detector):
        return compute_opportunity_identity(
            org_id=org, pack_id="ncino", signal_key=detector,
            primary_entity_ids=primary_entity_keys_for_detector(detector, "ncino"),
        )

    id_a = identity_for("covenant_tracking_gap")
    id_b = identity_for("stale_loan_review")
    assert id_a != id_b
    assert len(get_instances_by_identity(id_a, org_id=org)) == 1
    assert len(get_instances_by_identity(id_b, org_id=org)) == 1


def test_ac6_stored_row_has_identity_run_pack_and_version(isolated_org):
    org = isolated_org
    record_opportunity_instances("run_a", [_opp()], org_id=org)
    instances = get_instances_for_run("run_a", org_id=org)
    assert len(instances) == 1
    inst = instances[0]
    assert inst.opportunity_identity.startswith("opp_")
    assert inst.run_id == "run_a"
    assert inst.pack_id == "ncino"
    assert inst.pack_version == "1.4.2"
    # run-specific observation persisted too
    assert inst.confidence == "HIGH"
    assert inst.evidence_ids == ["ev1", "ev2"]
    assert inst.evidence_count == 2
    assert inst.narrative == "narrative text"


def test_replay_same_run_upserts_not_duplicates(isolated_org):
    org = isolated_org
    record_opportunity_instances("run_a", [_opp(confidence="LOW")], org_id=org)
    # Re-recording the same run (e.g. a replay) must refresh, not duplicate.
    record_opportunity_instances("run_a", [_opp(confidence="HIGH")], org_id=org)

    instances = get_instances_for_run("run_a", org_id=org)
    assert len(instances) == 1
    assert instances[0].confidence == "HIGH"


def test_empty_opps_writes_nothing(isolated_org):
    assert record_opportunity_instances("run_a", [], org_id=isolated_org) == 0
