"""R191-P1 T3 (AT-705) — provenance tagging: packId on every finding, evidence
bundle item, and roadmap entry; pack_ids + version stamps on the run record.

Findings and the run record's multi-pack surface (packId/packVersion on each
opportunity; packIds/packVersions/packs on the run payload) were already
stamped by T1/T2 (see test_multi_pack_execution.py, test_multi_pack_run_config.py).
This file is scoped to what T3 actually adds:

  * Every EVIDENCE item — not just the finding it belongs to — carries the
    packId of the pack that produced it. This covers evidence built via
    discovery.evidence_builder.build_evidence() (including the nCino/STRS
    banking-language builders and the CROSS_SYSTEM_ECHO supplemental item)
    AND the Jira/ServiceNow corroboration evidence constructed inline in
    discovery/runner.py (a separate code path that does not go through
    build_evidence()).
  * Every ROADMAP entry carries packId/packVersion. build_roadmap() buckets
    opportunities into stages without reconstructing them, so this holds by
    construction once findings carry packId — this test pins that behaviour
    so a future refactor of roadmap_engine.py cannot silently strip it.
  * The full track_a_adapter conversion (the layer between the runner payload
    and what is actually stored/served) preserves packId on both the stored
    opportunity AND every evidence item — the story's evidence gap lived here.

Runs the OFFLINE runner directly (fixtures; no live credentials, no DB),
matching test_multi_pack_execution.py's convention.

Acceptance Criteria covered (R191-P1)
--------------------------------------
AC1: every finding AND its evidence bundle carries the correct packId in a
     multi-pack run.
AC6: evidence pointers on every finding resolve within the finding's own pack
     context — extended here to mean each evidence item is itself provenance-
     tagged, not just reachable via its owning finding's evidenceIds.
"""
from __future__ import annotations

import os

os.environ["INGEST_MODE"] = "offline"

from discovery.evidence_builder import build_evidence  # noqa: E402
from discovery.models import DetectorResult  # noqa: E402
from discovery.packs.pack_config import get_pack_version  # noqa: E402
from discovery.runner import run  # noqa: E402
from discovery.track_a_adapter import export_track_a_seed  # noqa: E402
from app.roadmap_engine import build_roadmap  # noqa: E402


def _detector_result(detector_id: str = "REPETITION", signal_source: str = "salesforce") -> DetectorResult:
    return DetectorResult(
        detector_id=detector_id,
        signal_source=signal_source,
        metric_value=10.0,
        threshold=5.0,
        raw_evidence={
            "case_ids": ["case_001", "case_002"],
            "count": 2,
            "window_days": 90,
        },
    )


# ---------------------------------------------------------------------------
# Unit level — evidence_builder.build_evidence() stamps packId directly.
# ---------------------------------------------------------------------------


def test_build_evidence_stamps_pack_id_on_every_item():
    dr = _detector_result(detector_id="APPROVAL_BOTTLENECK")
    opportunity = {"confidence": "MEDIUM", "packId": "service_cloud"}
    evidence = build_evidence(dr, opportunity, id_factory=lambda: "test001")
    assert evidence, "expected at least one evidence item for a known detector"
    for ev in evidence:
        assert ev["packId"] == "service_cloud"


def test_build_evidence_stamps_ncino_pack_id():
    """The pack-specific (banking-language) builder path also gets stamped —
    the packId gate that SELECTS the builder must not skip the stamp."""
    dr = _detector_result(detector_id="APPROVAL_BOTTLENECK")
    opportunity = {"confidence": "HIGH", "packId": "ncino"}
    evidence = build_evidence(dr, opportunity, id_factory=lambda: "test002")
    assert evidence
    for ev in evidence:
        assert ev["packId"] == "ncino"


def test_build_evidence_omits_pack_id_when_absent():
    """No packId in the opportunity (e.g. a caller that never set one) must not
    fabricate a value — absent, not empty-string-on-every-item noise."""
    dr = _detector_result(detector_id="APPROVAL_BOTTLENECK")
    opportunity = {"confidence": "MEDIUM"}
    evidence = build_evidence(dr, opportunity, id_factory=lambda: "test003")
    assert evidence
    for ev in evidence:
        assert "packId" not in ev


# ---------------------------------------------------------------------------
# End-to-end (offline runner) — single-pack and multi-pack.
# ---------------------------------------------------------------------------


