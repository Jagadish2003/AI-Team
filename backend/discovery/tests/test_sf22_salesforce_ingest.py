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
    from discovery.ingest.salesforce import ingest

    return ingest()


# ── Shape tests ───────────────────────────────────────────────────────────────


class TestIngestShape:
    def test_top_level_keys_present(self, sf_data):
        required = {
            "case_metrics",
            "flow_inventory",
            "approval_processes",
            "named_credentials",
            "cross_system_references",
        }
        assert required <= set(sf_data.keys()), (
            f"Missing keys: {required - set(sf_data.keys())}"
        )

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
        assert cm["handoff_score"] > 1.5, (
            f"D2 will not fire: handoff_score={cm['handoff_score']}"
        )
        assert cm["total_cases_90d"] >= 50

    def test_d3_approval_bottleneck_fires(self, sf_data):
        """D3: avg_delay_days > 3 AND bottleneck_score > 10"""
        aps = sf_data["approval_processes"]
        fires = any(
            ap["avg_delay_days"] > 3 and ap["bottleneck_score"] > 10 for ap in aps
        )
        assert fires, f"D3 will not fire: {aps}"

    def test_d4_knowledge_gap_fires(self, sf_data):
        """D4: knowledge_gap_score > 0.40 AND closed_cases >= 30"""
        cm = sf_data["case_metrics"]
        assert cm["knowledge_gap_score"] > 0.40, (
            f"D4 will not fire: knowledge_gap_score={cm['knowledge_gap_score']}"
        )
        assert cm["closed_cases_90d"] >= 30

    def test_d5_integration_concentration_fires(self, sf_data):
        """D5: at least one Named Credential with flow_reference_count >= 3"""
        ncs = sf_data["named_credentials"]
        fires = any(nc["flow_reference_count"] >= 3 for nc in ncs)
        assert fires, (
            f"D5 will not fire: {[(n['credential_name'], n['flow_reference_count']) for n in ncs]}"
        )

    def test_d6_permission_bottleneck_fires(self, sf_data):
        """D6: bottleneck_score > 10"""
        aps = sf_data["approval_processes"]
        fires = any(ap["bottleneck_score"] > 10 for ap in aps)
        assert fires, f"D6 will not fire: {aps}"

    def test_d7_cross_system_echo_fires(self, sf_data):
        """D7: sf_echo_score > 0.15"""
        csr = sf_data["cross_system_references"]
        assert csr["sf_echo_score"] > 0.15, (
            f"D7 will not fire from SF side: sf_echo_score={csr['sf_echo_score']}"
        )


# ── Function-level tests ──────────────────────────────────────────────────────


class TestIndividualFunctions:
    def test_get_case_metrics_offline(self):
        from discovery.ingest.salesforce import get_case_metrics

        result = get_case_metrics()
        assert result["total_cases_90d"] == 300
        assert result["handoff_score"] == 1.6

    def test_get_flow_inventory_offline(self):
        from discovery.ingest.salesforce import get_flow_inventory

        result = get_flow_inventory()
        assert result["flow_activity_score"] == 2.128
        assert len(result["flows"]) == 4

    def test_get_approval_pending_offline(self):
        from discovery.ingest.salesforce import get_approval_pending

        result = get_approval_pending()
        assert len(result) >= 1
        assert result[0]["process_name"] == "Discount Approval"
        assert result[0]["bottleneck_score"] == 30.0

    def test_get_knowledge_coverage_offline(self):
        from discovery.ingest.salesforce import get_knowledge_coverage

        result = get_knowledge_coverage()
        assert result["knowledge_gap_score"] == 0.5
        assert result["closed_cases_90d"] == 60

    def test_get_named_credentials_offline(self):
        """get_named_credentials returns catalog with flow refs in offline mode (fixture is pre-merged)."""
        from discovery.ingest.salesforce import get_named_credentials

        result = get_named_credentials()
        sn_cred = next(
            (c for c in result if "ServiceNow" in c["credential_name"]), None
        )
        assert sn_cred is not None
        assert sn_cred["flow_reference_count"] == 3

    def test_get_named_credential_flow_refs_offline(self):
        """In offline mode, flow_refs returns the fixture list unchanged."""
        from discovery.ingest.salesforce import (
            get_named_credential_flow_refs,
            get_named_credentials,
        )

        catalog = get_named_credentials()
        result = get_named_credential_flow_refs(catalog)
        # Offline: same as catalog (fixture already merged)
        assert result == catalog

    def test_match_type_is_name(self):
        """match_type field is always 'name' (v1 heuristic)."""
        from discovery.ingest.salesforce import get_named_credentials

        result = get_named_credentials()
        for nc in result:
            assert nc.get("match_type") == "name"

    def test_get_cross_system_references_offline(self):
        from discovery.ingest.salesforce import get_cross_system_references

        result = get_cross_system_references()
        assert result["sf_echo_score"] == 0.25
        assert "INC-" in result["matched_patterns"]


# ── Error handling tests ──────────────────────────────────────────────────────


def test_relationship_records_live_include_owner_and_record_ids(monkeypatch):
    from discovery.ingest import salesforce as sf_mod

    class Client:
        query = ""

        def soql(self, query):
            self.query = query
            return [
                {
                    "Id": "500CASE",
                    "CaseNumber": "000123",
                    "Subject": "Broken login flow",
                    "OwnerId": "005OWNER",
                }
            ]

    client = Client()
    monkeypatch.setattr(sf_mod, "is_live", lambda: True)
    result = sf_mod.get_relationship_records(client)

    assert result[0]["OwnerId"] == "005OWNER"
    assert result[0]["Subject"] == "Broken login flow"
    assert "Id, CaseNumber, Subject, OwnerId" in client.query


