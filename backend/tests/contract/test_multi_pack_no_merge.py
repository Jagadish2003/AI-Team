"""R191-P1 T4 (AT-706) — no-merge for overlapping cross-pack findings (AC4).

When two packs run in one discovery run and both surface the SAME detector
(e.g. APPROVAL_BOTTLENECK is emitted by both `service_cloud` and `ncino`), the
two findings must remain TWO distinct findings — each keeping its own pack
provenance and evidence. Cross-pack identity/merge is an explicit non-goal.

The no-merge guarantee rests on `opportunity_identity` including `pack_id`
(R16-B1 §2), threaded end to end by T2/T3. The one place a merge COULD silently
happen is the `opportunity_instances` persistence layer, which upserts on the PK
`(opportunity_identity, run_id)`: two findings that collapsed to one identity
would overwrite each other into a single row. These tests pin that they do NOT —
at the identity level, at the persistence layer (the real merge point), and end
to end through the offline runner — so a future change that drops `pack_id` from
the identity basis, or adds a dedup keyed on `detector_id`, fails CI.

Runs against the disposable PostgreSQL test DB (conftest runs `alembic upgrade
head`, creating opportunity_instances via migration 0019). Offline runner only —
no live credentials.

Acceptance criteria: R191-P1 AC4 (overlapping opportunities from two packs remain
two findings with distinct provenance — no silent merging).
"""
from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import db
from app.opportunity_instances import (
    build_opportunity_instance,
    ensure_opportunity_instances_table,
    get_instances_by_identity,
    get_instances_for_run,
    record_opportunity_instances,
)
from discovery.opportunity_identity import (
    compute_opportunity_identity,
    primary_entity_keys_for_detector,
)
from discovery.runner import run


@pytest.fixture()
def isolated_org():
    """A unique org id so this test's rows never collide with others; cleans up
    its own opportunity_instances afterwards (the shared test DB is not reset)."""
    org = f"t4nomerge_{uuid.uuid4().hex[:8]}"
    ensure_opportunity_instances_table()
    yield org
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM opportunity_instances WHERE org_id = %s", (org,))
        con.commit()
    finally:
        con.close()


_OVERLAP_DETECTOR = "APPROVAL_BOTTLENECK"


def _overlap_opp(pack_id: str, org: str) -> dict:
    """A finding for the SAME detector/org/entities under a given pack — the exact
    overlap two packs produce. No pre-stamped identity, so persistence recomputes
    it from (org, pack, detector, entities) — the realistic stored-opp path."""
    return {
        "id": f"opp_{pack_id}",
        "orgId": org,
        "packId": pack_id,
        "packVersion": "1.0.0",
        "detector_id": _OVERLAP_DETECTOR,
        "signal_source": "salesforce",
        "impact": 3,
        "effort": 2,
        "confidence": "MEDIUM",
        "tier": "Strategic",
        "evidenceIds": [f"ev_{pack_id}"],
    }


# ── Identity level: pack_id distinguishes otherwise-identical findings ─────────

def test_same_detector_different_pack_yields_distinct_identity():
    keys = primary_entity_keys_for_detector(_OVERLAP_DETECTOR, "salesforce")
    sc = compute_opportunity_identity("org1", "service_cloud", _OVERLAP_DETECTOR, keys)
    nc = compute_opportunity_identity("org1", "ncino", _OVERLAP_DETECTOR, keys)
    assert sc != nc, "pack_id must be part of the identity — else the two merge"


# ── Persistence layer: the real merge point (ON CONFLICT upsert) ───────────────

def test_overlapping_findings_persist_as_two_distinct_instances(isolated_org):
    org = isolated_org
    run_id = "t4-persist"
    opps = [_overlap_opp("service_cloud", org), _overlap_opp("ncino", org)]

    # Distinct identities by construction (same detector, different pack).
    inst_sc = build_opportunity_instance(opps[0], run_id, org_id=org)
    inst_nc = build_opportunity_instance(opps[1], run_id, org_id=org)
    assert inst_sc.opportunity_identity != inst_nc.opportunity_identity

    # Both persist — the (identity, run_id) upsert does NOT collapse them.
    written = record_opportunity_instances(run_id, opps, org_id=org)
    assert written == 2

    stored = get_instances_for_run(run_id, org_id=org)
    assert len(stored) == 2
    assert {s.pack_id for s in stored} == {"service_cloud", "ncino"}
    assert {s.detector_id for s in stored} == {_OVERLAP_DETECTOR}  # same detector…
    assert len({s.opportunity_identity for s in stored}) == 2      # …two identities

    # Each identity is its own single-instance series — no collapse, no duplication.
    for s in stored:
        series = get_instances_by_identity(s.opportunity_identity, org_id=org)
        assert len(series) == 1
        assert series[0].pack_id == s.pack_id


# ── End to end (offline runner): no finding is lost to a merge ─────────────────

@pytest.fixture(scope="module")
def two_pack_payload():
    return run(
        mode="offline",
        run_id="t4-e2e",
        org_id="t4nomerge_e2e",
        pack_ids=["service_cloud", "ncino"],
    )


def test_every_finding_has_a_unique_identity(two_pack_payload):
    # The property that makes a merge impossible: no two findings share an
    # opportunity_identity, so the (identity, run_id) PK can never collide.
    opps = two_pack_payload["opportunities"]
    assert opps, "expected findings from a two-pack offline run"
    identities = [o["opportunity_identity"] for o in opps]
    assert len(set(identities)) == len(identities)


def test_end_to_end_run_persists_every_finding(isolated_org):
    org = isolated_org
    run_id = f"t4-e2e-persist-{uuid.uuid4().hex[:6]}"
    payload = run(
        mode="offline", run_id=run_id, org_id=org,
        pack_ids=["service_cloud", "ncino"],
    )
    opps = payload["opportunities"]
    assert opps

    record_opportunity_instances(run_id, opps, org_id=org)
    stored = get_instances_for_run(run_id, org_id=org)
    # Nothing merged away: one persisted instance per runner finding.
    assert len(stored) == len(opps)


def test_overlapping_detector_stays_two_findings_with_own_pack_and_evidence(two_pack_payload):
    by_detector: dict = {}
    for o in two_pack_payload["opportunities"]:
        by_detector.setdefault(o["detector_id"], []).append(o)

    # Detectors surfaced by BOTH packs (the overlap AC4 is about).
    cross_pack = {
        det: findings
        for det, findings in by_detector.items()
        if len({f["packId"] for f in findings}) > 1
    }
    assert cross_pack, (
        "expected at least one detector surfaced by both packs "
        "(APPROVAL_BOTTLENECK) to exercise the no-merge guarantee"
    )

    for det, findings in cross_pack.items():
        # Two (or more) findings, one per pack — never merged into one.
        identities = {f["opportunity_identity"] for f in findings}
        assert len(identities) == len(findings), f"{det}: findings merged"
        # Each finding keeps its OWN pack provenance and evidence (T3): an
        # evidence item never carries a different pack's id.
        for f in findings:
            for evd in f["evidence"]:
                assert evd.get("packId") == f["packId"]
