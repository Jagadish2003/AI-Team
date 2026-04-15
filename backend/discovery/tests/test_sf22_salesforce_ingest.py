"""
SF-2.2 tests — Salesforce ingestion module.
All tests run in offline mode against the fixture file.
No Salesforce credentials required.
"""
from __future__ import annotations
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sf_data():
    from backend.discovery.ingest.salesforce import ingest
    return ingest()


# ── Shape tests ───────────────────────────────────────────────────────────────

class TestIngestShape:
    def test_top_level_keys_present(self, sf_data):
        required = {
            "case_metrics", "flow_inventory",
            "approval_processes", "named_credentials",
            "cross_system_references",
        }
        assert required <= set(sf_data.keys()), \
            f"Missing keys: {required - set(sf_data.keys())}"

    def test_case_metrics_shape(self, sf_data):
        cm = sf_data["case_metrics"]
        assert isinstance(cm["total_cases_90d"], int)
        assert isinstance(cm["closed_cases_90d"], int)
        assert isinstance(cm["owner_changes_90d"], int)
        assert isinstance(cm["handoff_score"], float)
        assert isinstance(cm["cases_with_kb_link"], int)
        assert isinstance(cm["knowledge_gap_score"], float)
        assert isinstance(cm["category_breakdown"], list)

    def test_flow_inventory_shape(self, sf_data):
        fi = sf_data["flow_inventory"]
        assert isinstance(fi["active_flow_count_on_object"], int)
        assert isinstance(fi["avg_element_count"], float)
        assert isinstance(fi["flow_activity_score"], float)
        assert isinstance(fi["flows"], list)
        for flow in fi["flows"]:
            assert "flow_id" in flow
            assert "flow_label" in flow
            assert "element_count" in flow

    def test_approval_processes_shape(self, sf_data):
        aps = sf_data["approval_processes"]
        assert isinstance(aps, list)
        assert len(aps) >= 1
        for ap in aps:
            assert "process_name" in ap
            assert "pending_count" in ap
            assert "avg_delay_days" in ap
            assert "approver_count" in ap
            assert "bottleneck_score" in ap

    def test_named_credentials_shape(self, sf_data):
        ncs = sf_data["named_credentials"]
        assert isinstance(ncs, list)
        for nc in ncs:
            assert "credential_name" in nc
            assert "credential_developer_name" in nc
            assert isinstance(nc["flow_reference_count"], int)
            assert isinstance(nc["referencing_flow_ids"], list)

    def test_cross_system_refs_shape(self, sf_data):
        csr = sf_data["cross_system_references"]
        assert isinstance(csr["sf_echo_count"], int)
        assert isinstance(csr["sf_total_cases"], int)
        assert isinstance(csr["sf_echo_score"], float)
        assert isinstance(csr["matched_patterns"], list)


# ── Detector readiness tests ──────────────────────────────────────────────────

class TestDetectorReadiness:
    """Confirm fixture values fire each of the 7 detectors."""

    def test_d1_repetitive_automation_fires(self, sf_data):
        """D1: flow_activity_score > 0.6"""
        score = sf_data["flow_inventory"]["flow_activity_score"]
        assert score > 0.6, f"D1 will not fire: flow_activity_score={score}"

    def test_d2_handoff_friction_fires(self, sf_data):
        """D2: handoff_score > 1.5 AND total_cases >= 50"""
        cm = sf_data["case_metrics"]
        assert cm["handoff_score"] > 1.5, f"D2 will not fire: handoff_score={cm['handoff_score']}"
        assert cm["total_cases_90d"] >= 50

    def test_d3_approval_bottleneck_fires(self, sf_data):
        """D3: avg_delay_days > 3 AND bottleneck_score > 10"""
        aps = sf_data["approval_processes"]
        fires = any(
            ap["avg_delay_days"] > 3 and ap["bottleneck_score"] > 10
            for ap in aps
        )
        assert fires, f"D3 will not fire: {aps}"

    def test_d4_knowledge_gap_fires(self, sf_data):
        """D4: knowledge_gap_score > 0.40 AND closed_cases >= 30"""
        cm = sf_data["case_metrics"]
        assert cm["knowledge_gap_score"] > 0.40, \
            f"D4 will not fire: knowledge_gap_score={cm['knowledge_gap_score']}"
        assert cm["closed_cases_90d"] >= 30

    def test_d5_integration_concentration_fires(self, sf_data):
        """D5: at least one Named Credential with flow_reference_count >= 3"""
        ncs = sf_data["named_credentials"]
        fires = any(nc["flow_reference_count"] >= 3 for nc in ncs)
        assert fires, f"D5 will not fire: {[(n['credential_name'], n['flow_reference_count']) for n in ncs]}"

    def test_d6_permission_bottleneck_fires(self, sf_data):
        """D6: bottleneck_score > 10"""
        aps = sf_data["approval_processes"]
        fires = any(ap["bottleneck_score"] > 10 for ap in aps)
        assert fires, f"D6 will not fire: {aps}"

    def test_d7_cross_system_echo_fires(self, sf_data):
        """D7: sf_echo_score > 0.15"""
        csr = sf_data["cross_system_references"]
        assert csr["sf_echo_score"] > 0.15, \
            f"D7 will not fire from SF side: sf_echo_score={csr['sf_echo_score']}"


