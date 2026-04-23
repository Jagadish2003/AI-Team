"""
SF-2.3 tests — ServiceNow ingestion module.
All tests run in offline mode. No ServiceNow credentials required.
"""
from __future__ import annotations
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


@pytest.fixture
def sn_data():
    from discovery.ingest.servicenow import ingest
    return ingest()


class TestIngestShape:
    def test_top_level_keys(self, sn_data):
        assert "incident_metrics" in sn_data
        assert "cross_system_references" in sn_data

    def test_incident_metrics_shape(self, sn_data):
        im = sn_data["incident_metrics"]
        assert isinstance(im["total_incidents_90d"], int)
        assert isinstance(im["avg_resolution_hours"], float)
        assert isinstance(im["category_breakdown"], list)

    def test_cross_system_refs_shape(self, sn_data):
        csr = sn_data["cross_system_references"]
        assert isinstance(csr["sn_match_count"], int)
        assert isinstance(csr["sn_total_incidents"], int)
        assert isinstance(csr["sn_echo_score"], float)
        assert isinstance(csr["sample_matches"], list)

    def test_echo_score_derived_from_real_total(self, sn_data):
        """Key fix from SF-2.3: echo_score = match/total, NOT hardcoded 0.0"""
        csr = sn_data["cross_system_references"]
        if csr["sn_total_incidents"] > 0:
            expected = round(csr["sn_match_count"] / csr["sn_total_incidents"], 4)
            assert abs(csr["sn_echo_score"] - expected) < 0.001, \
                f"echo_score {csr['sn_echo_score']} != match/total {expected}"


class TestD7Readiness:
    def test_sn_echo_score_fires_d7(self, sn_data):
        """D7: sn_echo_score > 0.15"""
        score = sn_data["cross_system_references"]["sn_echo_score"]
        assert score > 0.15, f"D7 will not fire from SN side: sn_echo_score={score}"

    def test_sample_matches_present(self, sn_data):
        matches = sn_data["cross_system_references"]["sample_matches"]
        assert len(matches) >= 1
        for m in matches:
            assert "incident_id" in m
            assert "pattern" in m
            assert "field" in m

    def test_cs_pattern_matched(self, sn_data):
        patterns = [m["pattern"] for m in sn_data["cross_system_references"]["sample_matches"]]
        assert any("CS-" in p or "INC" in p for p in patterns), \
            f"Expected CS- or INC patterns in matches: {patterns}"


class TestIndividualFunctions:
    def test_get_incident_metrics_offline(self):
        from discovery.ingest.servicenow import get_incident_metrics
        result = get_incident_metrics()
        assert result["total_incidents_90d"] == 500
        assert result["avg_resolution_hours"] == 18.4

    def test_get_cross_system_references_offline(self):
        from discovery.ingest.servicenow import get_cross_system_references
        result = get_cross_system_references()
        assert result["sn_echo_score"] == 0.16
        assert result["sn_match_count"] == 80
        assert result["sn_total_incidents"] == 500

    def test_echo_score_not_hardcoded_zero(self):
        """Regression test: ensures the known SF-2.3 bug is fixed."""
        from discovery.ingest.servicenow import get_cross_system_references
        result = get_cross_system_references()
        assert result["sn_echo_score"] != 0.0, \
            "echo_score is 0.0 — the hardcoded-total_count bug has re-appeared"
        assert result["sn_total_incidents"] != 0, \
            "total_count is 0 — was hardcoded again instead of fetched"


class TestErrorHandling:
    def test_missing_fixture_raises(self, tmp_path, monkeypatch):
        from discovery.ingest import servicenow as sn_mod
        monkeypatch.setattr(sn_mod, "FIXTURE_PATH", tmp_path / "missing.json")
        with pytest.raises(sn_mod.ServiceNowIngestError, match="fixture not found"):
            sn_mod.ingest()

    def test_live_no_url_returns_empty(self, monkeypatch):
        """If SERVICENOW_URL not set in live mode, ingest returns {} gracefully."""
        monkeypatch.setenv("INGEST_MODE", "live")
        monkeypatch.delenv("SERVICENOW_URL", raising=False)
        import importlib
        import discovery.ingest as pkg
        import discovery.ingest.servicenow as sn_mod
        importlib.reload(pkg)
        importlib.reload(sn_mod)
        result = sn_mod.ingest()
        assert result == {}, f"Expected empty dict, got: {result}"
