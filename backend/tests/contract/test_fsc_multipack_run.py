"""2.0-D1 AC2 — the FSC template is selectable alongside Lending IN ONE RUN.

AC2's wording is "selectable alongside Lending in one run (multi-pack regression)".
``test_fsc_template.py`` covers the COMPOSITION half (resolve_launch_config returns
both packs with separate boundaries) and the CALIBRATION half (each pack's scorer
claims only its own detectors). Neither drives an actual run, so this file closes
that gap at the two levels a run actually happens at:

  1. ``TestLaunchEndpointSelectsBothPacks`` — POST /api/stack-builder/launch with
     both ``template_ids`` puts BOTH packs on the run record. This is the
     "selectable" half, exercised through the real API rather than the resolver.
  2. ``TestCombinedRunProducesBothPacksFindings`` — ``discovery.runner.run`` with
     both packs produces both packs' findings, each scored by its OWN pack's
     scorer, each stamped with its own pack version, and the two approval findings
     stay TWO distinguished by ``opportunity_identity``.

That last assertion is the point AC2 rests on and the one a naive reading gets
wrong: both packs surface an approval bottleneck, so the correct outcome of a
combined run is two findings, not one. Cross-pack merging is a permanent non-goal
(R191-P1), so this is not duplication and the test asserts it rather than
"deduplicating" it.

Runs against the disposable PostgreSQL test DB (conftest `alembic upgrade head`).
"""
from __future__ import annotations

import os
from collections import Counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rbac import seed_owner

_DEV_TOKEN = "dev-token-change-me"

FSC_TEMPLATE = "financial_services_cloud"
LENDING_TEMPLATE = "commercial_lending"
FSC_PACK = "financial_services_cloud"
LENDING_PACK = "ncino"
BOTH_PACKS = [FSC_PACK, LENDING_PACK]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(org_id: str) -> dict:
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _owner_org(prefix: str) -> str:
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, _DEV_TOKEN)
    return org_id


@pytest.fixture(scope="module")
def combined_run():
    """One offline run with BOTH packs selected — the actual multi-pack run."""
    os.environ["INGEST_MODE"] = "offline"
    from discovery.runner import run

    return run(mode="offline", pack_ids=list(BOTH_PACKS))


# ── 1. Selectable: the launch endpoint puts both packs on the run ───────────────

