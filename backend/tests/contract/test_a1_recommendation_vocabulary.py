"""Contract tests for 2.0-A1 T5 — intervention-language recommendation copy.

AC3 in full: *"No projection output — API, UI, report, or export — contains a
point-estimate savings claim or guarantee language; template-level check over the
projection vocabulary."*

The unit tests in ``discovery/tests/test_projection_vocabulary.py`` pin the guard
and the generator. These pin the promise ON THE WIRE — the surfaces AC3 names,
served through the real pipeline hook:

  * ``GET /api/runs/{id}/opportunities``            (API)
  * ``.../opportunities/{opp}/enrichment``          (Opportunity Review)
  * ``.../opportunities/{opp}/blueprint``           (Agentforce Blueprint)
  * ``.../executive-report``                        (Executive Report + PDF source)

Every one is swept WHOLE — every string at every depth — rather than spot-checked
on the fields we happened to think of. The point of a template-level check is
that it catches the field nobody remembered.
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from discovery.projection.recommendation import REQUIRED_PARTS
from discovery.projection.vocabulary import scan_payload, scan_text

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth(org_id: str = "default") -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, DEV_TOKEN, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _ids() -> tuple[str, str]:
    org_id = f"org-a1t5-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id)
    return org_id, f"run-a1t5-{uuid4().hex[:6]}"


def _seeded_opp(opp_id: str = "opp_001", **overrides: Any) -> Dict[str, Any]:
    opp = {
        "id": opp_id,
        "title": "Elevated case owner reassignment",
        "category": "Automation Opportunity",
        "tier": "Quick Win",
        "decision": "UNREVIEWED",
        "impact": 8,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "Owner changes are running above the handoff threshold.",
        "evidenceIds": ["ev_sf_aaa111"],
        "requiredPermissions": [],
        "override": {
            "isLocked": False,
            "rationaleOverride": "",
            "overrideReason": "",
            "updatedAt": None,
        },
        "corroboration_sources": ["ServiceNow", "Jira"],
        "corroboration_label": "Corroborated by ServiceNow incidents",
        "triple_corroboration": False,
        "corroboration_rule_ids": ["COR-01", "COR-02"],
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "recent_values": [200.0, 205.0, 198.0, 202.0, 203.0],
        "baseline_mean": 201.6,
        "baseline_stddev": 2.7,
        "baseline_window_days": 90,
        "run_count": 5,
        "signal_key": "service_cloud::HANDOFF_FRICTION::metric_value",
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "signal_source": "salesforce",
            "metric_value": 2.4,
            "threshold": 1.5,
            "roadmap_stage": "NEXT_30",
            "score_debug": {},
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
                "handoff_score": 2.4,
            },
        },
    }
    opp.update(overrides)
    return opp


def _seed_run(org_id: str, run_id: str, opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from app.materialize_t2 import _apply_intervention_projection

    db.run_set(
        run_id,
        {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id},
    )
    db.run_kv_set("opps", run_id, opps)
    db.run_kv_set("evidence", run_id, [])
    assert _apply_intervention_projection(run_id, opps) == len(opps)
    return opps


def _surfaces(client: TestClient, org: str, run: str) -> Dict[str, Any]:
    """Every AC3 surface, as served."""
    return {
        "opportunities": client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json(),
        "enrichment": client.get(
            f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
        ).json(),
        "blueprint": client.get(
            f"/api/runs/{run}/opportunities/opp_001/blueprint", headers=_auth(org)
        ).json(),
        "executive_report": client.get(
            f"/api/runs/{run}/executive-report", headers=_auth(org)
        ).json(),
    }


# ---------------------------------------------------------------------------
# AC3 — the template-level sweep, over every named surface.
# ---------------------------------------------------------------------------


class TestNoProhibitedVocabularyOnAnySurface:
    def test_every_projection_surface_is_clean(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        for name, payload in _surfaces(client, org, run).items():
            violations = scan_payload(payload)
            assert not violations, (
                f"{name} surface carries prohibited projection vocabulary: "
                + "; ".join(str(v) for v in violations)
            )

    @pytest.mark.parametrize(
        "variant",
        [
            {},
            {"confidence": "LOW"},
            {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]},
            {"tier": "Strategic"},
        ],
    )
    def test_surfaces_stay_clean_across_evidence_variants(self, client, variant):
        """A capped or thin finding must not tempt any surface into over-claiming."""
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp(**variant)])

        for name, payload in _surfaces(client, org, run).items():
            violations = scan_payload(payload)
            assert not violations, f"{name}: " + "; ".join(str(v) for v in violations)

    def test_a_seeded_savings_claim_is_scrubbed_before_it_is_served(self, client):
        """The guard is a control, not a hope.

        The stored opportunity here carries exactly the sentence the story
        forbids. It must not reach the executive report.
        """
        org, run = _ids()
        poisoned = _seeded_opp(
            aiRationale="This agent will reduce cost by 40% and guarantees savings.",
            title="Elevated case owner reassignment",
        )
        _seed_run(org, run, [poisoned])

        report = client.get(
            f"/api/runs/{run}/executive-report", headers=_auth(org)
        ).json()
        violations = scan_payload(report)
        assert not violations, (
            "a seeded savings claim reached the executive report: "
            + "; ".join(str(v) for v in violations)
        )

    def test_a_seeded_savings_claim_is_scrubbed_on_the_opportunities_api(self, client):
        """The same guarantee on the API the Opportunity Review renders.

        The live route composes its response from stored opps directly, so the
        guard has to sit on the serve path — not only inside the report engine.
        """
        org, run = _ids()
        _seed_run(
            org,
            run,
            [
                _seeded_opp(
                    aiRationale="This agent will reduce cost by 40% and guarantees savings."
                )
            ],
        )

        served = client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json()
        violations = scan_payload(served)
        assert not violations, (
            "a seeded savings claim reached the opportunities API: "
            + "; ".join(str(v) for v in violations)
        )
        assert "40%" not in served[0]["aiRationale"]

    def test_scrubbing_never_rewrites_the_stored_opportunity(self):
        """A replay must serve what the run produced, not a doctored version."""
        org, run = _ids()
        claim = "This agent will reduce cost by 40% and guarantees savings."
        _seed_run(org, run, [_seeded_opp(aiRationale=claim)])

        stored = db.run_kv_get("opps", run, [])[0]
        assert stored["aiRationale"] == claim, (
            "the guard is a serve-time overlay — it must not edit stored history"
        )

    def test_blueprint_agent_purpose_is_scrubbed(self, client):
        org, run = _ids()
        _seed_run(
            org,
            run,
            [_seeded_opp(aiRationale="This agent will reduce cost by 40%.")],
        )

        blueprint = client.get(
            f"/api/runs/{run}/opportunities/opp_001/blueprint", headers=_auth(org)
        ).json()
        assert not scan_text(blueprint["agentTopic"])
        assert "40%" not in blueprint["agentTopic"]


# ---------------------------------------------------------------------------
# The recommendation reaches the surfaces, and says the five things.
# ---------------------------------------------------------------------------


class TestRecommendationOnTheWire:
    def test_recommendation_reaches_every_projection_surface(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        surfaces = _surfaces(client, org, run)

        projections = [
            surfaces["opportunities"][0]["projection"],
            surfaces["enrichment"]["projection"],
            surfaces["blueprint"]["projection"],
            surfaces["executive_report"]["topQuickWins"][0]["projection"],
        ]
        for projection in projections:
            recommendation = projection.get("recommendation")
            assert recommendation, "surface carries no recommendation"
            assert [p["id"] for p in recommendation["parts"]] == list(REQUIRED_PARTS)
            assert recommendation["headline"].startswith("Agent handles ")
            assert recommendation["nextSteps"]

    def test_recommendation_names_the_n_recurring_cases(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        recommendation = client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json()[0]["projection"]["recommendation"]

        assert "240" in recommendation["headline"]
        scope = next(p for p in recommendation["parts"] if p["id"] == "cases_in_scope")
        assert "240" in scope["text"]

    def test_recommendation_names_a_real_measured_signal(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        opp = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org)).json()[0]

        measured = opp["_debug"]["raw_evidence"]
        signal_part = next(
            p
            for p in opp["projection"]["recommendation"]["parts"]
            if p["id"] == "signal_expected_to_move"
        )
        assert any(name in signal_part["text"] for name in measured), (
            "the recommendation must name a field the detector actually measures"
        )

    def test_recommendation_states_the_band_and_horizon(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        projection = client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json()[0]["projection"]

        band_part = next(
            p
            for p in projection["recommendation"]["parts"]
            if p["id"] == "band_and_horizon"
        )
        band = projection["magnitudeBand"]
        assert f"{band['lowPct']}" in band_part["text"]
        assert f"{band['highPct']}" in band_part["text"]
        assert str(projection["observationHorizonDays"]) in band_part["text"]

    def test_recommendation_is_stored_with_the_opportunity(self):
        """AC6 — it travels with the projection, so a replay serves it unchanged."""
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        stored = db.run_kv_get("opps", run, [])[0]
        assert stored["projection"]["recommendation"]["headline"]

    def test_served_recommendation_equals_the_stored_one(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = db.run_kv_get("opps", run, [])[0]["projection"]["recommendation"]

        served = client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json()[0]["projection"]["recommendation"]
        assert served == stored

    def test_recommendation_is_reproducible(self):
        """AC5 — unchanged signal reproduces identical copy."""
        from discovery.projection import build_projection

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = db.run_kv_get("opps", run, [])[0]

        assert (
            build_projection(stored)["recommendation"]
            == stored["projection"]["recommendation"]
        )