def _assert_every_evidence_item_matches_its_finding(opportunities):
    for opp in opportunities:
        pack_id = opp["packId"]
        for ev in opp["evidence"]:
            assert ev.get("packId") == pack_id, (
                f"evidence {ev.get('id')} on finding {opp.get('detector_id')} "
                f"(pack={pack_id}) carries packId={ev.get('packId')!r}"
            )


def test_single_pack_run_stamps_every_evidence_item():
    payload = run(mode="offline", run_id="prov-single", pack_ids=["service_cloud"])
    opportunities = payload["opportunities"]
    assert opportunities, "expected at least one finding"
    _assert_every_evidence_item_matches_its_finding(opportunities)


def test_multi_pack_run_stamps_every_evidence_item_with_its_own_pack():
    payload = run(
        mode="offline", run_id="prov-multi", pack_ids=["service_cloud", "ncino"]
    )
    opportunities = payload["opportunities"]
    packs_seen = {o["packId"] for o in opportunities}
    assert {"service_cloud", "ncino"}.issubset(packs_seen)
    _assert_every_evidence_item_matches_its_finding(opportunities)


def test_multi_pack_run_evidence_never_carries_the_other_packs_id():
    """A finding's evidence must never carry a DIFFERENT pack's id — the exact
    cross-pack leak this task closes."""
    payload = run(
        mode="offline", run_id="prov-cross-check", pack_ids=["service_cloud", "ncino"]
    )
    for opp in payload["opportunities"]:
        other_pack = "ncino" if opp["packId"] == "service_cloud" else "service_cloud"
        for ev in opp["evidence"]:
            assert ev.get("packId") != other_pack


# ---------------------------------------------------------------------------
# The track_a_adapter layer — what is actually stored/served — preserves
# packId on both the opportunity AND every evidence item.
# ---------------------------------------------------------------------------


def test_track_a_seed_preserves_pack_id_on_opportunities_and_evidence():
    payload = run(
        mode="offline", run_id="prov-seed", pack_ids=["service_cloud", "ncino"]
    )
    seed = export_track_a_seed(payload)
    stored_opps = seed["opportunities"]
    stored_evidence = seed["evidence"]

    assert stored_opps, "expected stored opportunities"
    for opp in stored_opps:
        assert opp.get("packId") in ("service_cloud", "ncino")
        assert opp.get("packVersion") == get_pack_version(opp["packId"])

    assert stored_evidence, "expected stored evidence"
    for ev in stored_evidence:
        assert ev.get("packId") in ("service_cloud", "ncino"), (
            f"stored evidence {ev.get('id')} missing/invalid packId"
        )


# ---------------------------------------------------------------------------
# Roadmap entries — opportunities embedded in PilotRoadmap stages carry
# packId/packVersion (build_roadmap buckets by reference; pinned so a future
# refactor cannot silently strip it).
# ---------------------------------------------------------------------------


def test_roadmap_stage_opportunities_carry_pack_id_and_version():
    payload = run(
        mode="offline", run_id="prov-roadmap", pack_ids=["service_cloud", "ncino"]
    )
    seed = export_track_a_seed(payload)
    stored_opps = seed["opportunities"]
    # APPROVED items land in a stage unconditionally; mark a few so at least
    # one stage is populated regardless of the fixture's UNREVIEWED default mix.
    for opp in stored_opps[:3]:
        opp["decision"] = "APPROVED"

    roadmap = build_roadmap(stored_opps)
    staged_opps = [o for stage in roadmap["stages"] for o in stage["opportunities"]]
    assert staged_opps, "expected at least one opportunity across roadmap stages"
    for opp in staged_opps:
        assert opp.get("packId") in ("service_cloud", "ncino")
        assert opp.get("packVersion") == get_pack_version(opp["packId"])


# ---------------------------------------------------------------------------
# Run record — pack_ids with each pack's version stamp (AC6). Already built by
# T2 (see test_multi_pack_execution.py); this pins it as part of T3's own
# acceptance-criteria claim rather than relying solely on a prior task's tests.
# ---------------------------------------------------------------------------


def test_run_record_carries_pack_ids_with_version_stamps():
    payload = run(
        mode="offline", run_id="prov-run-record", pack_ids=["service_cloud", "ncino"]
    )
    assert payload["packIds"] == ["service_cloud", "ncino"]
    assert payload["packVersions"] == {
        "service_cloud": get_pack_version("service_cloud"),
        "ncino": get_pack_version("ncino"),
    }
    packs_by_id = {p["packId"]: p for p in payload["packs"]}
    assert set(packs_by_id) == {"service_cloud", "ncino"}
    for pack_id, meta in packs_by_id.items():
        assert meta["packVersion"] == get_pack_version(pack_id)