# ── Function-level tests ──────────────────────────────────────────────────────

class TestIndividualFunctions:
    def test_get_case_metrics_offline(self):
        from backend.discovery.ingest.salesforce import get_case_metrics
        result = get_case_metrics()
        assert result["total_cases_90d"] == 300
        assert result["handoff_score"] == 1.6

    def test_get_flow_inventory_offline(self):
        from backend.discovery.ingest.salesforce import get_flow_inventory
        result = get_flow_inventory()
        assert result["flow_activity_score"] == 2.128
        assert len(result["flows"]) == 4

    def test_get_approval_pending_offline(self):
        from backend.discovery.ingest.salesforce import get_approval_pending
        result = get_approval_pending()
        assert len(result) >= 1
        assert result[0]["process_name"] == "Discount Approval"
        assert result[0]["bottleneck_score"] == 30.0

    def test_get_knowledge_coverage_offline(self):
        from backend.discovery.ingest.salesforce import get_knowledge_coverage
        result = get_knowledge_coverage()
        assert result["knowledge_gap_score"] == 0.5
        assert result["closed_cases_90d"] == 60

    def test_get_named_credentials_offline(self):
        """get_named_credentials returns catalog with flow refs in offline mode (fixture is pre-merged)."""
        from backend.discovery.ingest.salesforce import get_named_credentials
        result = get_named_credentials()
        sn_cred = next((c for c in result if "ServiceNow" in c["credential_name"]), None)
        assert sn_cred is not None
        assert sn_cred["flow_reference_count"] == 3

    def test_get_named_credential_flow_refs_offline(self):
        """In offline mode, flow_refs returns the fixture list unchanged."""
        from backend.discovery.ingest.salesforce import (
            get_named_credentials, get_named_credential_flow_refs
        )
        catalog = get_named_credentials()
        result = get_named_credential_flow_refs(catalog)
        # Offline: same as catalog (fixture already merged)
        assert result == catalog

    def test_match_type_is_name(self):
        """match_type field is always 'name' (v1 heuristic)."""
        from backend.discovery.ingest.salesforce import get_named_credentials
        result = get_named_credentials()
        for nc in result:
            assert nc.get("match_type") == "name"

    def test_get_cross_system_references_offline(self):
        from backend.discovery.ingest.salesforce import get_cross_system_references
        result = get_cross_system_references()
        assert result["sf_echo_score"] == 0.25
        assert "INC-" in result["matched_patterns"]


# ── Error handling tests ──────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_fixture_raises_ingest_error(self, tmp_path, monkeypatch):
        from backend.discovery.ingest import salesforce as sf_mod
        monkeypatch.setattr(sf_mod, "FIXTURE_PATH", tmp_path / "nonexistent.json")
        monkeypatch.setenv("INGEST_MODE", "offline")
        with pytest.raises(sf_mod.IngestError, match="Fixture file not found"):
            sf_mod.ingest()

    def test_live_mode_without_env_vars_raises_ingest_error(self, monkeypatch):
        monkeypatch.setenv("INGEST_MODE", "live")
        monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
        monkeypatch.delenv("SF_ACCESS_TOKEN", raising=False)
        from backend.discovery.ingest import salesforce as sf_mod
        # Reload to pick up env change
        import importlib
        import backend.discovery.ingest as ingest_pkg
        importlib.reload(ingest_pkg)
        importlib.reload(sf_mod)
        with pytest.raises(sf_mod.IngestError, match="SF_INSTANCE_URL"):
            sf_mod._get_client()
