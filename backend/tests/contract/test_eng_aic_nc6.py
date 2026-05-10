"""
ENG-AIQ-NC-6 - nCino Offline Dataset + Full Smoke Path.

Sprint 5 closes when this file exits 0.

Run:
  pytest tests/contract/test_eng_aic_nc6.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


LENDING_DETECTORS = {
    "LOAN_ORIGINATION_ROUTING_FRICTION",
    "COVENANT_TRACKING_GAP",
    "CHECKLIST_BOTTLENECK",
    "SPREADING_BOTTLENECK",
    "APPROVAL_BOTTLENECK",
}


@pytest.fixture(scope="module")
def ncino_run() -> Dict[str, Any]:
    """Run the full nCino pipeline once for all smoke tests."""
    os.environ["INGEST_MODE"] = "offline"
    try:
        from discovery.runner import run

        return run(mode="offline", pack="ncino")
    finally:
        os.environ.pop("INGEST_MODE", None)


@pytest.fixture(scope="module")
def sc_run() -> Dict[str, Any]:
    """Run the service_cloud pack for isolation tests."""
    os.environ["INGEST_MODE"] = "offline"
    try:
        from discovery.runner import run

        return run(mode="offline", pack="service_cloud")
    finally:
        os.environ.pop("INGEST_MODE", None)


@pytest.fixture(scope="module")
def opps(ncino_run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return ncino_run.get("opportunities", [])


@pytest.fixture(scope="module")
def opp_by_detector(opps: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {opp["detector_id"]: opp for opp in opps}


class TestPipelineSmokeNCino:
    def test_all_five_detectors_fire(self, opps: List[Dict[str, Any]]) -> None:
        fired = {opp["detector_id"] for opp in opps}

        for detector_id in LENDING_DETECTORS:
            assert detector_id in fired, f"Detector did not fire: {detector_id}"

    def test_run_packid_is_ncino(self, ncino_run: Dict[str, Any]) -> None:
        assert ncino_run.get("packId") == "ncino"

    def test_every_opp_has_packid_ncino(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp.get("packId") == "ncino", (
                f"Opportunity {opp['detector_id']} missing packId=ncino"
            )

    def test_all_detector_ids_are_lending(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp["detector_id"] in LENDING_DETECTORS, (
                f"Non-lending detector in ncino pack: {opp['detector_id']}"
            )

    def test_exactly_five_opportunities(self, opps: List[Dict[str, Any]]) -> None:
        assert len(opps) == 5, (
            f"Expected 5 opportunities, got {len(opps)}: "
            f"{[opp['detector_id'] for opp in opps]}"
        )


class TestScoringNC5Confirmed:
    def test_routing_friction_scoring(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("LOAN_ORIGINATION_ROUTING_FRICTION")

        assert opp is not None
        assert opp["impact"] == 7
        assert opp["tier"] == "Quick Win"
        assert opp["confidence"] == "HIGH"

    def test_covenant_tracking_scoring(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("COVENANT_TRACKING_GAP")

        assert opp is not None
        assert opp["impact"] == 9
        assert opp["tier"] == "Strategic"
        assert opp["confidence"] == "HIGH"

    def test_checklist_scoring(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("CHECKLIST_BOTTLENECK")

        assert opp is not None
        assert opp["impact"] == 7
        assert opp["tier"] == "Quick Win"
        assert opp["confidence"] == "HIGH"

    def test_spreading_scoring(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("SPREADING_BOTTLENECK")

        assert opp is not None
        assert opp["impact"] == 7
        assert opp["tier"] == "Strategic"
        assert opp["confidence"] == "HIGH"

    def test_approval_scoring(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("APPROVAL_BOTTLENECK")

        assert opp is not None
        assert opp["impact"] == 8
        assert opp["tier"] == "Strategic"
        assert opp["confidence"] == "HIGH"


class TestUILabelsNC4Approved:
    def test_every_opp_has_title(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp.get("title"), f"{opp['detector_id']} missing title"

    def test_every_opp_has_category(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp.get("category"), f"{opp['detector_id']} missing category"

    def test_covenant_title_is_banking_language(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("COVENANT_TRACKING_GAP")
        assert opp is not None
        title = opp.get("title", "").lower()

        assert any(word in title for word in ["covenant", "compliance", "monitor", "risk"]), (
            f"Covenant title not banking language: {opp.get('title')}"
        )

    def test_every_opp_has_s9_roadmap(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp.get("s9_roadmap"), f"{opp['detector_id']} missing s9_roadmap"

    def test_every_opp_has_s10_exec(self, opps: List[Dict[str, Any]]) -> None:
        for opp in opps:
            assert opp.get("s10_exec"), f"{opp['detector_id']} missing s10_exec"


class TestCorroborationEvidence:
    def test_at_least_one_opp_has_jira_evidence(
        self, opps: List[Dict[str, Any]]
    ) -> None:
        jira_found = any(
            ev.get("source") == "Jira"
            for opp in opps
            for ev in opp.get("evidence", [])
        )

        assert jira_found, "No Jira corroboration evidence found in any opportunity"

    def test_at_least_one_opp_has_sn_evidence(
        self, opps: List[Dict[str, Any]]
    ) -> None:
        sn_found = any(
            ev.get("source") == "ServiceNow"
            for opp in opps
            for ev in opp.get("evidence", [])
        )

        assert sn_found, "No ServiceNow corroboration evidence found in any opportunity"

    def test_evidence_items_have_required_keys(
        self, opps: List[Dict[str, Any]]
    ) -> None:
        for opp in opps:
            for evidence in opp.get("evidence", []):
                assert "id" in evidence, f"Evidence missing id in {opp['detector_id']}"
                assert "source" in evidence, f"Evidence missing source in {opp['detector_id']}"

                if evidence.get("source") in ("Jira", "ServiceNow"):
                    assert "detectorId" in evidence, (
                        f"Corroboration evidence missing detectorId in {opp['detector_id']}"
                    )
                    assert "snippet" in evidence, (
                        f"Corroboration evidence missing snippet in {opp['detector_id']}"
                    )

    def test_total_evidence_count_reasonable(self, opps: List[Dict[str, Any]]) -> None:
        total = sum(len(opp.get("evidence", [])) for opp in opps)

        assert total >= 5, f"Too few evidence items: {total}"


class TestComplianceGuardrails:
    def test_covenant_has_compliance_guardrail(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("COVENANT_TRACKING_GAP")
        assert opp is not None
        guardrail = opp.get("compliance_guardrail")

        assert guardrail is not None
        assert len(str(guardrail)) > 0

    def test_approval_has_compliance_guardrail(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("APPROVAL_BOTTLENECK")
        assert opp is not None
        guardrail = opp.get("compliance_guardrail")

        assert guardrail is not None
        assert len(str(guardrail)) > 0

    def test_routing_has_no_compliance_guardrail(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("LOAN_ORIGINATION_ROUTING_FRICTION")
        assert opp is not None
        assert opp.get("compliance_guardrail") is None

    def test_checklist_has_no_compliance_guardrail(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("CHECKLIST_BOTTLENECK")
        assert opp is not None
        assert opp.get("compliance_guardrail") is None

    def test_spreading_has_no_compliance_guardrail(
        self, opp_by_detector: Dict[str, Dict[str, Any]]
    ) -> None:
        opp = opp_by_detector.get("SPREADING_BOTTLENECK")
        assert opp is not None
        assert opp.get("compliance_guardrail") is None


class TestPackIsolation:
    def test_service_cloud_does_not_fire_ncino_only_detectors(
        self, sc_run: Dict[str, Any]
    ) -> None:
        ncino_only = {
            "LOAN_ORIGINATION_ROUTING_FRICTION",
            "COVENANT_TRACKING_GAP",
            "CHECKLIST_BOTTLENECK",
            "SPREADING_BOTTLENECK",
        }
        sc_detectors = {opp["detector_id"] for opp in sc_run.get("opportunities", [])}
        overlap = sc_detectors & ncino_only

        assert not overlap, f"nCino-only detectors in SC pack: {overlap}"

    def test_service_cloud_opportunities_have_sc_packid(
        self, sc_run: Dict[str, Any]
    ) -> None:
        for opp in sc_run.get("opportunities", []):
            assert opp.get("packId") == "service_cloud", (
                f"SC opportunity has wrong packId: {opp.get('packId')}"
            )


class TestConnectorHealthAll:
    def test_check_all_connectors_has_three_systems(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "SERVICENOW_URL",
                "JIRA_URL",
                "SF_INSTANCE_URL",
                "SF_ACCESS_TOKEN",
            )
        }

        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.connector_health import check_all_connectors

            result = check_all_connectors()

        assert "ServiceNow" in result
        assert "Jira" in result
        assert "nCino" in result

    def test_all_connectors_fixture_when_no_creds(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "SERVICENOW_URL",
                "JIRA_URL",
                "SF_INSTANCE_URL",
                "SF_ACCESS_TOKEN",
            )
        }

        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.connector_health import check_all_connectors

            result = check_all_connectors()

        for system in ("ServiceNow", "Jira", "nCino"):
            assert result[system]["status"] == "fixture", (
                f"{system} should be fixture when no creds set"
            )