class TestErrorHandling:
    def test_missing_fixture_raises_ingest_error(self, tmp_path, monkeypatch):
        from discovery.ingest import salesforce as sf_mod

        monkeypatch.setattr(sf_mod, "FIXTURE_PATH", tmp_path / "nonexistent.json")
        monkeypatch.setenv("INGEST_MODE", "offline")
        with pytest.raises(sf_mod.IngestError, match="Fixture file not found"):
            sf_mod.ingest()

    def test_live_mode_without_credential_raises_ingest_error(self, monkeypatch):
        import discovery.ingest as ingest_pkg
        from discovery.ingest import salesforce as sf_mod

        # Force is_live to return True for this test
        monkeypatch.setattr(sf_mod, "is_live", lambda: True)

        # No credential in the per-run context or the vault, and an env
        # SF_INSTANCE_URL must be irrelevant (no env fallback — R191-H1 / T2).
        # _get_client imports these from the package at call time, so patch there.
        monkeypatch.setenv("SF_INSTANCE_URL", "https://env-should-never-be-used")
        monkeypatch.setattr(ingest_pkg, "get_live_connector", lambda cid: None)
        monkeypatch.setattr(ingest_pkg, "resolve_vault_connector", lambda cid: None)

        # Live ingest is credential-record-only now — with no credential, _get_client
        # raises a clear IngestError naming Salesforce (never a silent env default).
        with pytest.raises(sf_mod.IngestError, match="Salesforce credential"):
            sf_mod._get_client()

    def test_live_mode_credential_missing_url_raises_named_error(self, monkeypatch):
        # AC4: a credential record present but missing its instance URL is a LOUD,
        # NAMED configuration error — never a silent SF_INSTANCE_URL env default.
        import discovery.ingest as ingest_pkg
        from discovery.ingest import salesforce as sf_mod

        monkeypatch.setattr(sf_mod, "is_live", lambda: True)
        monkeypatch.setenv("SF_INSTANCE_URL", "https://env-should-never-be-used")
        # A credential with a token but NO url (e.g. an OAuth row whose instance URL
        # was never captured onto the per-run context).
        monkeypatch.setattr(
            ingest_pkg, "get_live_connector",
            lambda cid: {"token": "tok"} if cid == "salesforce" else None,
        )
        monkeypatch.setattr(ingest_pkg, "resolve_vault_connector", lambda cid: None)

        with pytest.raises(sf_mod.IngestError) as exc:
            sf_mod._get_client()
        msg = str(exc.value)
        assert "instance URL" in msg
        assert "salesforce" in msg                      # names the record
        assert "env-should-never-be-used" not in msg    # never leaks / uses the env value

    def test_live_mode_credential_url_used_no_env(self, monkeypatch):
        # The instance URL comes from the credential record, not the environment.
        import discovery.ingest as ingest_pkg
        from discovery.ingest import salesforce as sf_mod

        monkeypatch.setattr(sf_mod, "is_live", lambda: True)
        monkeypatch.setenv("SF_INSTANCE_URL", "https://env-should-never-be-used")
        monkeypatch.setattr(
            ingest_pkg, "get_live_connector",
            lambda cid: {"url": "https://record.my.salesforce.com", "token": "tok"}
            if cid == "salesforce" else None,
        )
        monkeypatch.setattr(ingest_pkg, "resolve_vault_connector", lambda cid: None)

        client = sf_mod._get_client()
        assert client is not None
        assert client.instance_url == "https://record.my.salesforce.com"


class TestUserNameResolution:
    """Owner/approver User Ids resolve to display names in one batched query."""

    def test_offline_ingest_includes_fixture_user_names(self, sf_data):
        # Offline fixture carries a user_names map so the knowledge graph shows
        # real names instead of raw 005... Ids without live credentials.
        names = sf_data.get("user_names") or {}
        assert names.get("005xx000001AAA1") == "Sarah Chen"
        assert names.get("005xx000001AAA2") == "Marcus Rivera"

    def test_resolve_user_names_batches_and_aliases(self):
        from discovery.ingest.salesforce import resolve_user_names

        id18 = "005Qy00000123456AB"  # canonical 18-char Salesforce Id
        assert len(id18) == 18
        id15 = id18[:15]
        calls = []

        def fake_query(q):
            calls.append(q)
            return [
                {"Id": id18, "Name": "Sarah Chen"},
                {"Id": "005Qy000002BBB", "Name": "Marcus Rivera"},
            ]

        names = resolve_user_names(fake_query, [id18, "005Qy000002BBB", id18, ""])
        # One batched query for all distinct Ids — never one per owner.
        assert len(calls) == 1
        assert names["005Qy000002BBB"] == "Marcus Rivera"
        # 18-char Id is also reachable by its 15-char case-sensitive prefix.
        assert names[id15] == "Sarah Chen"

    def test_resolve_user_names_is_graceful_on_failure(self):
        from discovery.ingest.salesforce import resolve_user_names

        def boom(q):
            raise RuntimeError("SOQL failed")

        # A lookup failure degrades to an empty map; the run never breaks.
        assert resolve_user_names(boom, ["005Qy000001AAA"]) == {}
        assert resolve_user_names(None, ["005Qy000001AAA"]) == {}
        assert resolve_user_names(lambda q: [], []) == {}
