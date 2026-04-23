"""
SF-2.8 tests — Runner CLI + Demo Seeder.
All tests run in offline mode. No credentials required.
"""
from __future__ import annotations
import json
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


# ─────────────────────────────────────────────────────────────────────────────
# Runner tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRunnerOffline:

    def test_run_returns_payload_shape(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-run-001")
        for key in ("runId", "orgId", "mode", "startedAt", "completedAt",
                    "inputs", "opportunities"):
            assert key in payload, f"Missing top-level key: {key}"

    def test_run_id_preserved(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="my-explicit-run-id")
        assert payload["runId"] == "my-explicit-run-id"

    def test_mode_recorded(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-mode")
        assert payload["mode"] == "offline"

    def test_produces_seven_or_more_opportunities(self):
        """All 7 detectors fire on standard fixture — 7+ opportunities."""
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-opps")
        opps = payload["opportunities"]
        assert len(opps) >= 7, f"Expected >= 7 opportunities, got 0"

    def test_all_detectors_represented(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-detectors")
        fired_ids = {o["detector_id"] for o in payload["opportunities"]}
        expected = {
            "REPETITIVE_AUTOMATION", "HANDOFF_FRICTION", "APPROVAL_BOTTLENECK",
            "KNOWLEDGE_GAP", "INTEGRATION_CONCENTRATION",
            "PERMISSION_BOTTLENECK", "CROSS_SYSTEM_ECHO",
        }
        assert expected == fired_ids, f"Missing detectors: {expected - fired_ids}"


class TestOpportunityCandidateShape:

    @pytest.fixture
    def opportunities(self):
        from discovery.runner import run
        return run(mode="offline", run_id="test-shape")["opportunities"]

    def test_all_required_keys_present(self, opportunities):
        required = {
            "runId", "orgId", "detector_id", "signal_source",
            "metric_value", "threshold", "impact", "effort",
            "confidence", "tier", "roadmap_stage",
            "evidenceIds", "evidence", "raw_evidence", "score_debug",
        }
        for opp in opportunities:
            missing = required - set(opp.keys())
            assert not missing, f"{opp['detector_id']}: missing keys {missing}"

    def test_impact_effort_in_range(self, opportunities):
        for opp in opportunities:
            assert 1 <= opp["impact"] <= 10, f"{opp['detector_id']}: impact out of range"
            assert 1 <= opp["effort"] <= 10, f"{opp['detector_id']}: effort out of range"

    def test_confidence_valid(self, opportunities):
        for opp in opportunities:
            assert opp["confidence"] in ("HIGH", "MEDIUM", "LOW"), \
                f"{opp['detector_id']}: invalid confidence"

    def test_tier_valid(self, opportunities):
        for opp in opportunities:
            assert opp["tier"] in ("Quick Win", "Strategic", "Complex"), \
                f"{opp['detector_id']}: invalid tier"

    def test_roadmap_stage_consistent_with_tier(self, opportunities):
        stage_map = {"Quick Win": "NEXT_30", "Strategic": "NEXT_60", "Complex": "NEXT_90"}
        for opp in opportunities:
            expected = stage_map[opp["tier"]]
            assert opp["roadmap_stage"] == expected, \
                f"{opp['detector_id']}: roadmap_stage {opp['roadmap_stage']} != {expected}"

    def test_evidence_ids_match_evidence_objects(self, opportunities):
        for opp in opportunities:
            ids_in_list = [e["id"] for e in opp["evidence"]]
            assert opp["evidenceIds"] == ids_in_list, \
                f"{opp['detector_id']}: evidenceIds mismatch"

    def test_all_evidence_unreviewed(self, opportunities):
        for opp in opportunities:
            for ev in opp["evidence"]:
                assert ev["decision"] == "UNREVIEWED", \
                    f"{opp['detector_id']} evidence {ev['id']}: decision != UNREVIEWED"

    def test_evidence_snippets_contain_numbers(self, opportunities):
        import re
        for opp in opportunities:
            for ev in opp["evidence"]:
                assert re.search(r"\d", ev["snippet"]), \
                    f"{opp['detector_id']}: snippet has no digits"

    def test_run_id_on_every_opportunity(self, opportunities):
        for opp in opportunities:
            assert opp["runId"] == "test-shape"

    def test_score_debug_present(self, opportunities):
        for opp in opportunities:
            assert "impact_factors" in opp["score_debug"], \
                f"{opp['detector_id']}: score_debug missing impact_factors"


class TestRunnerInputs:

    def test_inputs_context_present(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-ctx")
        inputs = payload["inputs"]
        assert "sf_total_cases_90d" in inputs
        assert "sources_connected" in inputs
        assert inputs["sources_connected"]["salesforce"] is True

    def test_timestamps_present(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-ts")
        assert payload["startedAt"]
        assert payload["completedAt"]

    def test_json_serialisable(self):
        """Full payload must serialise to JSON — no datetime objects etc."""
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-json")
        try:
            json.dumps(payload)
        except TypeError as e:
            pytest.fail(f"Payload not JSON serialisable: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Demo seeder tests (offline — no API calls)
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoSeeder:

    def test_dry_run_sf_returns_count(self, monkeypatch):
        monkeypatch.setenv("SF_INSTANCE_URL", "https://test.salesforce.com")
        monkeypatch.setenv("SF_ACCESS_TOKEN", "test_token")
        from discovery.seed.demo_seeder import seed_salesforce, SeedState
        state = SeedState()
        count = seed_salesforce(state, dry_run=True)
        assert count == 10   # 10 clusters
        assert len(state.sf_case_ids) == 0  # dry run — nothing stored

    def test_dry_run_sn_returns_count(self, monkeypatch):
        monkeypatch.setenv("SERVICENOW_URL", "https://test.service-now.com")
        monkeypatch.setenv("SERVICENOW_TOKEN", "test_token")
        from discovery.seed.demo_seeder import seed_servicenow, SeedState
        state = SeedState()
        count = seed_servicenow(state, dry_run=True)
        assert count == 10
        assert len(state.sn_incident_sys_ids) == 0

    def test_dry_run_jira_returns_count(self, monkeypatch):
        monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "test_token")
        from discovery.seed.demo_seeder import seed_jira, SeedState
        state = SeedState()
        count = seed_jira(state, dry_run=True)
        assert count == 10
        assert len(state.jira_issue_keys) == 0

    def test_missing_sf_creds_returns_zero(self, monkeypatch):
        monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
        monkeypatch.delenv("SF_ACCESS_TOKEN", raising=False)
        from discovery.seed.demo_seeder import seed_salesforce, SeedState
        count = seed_salesforce(SeedState(), dry_run=False)
        assert count == 0

    def test_cross_system_consistency(self):
        """All clusters must have cs, inc, and jira references."""
        from discovery.seed.demo_seeder import CROSS_SYSTEM_CLUSTERS
        for c in CROSS_SYSTEM_CLUSTERS:
            assert c["cs"].startswith("CS-")
            assert c["inc"].startswith("INC-")
            assert c["jira"].startswith("JIRA-")
            assert c["cs"] in c["sn_short"] or c["cs"] in c["jira_summary"], \
                f"CS ID not in snippet: {c['cs']}"

    def test_seed_state_save_load(self, tmp_path, monkeypatch):
        from discovery.seed import demo_seeder as ds_mod
        monkeypatch.setattr(ds_mod, "SEED_STATE_PATH", tmp_path / "state.json")
        from discovery.seed.demo_seeder import SeedState
        state = SeedState(
            sf_case_ids=["sf1","sf2"],
            sn_incident_sys_ids=["sn1"],
            jira_issue_keys=["CRM-100"],
            seeded_at="2026-04-15T10:00:00Z",
        )
        state.save()
        loaded = SeedState.load()
        assert loaded.sf_case_ids == ["sf1","sf2"]
        assert loaded.jira_issue_keys == ["CRM-100"]

    def test_seed_all_dry_run(self, monkeypatch):
        monkeypatch.setenv("SF_INSTANCE_URL", "https://test.salesforce.com")
        monkeypatch.setenv("SF_ACCESS_TOKEN", "t")
        monkeypatch.setenv("SERVICENOW_URL", "https://test.service-now.com")
        monkeypatch.setenv("SERVICENOW_TOKEN", "t")
        monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "t")
        from discovery.seed.demo_seeder import seed_all
        state = seed_all(systems=["all"], dry_run=True)
        # Dry run stores nothing
        assert len(state.sf_case_ids) == 0
        assert len(state.sn_incident_sys_ids) == 0
        assert len(state.jira_issue_keys) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Track A adapter tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackAAdapter:

    @pytest.fixture
    def track_a_payload(self):
        from discovery.runner import run
        from discovery.track_a_adapter import export_track_a_seed
        import itertools
        payload = run(mode="offline", run_id="test-ta-adapt")
        return export_track_a_seed(payload, id_counter=itertools.count(1))

    def test_track_a_shape_top_level_keys(self, track_a_payload):
        for key in ("opportunities", "evidence", "run_meta"):
            assert key in track_a_payload

    def test_track_a_opportunity_has_required_ts_fields(self, track_a_payload):
        """All required Track A TypeScript OpportunityCandidate fields present."""
        required = {
            "id", "title", "category", "tier", "decision",
            "impact", "effort", "confidence", "aiRationale",
            "evidenceIds", "requiredPermissions", "override",
        }
        for opp in track_a_payload["opportunities"]:
            missing = required - set(opp.keys())
            assert not missing, f"Missing Track A fields: {missing} in {opp.get('id')}"

    def test_decision_always_unreviewed(self, track_a_payload):
        for opp in track_a_payload["opportunities"]:
            assert opp["decision"] == "UNREVIEWED"

    def test_override_shape_correct(self, track_a_payload):
        for opp in track_a_payload["opportunities"]:
            ov = opp["override"]
            assert "isLocked" in ov
            assert "rationaleOverride" in ov
            assert "overrideReason" in ov
            assert "updatedAt" in ov
            assert ov["isLocked"] is False

    def test_title_is_non_empty_string(self, track_a_payload):
        for opp in track_a_payload["opportunities"]:
            assert isinstance(opp["title"], str)
            assert len(opp["title"]) > 5, f"Title too short: '{opp['title']}'"

    def test_ai_rationale_contains_numbers(self, track_a_payload):
        import re
        for opp in track_a_payload["opportunities"]:
            assert re.search(r"\d", opp["aiRationale"]), \
                f"{opp.get('_debug',{}).get('detector_id')}: aiRationale has no numbers"

    def test_id_format(self, track_a_payload):
        for opp in track_a_payload["opportunities"]:
            assert opp["id"].startswith("opp_")

    def test_confidence_uppercase_enum(self, track_a_payload):
        for opp in track_a_payload["opportunities"]:
            assert opp["confidence"] in ("HIGH", "MEDIUM", "LOW")

    def test_evidence_shape_unchanged(self, track_a_payload):
        """Evidence objects must retain Track A EvidenceReview shape."""
        for ev in track_a_payload["evidence"]:
            for field in ("id", "tsLabel", "source", "evidenceType",
                          "title", "snippet", "entities", "confidence", "decision"):
                assert field in ev, f"Evidence missing field: {field}"

    def test_calibration_fields_in_debug_namespace(self, track_a_payload):
        """Track B calibration fields preserved under _debug, not polluting Track A schema."""
        for opp in track_a_payload["opportunities"]:
            debug = opp.get("_debug", {})
            assert "detector_id" in debug
            assert "metric_value" in debug
            assert "threshold" in debug

    def test_json_serialisable(self, track_a_payload):
        import json
        try:
            json.dumps(track_a_payload)
        except TypeError as e:
            pytest.fail(f"track_a_seed not JSON serialisable: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Feedback fix tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemsFlag:
    """Feedback 1 — --systems flag makes ingestion deterministic."""

    def test_salesforce_only_still_produces_d1_d2(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-sf-only", systems=["salesforce"])
        fired = {o["detector_id"] for o in payload["opportunities"]}
        # D1-D6 are all Salesforce-side — should all fire
        assert "REPETITIVE_AUTOMATION" in fired
        assert "HANDOFF_FRICTION" in fired

    def test_systems_list_limits_ingestion(self):
        from discovery.runner import run
        # With only salesforce, D7 may still fire (sf_echo_score side)
        # but SN/Jira data should be empty
        payload = run(mode="offline", run_id="test-sf-only-2", systems=["salesforce"])
        assert payload["inputs"]["sources_connected"]["servicenow"] is False
        assert payload["inputs"]["sources_connected"]["jira"] is False

    def test_all_systems_default_unchanged(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="test-all-sys")
        assert payload["inputs"]["sources_connected"]["salesforce"] is True


class TestOfflineExport:
    """Feedback 2 & 3 — offline_export.py is the documented one-command path."""

    def test_dry_run_no_files_written(self, tmp_path):
        from discovery.offline_export import export
        result = export(out_dir=str(tmp_path / "seed"), dry_run=True)
        # Dry run — no files
        assert not (tmp_path / "seed" / "opportunities.json").exists()
        # But result is populated
        assert len(result["opportunities"]) >= 7

    def test_export_writes_two_json_files(self, tmp_path):
        from discovery.offline_export import export
        export(out_dir=str(tmp_path / "seed"), dry_run=False)
        assert (tmp_path / "seed" / "opportunities.json").exists()
        assert (tmp_path / "seed" / "evidence.json").exists()

    def test_exported_opportunities_are_track_a_shape(self, tmp_path):
        import json
        from discovery.offline_export import export
        export(out_dir=str(tmp_path / "seed"))
        opps = json.loads((tmp_path / "seed" / "opportunities.json").read_text())
        required = {"id", "title", "category", "tier", "decision", "impact",
                    "effort", "confidence", "aiRationale", "evidenceIds",
                    "requiredPermissions", "override"}
        for opp in opps:
            missing = required - set(opp.keys())
            assert not missing, f"Missing fields: {missing}"

    def test_debug_fields_stripped_from_clean_export(self, tmp_path):
        """_debug namespace should not appear in the seed file."""
        import json
        from discovery.offline_export import export
        export(out_dir=str(tmp_path / "seed"))
        opps = json.loads((tmp_path / "seed" / "opportunities.json").read_text())
        for opp in opps:
            assert "_debug" not in opp, "_debug leaked into Track A seed file"

    def test_export_with_systems_filter(self, tmp_path):
        from discovery.offline_export import export
        result = export(
            out_dir=str(tmp_path / "seed"),
            systems=["salesforce"],
            dry_run=True,
        )
        assert len(result["opportunities"]) >= 6  # D1-D6 all SF-side

    def test_evidence_file_has_correct_shape(self, tmp_path):
        import json
        from discovery.offline_export import export
        export(out_dir=str(tmp_path / "seed"))
        evs = json.loads((tmp_path / "seed" / "evidence.json").read_text())
        assert len(evs) >= 7
        for ev in evs:
            for field in ("id", "tsLabel", "source", "evidenceType",
                          "title", "snippet", "confidence", "decision"):
                assert field in ev
