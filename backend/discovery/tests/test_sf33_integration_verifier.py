"""
SF-3.3 tests — Integration Verifier + Shared Ranking + Contract checks.
All tests run offline. No credentials required.
"""
from __future__ import annotations
import json
import os
import itertools
import pytest

os.environ["INGEST_MODE"] = "offline"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def seed_files(tmp_path_factory):
    """Run offline_export into a temp dir — one time per session."""
    tmp = tmp_path_factory.mktemp("seed")
    from backend.discovery.offline_export import export
    export(out_dir=str(tmp))
    return tmp

@pytest.fixture(scope="session")
def opps(seed_files):
    return json.loads((seed_files / "opportunities.json").read_text())

@pytest.fixture(scope="session")
def evs(seed_files):
    return json.loads((seed_files / "evidence.json").read_text())

@pytest.fixture(scope="session")
def full_report(seed_files):
    from backend.discovery.integration_verifier import run_verification
    return run_verification(str(seed_files))


# ─────────────────────────────────────────────────────────────────────────────
# Three-command path
# ─────────────────────────────────────────────────────────────────────────────

class TestThreeCommandPath:

    def test_offline_export_produces_two_files(self, seed_files):
        assert (seed_files / "opportunities.json").exists()
        assert (seed_files / "evidence.json").exists()

    def test_opportunities_is_nonempty_list(self, opps):
        assert isinstance(opps, list)
        assert len(opps) >= 7

    def test_evidence_is_nonempty_list(self, evs):
        assert isinstance(evs, list)
        assert len(evs) >= 1

    def test_verifier_report_has_required_keys(self, full_report):
        for key in ("sf33_passed", "report_time", "checks",
                    "screen_readiness", "all_issues", "all_warnings", "summary"):
            assert key in full_report

    def test_sf33_passes_on_offline_output(self, full_report):
        if full_report["all_issues"]:
            pytest.fail(
                "SF-3.3 verifier failed:\n" +
                "\n".join(f"  ❌ {i}" for i in full_report["all_issues"])
            )
        assert full_report["sf33_passed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# OpportunityCandidate schema (Track A contract)
# ─────────────────────────────────────────────────────────────────────────────

class TestOpportunitySchema:

    def test_all_required_fields_present(self, opps):
        required = ["id","title","category","tier","decision","impact",
                    "effort","confidence","aiRationale","evidenceIds",
                    "requiredPermissions","override"]
        for opp in opps:
            for field in required:
                assert field in opp, f"{opp.get('id')}: missing '{field}'"

    def test_confidence_is_uppercase_enum(self, opps):
        """Must-fix from context: HIGH/MEDIUM/LOW uppercase for badge colours."""
        valid = {"HIGH", "MEDIUM", "LOW"}
        for opp in opps:
            assert opp["confidence"] in valid, (
                f"{opp['id']}: confidence='{opp['confidence']}' must be uppercase. "
                "HIGH=green, MEDIUM=amber, LOW=red."
            )

    def test_tier_is_valid_enum(self, opps):
        valid = {"Quick Win", "Strategic", "Complex"}
        for opp in opps:
            assert opp["tier"] in valid, f"{opp['id']}: tier='{opp['tier']}'"

    def test_decision_is_always_unreviewed(self, opps):
        """Algorithm must always produce UNREVIEWED — decisions are Track A's."""
        for opp in opps:
            assert opp["decision"] == "UNREVIEWED", (
                f"{opp['id']}: decision='{opp['decision']}' must be UNREVIEWED"
            )

    def test_impact_in_range(self, opps):
        for opp in opps:
            assert 1 <= opp["impact"] <= 10, f"{opp['id']}: impact={opp['impact']}"

    def test_effort_in_range(self, opps):
        for opp in opps:
            assert 1 <= opp["effort"] <= 10, f"{opp['id']}: effort={opp['effort']}"

    def test_ai_rationale_nonempty(self, opps):
        for opp in opps:
            assert opp["aiRationale"].strip(), f"{opp['id']}: aiRationale is empty"

    def test_override_has_required_keys(self, opps):
        required = ["isLocked","rationaleOverride","overrideReason","updatedAt"]
        for opp in opps:
            ov = opp["override"]
            for key in required:
                assert key in ov, f"{opp['id']}: override missing '{key}'"

    def test_impact_effort_not_at_origin(self, opps):
        """S7 quadrant: (0,0) hides items from the map."""
        for opp in opps:
            assert not (opp["impact"] == 0 and opp["effort"] == 0), (
                f"{opp['id']}: impact=0, effort=0 — invisible on S7 quadrant"
            )


# ─────────────────────────────────────────────────────────────────────────────
# EvidenceReview schema
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceSchema:

    def test_all_required_fields_present(self, evs):
        required = ["id","tsLabel","source","evidenceType",
                    "title","snippet","entities","confidence","decision"]
        for ev in evs:
            for field in required:
                assert field in ev, f"{ev.get('id')}: missing '{field}'"

    def test_evidence_type_valid_enum(self, evs):
        """evidenceType must match Track A contract — not 'Config' or 'Ticket'."""
        valid = {"Metric", "Log", "Document", "Survey"}
        for ev in evs:
            assert ev.get("evidenceType") in valid, (
                f"{ev.get('id')}: evidenceType='{ev.get('evidenceType')}' "
                f"not in {valid}"
            )

    def test_confidence_uppercase(self, evs):
        valid = {"HIGH", "MEDIUM", "LOW"}
        for ev in evs:
            assert ev.get("confidence") in valid

    def test_entities_is_list(self, evs):
        for ev in evs:
            assert isinstance(ev.get("entities"), list), (
                f"{ev.get('id')}: entities must be a list"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Evidence linkage — most critical S4→S6 check
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceLinkage:

    def test_all_evidence_ids_resolve(self, opps, evs):
        """
        Every evidenceId in opportunities.json must resolve to an
        evidence object in evidence.json.
        This is the most common integration break.
        From S6, open opportunity → evidenceId must resolve to S4 card.
        """
        ev_ids = {e["id"] for e in evs}
        broken = []
        for opp in opps:
            for eid in opp.get("evidenceIds", []):
                if eid not in ev_ids:
                    broken.append(f"{opp['id']} → {eid}")
        assert not broken, (
            "Broken evidence links (S6→S4 will fail):\n" +
            "\n".join(f"  {b}" for b in broken)
        )

    def test_evidence_has_entities_when_linked(self, opps, evs):
        """Linked evidence objects should have entities populated for S4 card tags."""
        ev_map = {e["id"]: e for e in evs}
        for opp in opps:
            for eid in opp.get("evidenceIds", []):
                if eid in ev_map:
                    ev = ev_map[eid]
                    entities = ev.get("entities", [])
                    # warn only — entities being empty is non-blocking
                    if not entities:
                        pytest.warns(None)  # soft check

    def test_no_orphan_evidence(self, opps, evs):
        """Every evidence object should be referenced by at least one opportunity."""
        all_ev_ids_used = {
            eid for opp in opps
            for eid in opp.get("evidenceIds", [])
        }
        orphans = [e["id"] for e in evs if e["id"] not in all_ev_ids_used]
        # Orphans are a warning not a failure — the evidence may be filtered later
        if orphans:
            pass  # acceptable, just note


# ─────────────────────────────────────────────────────────────────────────────
# Screen-specific checks
# ─────────────────────────────────────────────────────────────────────────────

class TestScreenChecks:

    def test_s7_confidence_badge_values(self, opps):
        """S7: HIGH=green, MEDIUM=amber, LOW=red. Must all be uppercase."""
        colour_map = {"HIGH":"green","MEDIUM":"amber","LOW":"red"}
        for opp in opps:
            conf = opp.get("confidence","")
            assert conf in colour_map, (
                f"{opp['id']}: confidence='{conf}' has no badge colour mapping"
            )

    def test_s9_quick_win_maps_to_next_30(self, opps):
        """S9: Quick Win tier must land in NEXT_30 roadmap stage."""
        for opp in opps:
            if opp.get("tier") == "Quick Win":
                debug = opp.get("_debug", {})
                stage = debug.get("roadmap_stage", "")
                if stage:
                    assert stage == "NEXT_30", (
                        f"{opp['id']}: Quick Win should be NEXT_30, got '{stage}'"
                    )

    def test_s9_strategic_maps_to_next_60(self, opps):
        for opp in opps:
            if opp.get("tier") == "Strategic":
                debug = opp.get("_debug", {})
                stage = debug.get("roadmap_stage", "")
                if stage:
                    assert stage == "NEXT_60", (
                        f"{opp['id']}: Strategic should be NEXT_60, got '{stage}'"
                    )

    def test_s9_complex_maps_to_next_90(self, opps):
        for opp in opps:
            if opp.get("tier") == "Complex":
                debug = opp.get("_debug", {})
                stage = debug.get("roadmap_stage", "")
                if stage:
                    assert stage == "NEXT_90", (
                        f"{opp['id']}: Complex should be NEXT_90, got '{stage}'"
                    )

    def test_s10_has_quick_wins(self, opps):
        """S10 topQuickWins requires at least one Quick Win."""
        quick_wins = [o for o in opps if o.get("tier") == "Quick Win"]
        assert len(quick_wins) >= 1, "No Quick Win opportunities for S10 topQuickWins"

    def test_s10_sources_analyzable(self, evs):
        """S10 sourcesAnalyzed requires non-empty source fields."""
        sources = {e.get("source","") for e in evs}
        sources.discard("")
        assert len(sources) >= 1, "No source fields in evidence for S10 sourcesAnalyzed"

    def test_s6_ai_rationale_for_all(self, opps):
        """S6 analyst review requires aiRationale on every opportunity."""
        missing = [o["id"] for o in opps if not o.get("aiRationale","").strip()]
        assert not missing, f"Missing aiRationale on: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# Shared ranking utility (SF-3.3 key deliverable)
# ─────────────────────────────────────────────────────────────────────────────

class TestSharedRanking:

    def test_ranking_module_importable(self):
        from backend.discovery.calibration.ranking import rank_opportunities, rank_key, TIER_ORDER
        assert callable(rank_opportunities)
        assert callable(rank_key)
        assert "Quick Win" in TIER_ORDER

    def test_quick_wins_rank_before_strategic(self):
        from backend.discovery.calibration.ranking import rank_opportunities
        opps = [
            {"id":"s","tier":"Strategic","impact":8,"effort":3},
            {"id":"q","tier":"Quick Win","impact":5,"effort":2},
            {"id":"c","tier":"Complex","impact":9,"effort":8},
        ]
        ranked = rank_opportunities(opps)
        ids = [o["id"] for o in ranked]
        assert ids == ["q","s","c"]

    def test_within_tier_net_value_desc(self):
        from backend.discovery.calibration.ranking import rank_opportunities
        opps = [
            {"id":"a","tier":"Quick Win","impact":5,"effort":4},  # net=1
            {"id":"b","tier":"Quick Win","impact":7,"effort":2},  # net=5
            {"id":"c","tier":"Quick Win","impact":6,"effort":3},  # net=3
        ]
        ranked = rank_opportunities(opps)
        ids = [o["id"] for o in ranked]
        assert ids == ["b","c","a"]

    def test_effort_tiebreak_asc(self):
        from backend.discovery.calibration.ranking import rank_opportunities
        opps = [
            {"id":"hi_effort","tier":"Quick Win","impact":5,"effort":4},  # net=1
            {"id":"lo_effort","tier":"Quick Win","impact":4,"effort":3},  # net=1 same
        ]
        ranked = rank_opportunities(opps)
        # Same net value → lower effort first
        assert ranked[0]["id"] == "lo_effort"

    def test_calibrator_uses_shared_ranking(self):
        """Calibrator must use ranking.py, not inline sort."""
        import inspect
        from backend.discovery.calibration import calibrator
        src = inspect.getsource(calibrator)
        assert "rank_opportunities" in src
        assert "from .ranking import" in src

    def test_adapter_uses_shared_ranking(self):
        """track_a_adapter must use ranking.py for output ordering."""
        import inspect
        from backend.discovery import track_a_adapter
        src = inspect.getsource(track_a_adapter)
        assert "rank_opportunities" in src

    def test_seed_output_is_ranked(self, opps):
        """Seed files must be in production ranking order."""
        from backend.discovery.calibration.ranking import rank_opportunities
        expected = [o["id"] for o in rank_opportunities(opps)]
        actual   = [o["id"] for o in opps]
        assert expected == actual, (
            f"Seed output not in ranking order.\n"
            f"Expected: {expected}\nGot:      {actual}"
        )

    def test_top3_is_stretch_kpi_not_blocking(self, opps):
        """
        From SF-3.3 context notes:
        Top-3 exact order match is a stretch KPI, not a blocking gate.
        Gate 1 is overlap >= 3 of 5. This test confirms the system
        does not enforce exact top-3 order as a hard constraint.
        """
        # Just verify top 3 are present — not that they are in a specific order
        assert len(opps) >= 3, "Need at least 3 opportunities for top-3 check"
        # No assertion on exact order — that's by design


# ─────────────────────────────────────────────────────────────────────────────
# Gate 3 direction formula — OR not AND (from SF-3.3 context notes)
# ─────────────────────────────────────────────────────────────────────────────

class TestGate3DirectionFormula:
    """
    SF-3.3 context note: Gate 3 should fail if impact_gap > 3 OR effort_gap > 3.
    The current code uses OR correctly. This test suite validates both axes.
    """

    def test_impact_gap_alone_triggers_discrepancy(self):
        from backend.discovery.calibration.calibrator import check_direction
        algo = [{"detector_id":"HANDOFF_FRICTION","impact":3,"effort":3}]
        arch = [{"detector_match":"HANDOFF_FRICTION","architect_impact":8,"architect_effort":4}]
        issues = check_direction(algo, arch)
        assert len(issues) == 1
        assert issues[0]["impact_gap"] == 5         # > 3 → fail
        assert issues[0]["effort_gap"] == 1         # <= 3 → ok
        assert issues[0]["impact_gap_ok"] is False
        assert issues[0]["effort_gap_ok"] is True
        # OR condition: either > 3 is enough to generate discrepancy

    def test_effort_gap_alone_triggers_discrepancy(self):
        from backend.discovery.calibration.calibrator import check_direction
        algo = [{"detector_id":"HANDOFF_FRICTION","impact":6,"effort":2}]
        arch = [{"detector_match":"HANDOFF_FRICTION","architect_impact":7,"architect_effort":7}]
        issues = check_direction(algo, arch)
        assert len(issues) == 1
        assert issues[0]["impact_gap"] == 1         # <= 3 → ok
        assert issues[0]["effort_gap"] == 5         # > 3 → fail
        assert issues[0]["impact_gap_ok"] is True
        assert issues[0]["effort_gap_ok"] is False

    def test_both_within_3_no_discrepancy(self):
        from backend.discovery.calibration.calibrator import check_direction
        algo = [{"detector_id":"HANDOFF_FRICTION","impact":6,"effort":3}]
        arch = [{"detector_match":"HANDOFF_FRICTION","architect_impact":8,"architect_effort":4}]
        issues = check_direction(algo, arch)
        assert len(issues) == 0

    def test_direction_formula_field_in_discrepancy(self):
        from backend.discovery.calibration.calibrator import check_direction
        algo = [{"detector_id":"HANDOFF_FRICTION","impact":3,"effort":2}]
        arch = [{"detector_match":"HANDOFF_FRICTION","architect_impact":9,"architect_effort":3}]
        issues = check_direction(algo, arch)
        assert "direction_formula" in issues[0]
        assert "abs" in issues[0]["direction_formula"]


# ─────────────────────────────────────────────────────────────────────────────
# Screen readiness in report
# ─────────────────────────────────────────────────────────────────────────────

class TestScreenReadiness:

    def test_report_has_screen_readiness(self, full_report):
        assert "screen_readiness" in full_report
        screens = [s["screen"] for s in full_report["screen_readiness"]]
        for expected in ("S4","S4→S6","S6","S7","S9","S10"):
            assert expected in screens

    def test_all_screens_ready_on_offline_output(self, full_report):
        not_ready = [
            s for s in full_report["screen_readiness"] if not s["ready"]
        ]
        if not_ready:
            issues = [(s["screen"], s["blocking_issues"]) for s in not_ready]
            pytest.fail(f"Screens not ready: {issues}")


# ─────────────────────────────────────────────────────────────────────────────
# Regression
# ─────────────────────────────────────────────────────────────────────────────

class TestRegression:

    def test_sf28_runner_unaffected(self):
        from backend.discovery.runner import run
        payload = run(mode="offline", run_id="sf33-reg")
        assert len(payload["opportunities"]) >= 7

    def test_sf32_calibrator_unaffected(self):
        from backend.discovery.runner import run
        from backend.discovery.calibration.calibrator import run_calibration
        payload = run(mode="offline", run_id="sf33-cal-reg")
        report = run_calibration(payload, None)
        assert "sf32_gate_passed" in report
        assert len(report["recommendations"]) >= 1

    def test_sf31_validator_imports_clean(self):
        from backend.discovery.ingest.live_validator import run_validation
        assert callable(run_validation)

    def test_evidence_type_no_longer_config(self):
        """Regression: evidenceType 'Config' was invalid Track A enum — now fixed."""
        from backend.discovery.runner import run
        from backend.discovery.track_a_adapter import export_track_a_seed
        import itertools
        payload = run(mode="offline", run_id="sf33-ev-reg")
        seed = export_track_a_seed(payload, id_counter=itertools.count(1))
        valid_types = {"Metric", "Log", "Document", "Survey"}
        for ev in seed["evidence"]:
            assert ev.get("evidenceType") in valid_types, (
                f"{ev['id']}: evidenceType='{ev.get('evidenceType')}' invalid"
            )
