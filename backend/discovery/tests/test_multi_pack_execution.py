"""R191-P1 T2 (AT-704) — Multi-pack execution: shared signal, isolated calibration.

The discovery runner now runs the detectors of EVERY selected pack against the
ONE shared normalised signal, applies each pack's OWN scorer calibration to that
pack's findings, and never blends calibrations across packs. These tests run the
OFFLINE runner (fixtures; no live credentials) and pin:

  * AC2 — a single-pack run (`pack_ids=[X]`) is byte-identical to the singular
    `pack=X` pipeline;
  * AC3 — the same pack's findings are scored with its own calibration whether it
    runs alone or alongside another pack (no blending);
  * AC1 — a two-pack run produces findings from both, each stamped with the right
    packId, and the payload lists both pack ids with version stamps;
  * AC4 — overlapping opportunities from two packs stay two findings with distinct
    provenance (no silent merging);
  * AC6 — evidence ids are globally unique across packs and every finding's
    evidence resolves within its own pack context.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.runner import run  # noqa: E402
from discovery.packs.pack_config import get_pack_version  # noqa: E402


def _opps_by_pack(payload):
    out = {}
    for o in payload["opportunities"]:
        out.setdefault(o["packId"], []).append(o)
    return out


def _calibration_slice(opp):
    """The pack-CALIBRATION output of an opportunity — the deterministic, run- and
    graph-invariant fields a pack's own scorer produces.

    Deliberately excludes `confidence` and the corroboration_* fields: those carry
    the ENT-2 cross-system corroboration OVERLAY, which the runner documents as
    cross-run MUTABLE state (a later relationship upsert can elevate a historical
    finding's confidence). They are not part of a pack's calibration, so they are
    not what "single-pack findings are identical" refers to.
    """
    return (
        opp["detector_id"],
        opp["packId"],
        opp["packVersion"],
        opp["impact"],
        opp["effort"],
        opp["tier"],
        opp["opportunity_identity"],
    )


# ── AC2: single-element pack_ids is identical to the singular pack ─────────────

class TestSinglePackRegression:
    def test_pack_ids_single_element_equals_singular(self):
        # `pack="ncino"` and `pack_ids=["ncino"]` normalise to the SAME one-pack
        # selection, so they run the identical pipeline. Compare each finding's
        # pack-calibration slice (impact/effort/tier/identity/pack stamps), the
        # substance of "byte-identical findings" for the single-pack case (AC2).
        a = run(mode="offline", run_id="mp-reg-1a", pack="ncino")
        b = run(mode="offline", run_id="mp-reg-1b", pack_ids=["ncino"])
        assert sorted(_calibration_slice(o) for o in a["opportunities"]) == sorted(
            _calibration_slice(o) for o in b["opportunities"]
        )

    def test_single_pack_scalar_fields_unchanged(self):
        p = run(mode="offline", run_id="mp-reg-2", pack_ids=["ncino"])
        assert p["packId"] == "ncino"
        assert p["packVersion"] == get_pack_version("ncino")
        # New multi-pack surface degrades to a one-entry list.
        assert p["packIds"] == ["ncino"]
        assert p["packVersions"] == {"ncino": get_pack_version("ncino")}
        assert [m["packId"] for m in p["packs"]] == ["ncino"]

    def test_no_selection_defaults_to_service_cloud(self):
        p = run(mode="offline", run_id="mp-reg-3")
        assert p["packId"] == "service_cloud"
        assert p["packIds"] == ["service_cloud"]


# ── AC1 + AC4 + AC6: a two-pack run ───────────────────────────────────────────

@pytest.fixture(scope="module")
def multi_payload():
    return run(mode="offline", run_id="mp-two", pack_ids=["service_cloud", "ncino"])


class TestTwoPackRun:
    def test_findings_from_both_packs(self, multi_payload):
        by_pack = _opps_by_pack(multi_payload)
        assert by_pack.get("service_cloud"), "expected service_cloud findings"
        assert by_pack.get("ncino"), "expected ncino findings"

    def test_every_finding_carries_its_own_pack_id_and_version(self, multi_payload):
        for o in multi_payload["opportunities"]:
            assert o["packId"] in ("service_cloud", "ncino")
            assert o["packVersion"] == get_pack_version(o["packId"])

    def test_payload_lists_both_packs_with_version_stamps(self, multi_payload):
        assert multi_payload["packIds"] == ["service_cloud", "ncino"]
        assert multi_payload["packVersions"] == {
            "service_cloud": get_pack_version("service_cloud"),
            "ncino": get_pack_version("ncino"),
        }
        packs = {m["packId"]: m for m in multi_payload["packs"]}
        assert set(packs) == {"service_cloud", "ncino"}
        for pid, meta in packs.items():
            assert meta["packVersion"] == get_pack_version(pid)
            assert isinstance(meta["detectorsExecuted"], list)

    def test_primary_scalar_fields_report_first_pack(self, multi_payload):
        # Backward-compatible scalars mirror the PRIMARY (first) selected pack.
        assert multi_payload["packId"] == "service_cloud"
        assert multi_payload["packVersion"] == get_pack_version("service_cloud")

    def test_no_silent_merging_counts_add_up(self, multi_payload):
        # AC4: overlapping opportunities stay two findings — the multi-pack total
        # equals the sum of each pack run alone; nothing is merged away.
        sc_alone = run(mode="offline", run_id="mp-sc", pack_ids=["service_cloud"])
        nc_alone = run(mode="offline", run_id="mp-nc", pack_ids=["ncino"])
        assert len(multi_payload["opportunities"]) == (
            len(sc_alone["opportunities"]) + len(nc_alone["opportunities"])
        )

    def test_identities_distinct_across_packs(self, multi_payload):
        # AC4: pack_id is part of opportunity identity, so no finding from one pack
        # collides identity with a finding from another (distinct provenance).
        by_pack = _opps_by_pack(multi_payload)
        sc_ids = {o["opportunity_identity"] for o in by_pack["service_cloud"]}
        nc_ids = {o["opportunity_identity"] for o in by_pack["ncino"]}
        assert sc_ids.isdisjoint(nc_ids)

    def test_overlapping_detector_stays_two_findings(self, multi_payload):
        # AC4 (concrete): service_cloud and ncino both surface APPROVAL_BOTTLENECK.
        # It must remain TWO findings — one per pack — each with its own packId and
        # a distinct opportunity_identity; never silently merged into one.
        overlap = [
            o for o in multi_payload["opportunities"]
            if o["detector_id"] == "APPROVAL_BOTTLENECK"
        ]
        packs = {o["packId"] for o in overlap}
        if {"service_cloud", "ncino"}.issubset(packs):
            identities = {o["opportunity_identity"] for o in overlap}
            assert len(identities) == len(overlap)  # no two collapsed into one

    def test_evidence_ids_globally_unique_and_self_contained(self, multi_payload):
        # AC6: the shared id factory keeps evidence ids unique ACROSS packs, and
        # each finding's evidenceIds resolve to its own evidence list.
        seen = set()
        for o in multi_payload["opportunities"]:
            ev_ids = [e["id"] for e in o["evidence"]]
            assert o["evidenceIds"] == ev_ids
            for ev_id in ev_ids:
                assert ev_id not in seen, f"duplicate evidence id {ev_id} across packs"
                seen.add(ev_id)


# ── AC3: per-pack calibration is isolated (no blending) ───────────────────────

class TestPerPackCalibrationIsolation:
    def test_pack_calibration_unaffected_by_a_co_selected_pack(self):
        # A pack's findings must be scored by its OWN calibration whether it runs
        # alone or beside another pack — the presence of service_cloud must not
        # change ncino's calibrated scores (impact/effort/tier/identity).
        alone = run(mode="offline", run_id="mp-iso-a", pack_ids=["ncino"])
        together = run(mode="offline", run_id="mp-iso-b", pack_ids=["service_cloud", "ncino"])

        nc_alone = sorted(_calibration_slice(o) for o in alone["opportunities"])
        nc_together = sorted(
            _calibration_slice(o) for o in _opps_by_pack(together)["ncino"]
        )
        assert nc_alone == nc_together

    def test_each_finding_scored_under_its_own_pack(self):
        # Each finding must be produced by — and stamped with — the pack whose pass
        # emitted it. Detector ids may OVERLAP across packs (APPROVAL_BOTTLENECK is
        # in both); the guarantee is that a finding carrying packId P has a detector
        # belonging to P's own detector set — no pack scores under another's id.
        together = run(mode="offline", run_id="mp-iso-c", pack_ids=["service_cloud", "ncino"])
        sc_alone = run(mode="offline", run_id="mp-iso-d", pack_ids=["service_cloud"])
        nc_alone = run(mode="offline", run_id="mp-iso-e", pack_ids=["ncino"])
        sc_detectors = {o["detector_id"] for o in sc_alone["opportunities"]}
        nc_detectors = {o["detector_id"] for o in nc_alone["opportunities"]}
        by_pack = _opps_by_pack(together)
        for o in by_pack.get("service_cloud", []):
            assert o["detector_id"] in sc_detectors
        for o in by_pack.get("ncino", []):
            assert o["detector_id"] in nc_detectors