class TestLaunchEndpointSelectsBothPacks:

    def test_launching_with_both_templates_activates_both_packs(self, client):
        org = _owner_org("d1_ac2_launch")
        body = {
            "org_id": org,
            "template_ids": [FSC_TEMPLATE, LENDING_TEMPLATE],
            "selected_system_ids": ["salesforce_fsc", "salesforce_ncino"],
        }
        resp = client.post(
            "/api/stack-builder/launch", headers=_auth(org), json=body
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["packIds"] == BOTH_PACKS
        # Backward-compatible scalar mirrors the primary (first-selected) pack.
        assert data["packId"] == FSC_PACK

        run_record = client.get(
            f"/api/runs/{data['runId']}", headers=_auth(org)
        ).json()
        assert run_record["packIds"] == BOTH_PACKS
        assert run_record["packId"] == FSC_PACK

    def test_selection_order_is_honoured_at_launch(self, client):
        org = _owner_org("d1_ac2_order")
        body = {
            "org_id": org,
            "template_ids": [LENDING_TEMPLATE, FSC_TEMPLATE],
            "selected_system_ids": ["salesforce_ncino", "salesforce_fsc"],
        }
        resp = client.post(
            "/api/stack-builder/launch", headers=_auth(org), json=body
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["packIds"] == [LENDING_PACK, FSC_PACK]

    def test_the_fsc_template_alone_activates_only_its_pack(self, client):
        org = _owner_org("d1_ac2_solo")
        resp = client.post(
            "/api/stack-builder/launch",
            headers=_auth(org),
            json={
                "org_id": org,
                "template_ids": [FSC_TEMPLATE],
                "selected_system_ids": ["salesforce_fsc"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["packIds"] == [FSC_PACK]

    def test_the_template_is_served_by_the_templates_endpoint(self, client):
        """"Selectable" starts with the picker being able to show it."""
        resp = client.get("/api/stack-builder/templates", headers=_auth("default"))
        assert resp.status_code == 200, resp.text
        rows = {row["template_id"]: row for row in resp.json()}
        assert FSC_TEMPLATE in rows
        assert rows[FSC_TEMPLATE]["pack_id"] == FSC_PACK
        assert "salesforce_fsc" in rows[FSC_TEMPLATE]["suggested_systems"]
        assert rows[FSC_TEMPLATE]["suggested_roles"]["salesforce_fsc"] == (
            "system_of_record"
        )
        assert rows[FSC_TEMPLATE]["focus_defaults"]["focus_id"] == (
            "member_customer_service"
        )


# ── 2. One run, both packs' findings, separately calibrated ────────────────────

class TestCombinedRunProducesBothPacksFindings:

    def test_the_run_records_both_packs(self, combined_run):
        assert combined_run["packIds"] == BOTH_PACKS
        assert combined_run["packId"] == FSC_PACK

    def test_each_pack_is_stamped_with_its_own_version(self, combined_run):
        from discovery.packs.pack_config import get_pack_version

        versions = combined_run["packVersions"]
        assert set(versions) == set(BOTH_PACKS)
        for pack_id, version in versions.items():
            assert version == get_pack_version(pack_id)
        # And they are genuinely different versions — not one blended value.
        assert versions[FSC_PACK] != versions[LENDING_PACK]

    def test_both_packs_produced_findings(self, combined_run):
        by_pack = Counter(
            opp.get("packId") for opp in combined_run["opportunities"]
        )
        assert by_pack[FSC_PACK] > 0, "FSC produced no findings in the combined run"
        assert by_pack[LENDING_PACK] > 0, "Lending produced no findings"

    def test_fsc_contributes_at_least_four_detectors(self, combined_run):
        """AC1 must still hold inside a combined run, not only alone."""
        detectors = {
            opp.get("detector_id")
            for opp in combined_run["opportunities"]
            if opp.get("packId") == FSC_PACK
        }
        assert len(detectors) >= 4, sorted(detectors)

    def test_every_finding_is_scored_by_its_own_packs_scorer(self, combined_run):
        """The non-blending guarantee, observed on a real run's output."""
        for opp in combined_run["opportunities"]:
            scorer = (opp.get("score_debug") or {}).get("scorer")
            if opp.get("packId") == FSC_PACK:
                assert scorer == "financial_services_cloud", opp.get("detector_id")
            elif opp.get("packId") == LENDING_PACK:
                assert scorer == "lending", opp.get("detector_id")

    def test_no_fsc_finding_was_scored_with_lending_calibration(self, combined_run):
        from discovery.lending_scorer import is_lending_detector

        for opp in combined_run["opportunities"]:
            if opp.get("packId") == FSC_PACK:
                assert not is_lending_detector(opp.get("detector_id", ""))

    def test_every_fsc_finding_still_carries_the_four_part_contract(self, combined_run):
        from discovery.packs.fsc_finding import (
            FOUR_PART_CONTRACT_FIELDS,
            missing_contract_parts,
        )

        fsc_opps = [
            o for o in combined_run["opportunities"] if o.get("packId") == FSC_PACK
        ]
        assert fsc_opps
        for opp in fsc_opps:
            contract = (opp.get("raw_evidence") or {}).get("finding_contract")
            assert contract, (
                f"{opp.get('detector_id')} carries no finding_contract through to "
                f"the opportunity"
            )
            assert missing_contract_parts(contract) == [], (
                f"{opp.get('detector_id')} missing {FOUR_PART_CONTRACT_FIELDS}"
            )

    def test_the_two_approval_findings_stay_two(self, combined_run):
        """Both packs surface an approval bottleneck. A naive reading expects one
        finding; the correct outcome is TWO, because cross-pack merging is a
        permanent non-goal."""
        approvals = [
            opp for opp in combined_run["opportunities"]
            if "APPROVAL" in str(opp.get("detector_id", ""))
        ]
        packs = {opp.get("packId") for opp in approvals}
        assert packs == set(BOTH_PACKS), (
            f"expected an approval finding from each pack, got {packs}"
        )
        assert len(approvals) >= 2

    def test_the_two_approval_findings_have_distinct_identities(self, combined_run):
        approvals = [
            opp for opp in combined_run["opportunities"]
            if "APPROVAL" in str(opp.get("detector_id", ""))
        ]
        identities = [opp.get("opportunity_identity") for opp in approvals]
        assert all(identities), "an approval finding carries no opportunity_identity"
        assert len(set(identities)) == len(identities), (
            "the two packs' approval findings collapsed to one identity — "
            "cross-pack merging is a permanent non-goal"
        )

    def test_no_identity_is_shared_across_the_two_packs(self, combined_run):
        by_pack: dict = {}
        for opp in combined_run["opportunities"]:
            by_pack.setdefault(opp.get("packId"), set()).add(
                opp.get("opportunity_identity")
            )
        assert by_pack[FSC_PACK].isdisjoint(by_pack[LENDING_PACK])

    def test_fsc_findings_carry_fsc_terminology_from_the_label_file(self, combined_run):
        """AC3 inside a combined run: FSC findings must speak FSC, and must not be
        relabelled with lending words."""
        fsc_opps = [
            o for o in combined_run["opportunities"] if o.get("packId") == FSC_PACK
        ]
        titles = " ".join(str(o.get("title") or "") for o in fsc_opps).lower()
        assert titles.strip(), "FSC findings have no titles"
        assert "borrower" not in titles and "covenant" not in titles
        assert any(
            term in titles
            for term in ("household", "referral", "service process", "financial")
        ), titles[:200]

    def test_a_single_pack_run_is_unaffected(self):
        """Multi-pack regression: running FSC alone must be unchanged."""
        os.environ["INGEST_MODE"] = "offline"
        from discovery.runner import run

        solo = run(mode="offline", pack="financial_services_cloud")
        assert solo["packIds"] == [FSC_PACK]
        assert solo["opportunities"]
        assert {o.get("packId") for o in solo["opportunities"]} == {FSC_PACK}

    def test_lending_alone_is_unaffected_by_fsc_existing(self):
        os.environ["INGEST_MODE"] = "offline"
        from discovery.runner import run

        solo = run(mode="offline", pack="ncino")
        assert solo["packIds"] == [LENDING_PACK]
        for opp in solo["opportunities"]:
            assert (opp.get("score_debug") or {}).get("scorer") == "lending"
