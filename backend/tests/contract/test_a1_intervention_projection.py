"""Contract tests for 2.0-A1 T1 — Intervention Projection Model.

The two backend surfaces the story names:

    GET /api/runs/{run_id}/opportunities
    GET /api/runs/{run_id}/opportunities/{opp_id}/enrichment
    GET /api/runs/{run_id}/opportunities/{opp_id}/blueprint

Coverage:
  * the stored projection reaches the named API surfaces, and is served AS STORED
    (never recomputed at read time, so what an analyst reads is what 2.0-A2 will
    compare a measured outcome against);
  * every served projection carries direction, magnitude band, observation
    horizon, the replaced manual step, the movement signal, and the assumption
    ledger (AC1);
  * the projection is STORED with the opportunity, so it survives without the
    LLM-enrichment artifact (AC6);
  * a projection is never a point estimate and never carries savings/guarantee
    language on the wire (AC3 groundwork — T3 owns the full vocabulary guard);
  * the Track A adapter carries the detector's measured numbers onto the stored
    opportunity, which is what makes a stored projection auditable;
  * the pipeline hook re-persists "opps" so the projection is durable.
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


DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")

#: AC1 — a projection missing any of these fails the contract.
REQUIRED_PROJECTION_KEYS = {
    "schemaVersion",
    "direction",
    "magnitudeBand",
    "observationHorizonDays",
    "manualStepReplaced",
    "movementSignal",
    "assumptionLedger",
    "affectedSignals",
    "basis",
    "bandWidthInputs",
    "confidenceCapped",
}

#: AC3 groundwork — no projection surface may carry these.
FORBIDDEN_PHRASES = (
    "will save",
    "will reduce",
    "will cut",
    "guarantee",
    "guaranteed",
    "savings",
    "roi",
    "eliminates",
    "ensures",
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth(org_id: str = "default") -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, role: str = "owner") -> None:
    """Give the dev token a role in this test's org so RBAC admits the request.

    Uses ``closing()`` rather than ``with db.connect()``: the pooled connection
    proxy's ``__exit__`` delegates to psycopg2's (which commits but does NOT
    close), so a bare ``with`` leaks the connection instead of returning it to
    the pool. ``closing()`` calls ``.close()``, which is what recycles it.
    """
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


def _stored_opp(opp_id: str = "opp_001") -> Dict[str, Any]:
    """An opportunity shaped exactly as the Track A adapter stores it."""
    return {
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
        "focus_emphasis": None,
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


def _seed_run_with_projection(org_id: str, run_id: str) -> List[Dict[str, Any]]:
    """Seed a complete run whose opps carry a STORED projection.

    Uses the same pipeline hook production uses, so the test exercises the real
    write path rather than a hand-written projection.
    """
    from app.materialize_t2 import _apply_intervention_projection

    db.run_set(
        run_id,
        {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id},
    )
    opps = [_stored_opp()]
    db.run_kv_set("opps", run_id, opps)
    db.run_kv_set("evidence", run_id, [])
    projected = _apply_intervention_projection(run_id, opps)
    assert projected == 1, "pipeline hook did not project the seeded opportunity"
    return opps


def _ids() -> tuple[str, str]:
    """Fresh (org_id, run_id), with the dev token already an owner of the org."""
    org_id = f"org-a1-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id)
    return org_id, f"run-a1-{uuid4().hex[:6]}"


def _assert_shape(projection: Dict[str, Any]) -> None:
    missing = REQUIRED_PROJECTION_KEYS - set(projection)
    assert not missing, f"projection missing required parts: {sorted(missing)}"

    band = projection["magnitudeBand"]
    assert band is not None, "a projecting finding must carry a band"
    assert band["lowPct"] < band["highPct"], "band must never be a point estimate"
    assert projection["observationHorizonDays"] in (30, 60, 90)
    assert projection["manualStepReplaced"]

    movement = projection["movementSignal"]
    for key in ("concept", "conceptLabel", "signalName", "unit"):
        assert key in movement, f"movementSignal missing {key}"

    assumptions = projection["assumptionLedger"]
    assert isinstance(assumptions, list) and assumptions, (
        "projection must carry an explicit assumption ledger"
    )
    for assumption in assumptions:
        for key in ("id", "label", "description"):
            assert assumption.get(key), f"assumption missing {key}"


# ---------------------------------------------------------------------------
# The pipeline hook — stores the projection with the opportunity (AC6).
# ---------------------------------------------------------------------------


class TestProjectionIsStoredWithTheOpportunity:
    def test_hook_persists_projection_into_run_kv_opps(self):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        reread = db.run_kv_get("opps", run, [])
        assert reread and "projection" in reread[0], (
            "AC6: the projection must be STORED with the opportunity — 2.0-A2 has "
            "nothing to validate against otherwise"
        )
        _assert_shape(reread[0]["projection"])

    def test_hook_is_non_blocking_on_bad_input(self):
        """A projection failure must never fail a run or lose an opportunity."""
        from app.materialize_t2 import _apply_intervention_projection

        org, run = _ids()
        db.run_set(run, {"id": run, "runId": run, "status": "complete", "org_id": org})
        opps: List[Any] = [{"id": "opp_bad", "_debug": None}, None]
        db.run_kv_set("opps", run, [{"id": "opp_bad"}])

        assert _apply_intervention_projection(run, opps) == 0
        assert len(opps) == 2, "an opportunity must never be dropped"

    def test_stored_projection_is_reproducible(self):
        """AC5: recomputing from the stored record reproduces the same result."""
        from discovery.projection import build_projection

        org, run = _ids()
        opps = _seed_run_with_projection(org, run)
        stored = db.run_kv_get("opps", run, [])[0]

        recomputed = build_projection(stored)
        assert recomputed == stored["projection"]
        assert opps[0]["projection"] == stored["projection"]


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/opportunities
# ---------------------------------------------------------------------------


class TestOpportunitiesListSurface:
    def test_projection_is_served_on_the_list_endpoint(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body, "no opportunities served"

        projection = body[0].get("projection")
        assert projection is not None, (
            "the stored projection must reach GET /api/runs/{run_id}/opportunities"
        )
        _assert_shape(projection)

    def test_served_projection_equals_the_stored_projection(self, client):
        """Served as stored — never recomputed at read time."""
        org, run = _ids()
        _seed_run_with_projection(org, run)
        stored = db.run_kv_get("opps", run, [])[0]["projection"]

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        assert response.json()[0]["projection"] == stored

    def test_list_projection_names_a_real_measured_signal(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        opp = response.json()[0]
        measured = opp["_debug"]["raw_evidence"]

        movement = opp["projection"]["movementSignal"]
        assert movement["signalName"] in measured, (
            "the movement signal must be a field the detector actually measures"
        )
        assert movement["currentValue"] == measured[movement["signalName"]]

    def test_opportunity_without_projection_still_serves(self, client):
        """A legacy or unprojectable opportunity must not break the endpoint."""
        org, run = _ids()
        db.run_set(run, {"id": run, "runId": run, "status": "complete", "org_id": org})
        legacy = _stored_opp()
        legacy["_debug"]["detector_id"] = "NOT_A_REAL_DETECTOR"
        db.run_kv_set("opps", run, [legacy])

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        assert response.status_code == 200
        assert response.json()[0].get("projection") is None


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/opportunities/{opp_id}/enrichment
# ---------------------------------------------------------------------------


class TestEnrichmentSurface:
    def test_oppenrichment_projection_defaults_to_none(self):
        """Absence is semantically distinct from an empty projection."""
        from app.routes_sprint4_t6 import OppEnrichment

        assert OppEnrichment(oppId="opp-1").projection is None

    def test_oppenrichment_serialises_projection(self):
        from app.routes_sprint4_t6 import OppEnrichment

        dumped = OppEnrichment(
            oppId="opp-1", projection={"direction": "improves"}
        ).model_dump()
        assert dumped["projection"] == {"direction": "improves"}

    def test_projection_served_without_an_llm_enrichment_artifact(self, client):
        """AC6: the projection is stored on the opp, so it survives the fallback."""
        org, run = _ids()
        _seed_run_with_projection(org, run)

        response = client.get(
            f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
        )
        assert response.status_code == 200, response.text
        projection = response.json().get("projection")
        assert projection is not None, (
            "the projection must not depend on the LLM enrichment artifact"
        )
        _assert_shape(projection)

    def test_projection_served_with_an_llm_enrichment_artifact(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        from app.llm_enrichment import KV_LLM_ENRICHMENT

        db.run_kv_set(
            KV_LLM_ENRICHMENT,
            run,
            {
                "runId": run,
                "perOpportunity": {
                    "opp_001": {
                        "aiSummary": "Reassignment is elevated.",
                        "aiWhyBullets": [],
                        "aiRisks": [],
                        "aiSuggestedNextSteps": [],
                        "llmGenerated": True,
                    }
                },
            },
        )

        response = client.get(
            f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
        )
        assert response.status_code == 200, response.text
        _assert_shape(response.json()["projection"])

    def test_enrichment_projection_equals_stored_projection(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)
        stored = db.run_kv_get("opps", run, [])[0]["projection"]

        response = client.get(
            f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
        )
        assert response.json()["projection"] == stored

    def test_enrichment_projection_is_none_for_unprojectable_opportunity(self, client):
        org, run = _ids()
        db.run_set(run, {"id": run, "runId": run, "status": "complete", "org_id": org})
        legacy = _stored_opp()
        legacy["_debug"]["detector_id"] = "NOT_A_REAL_DETECTOR"
        db.run_kv_set("opps", run, [legacy])

        response = client.get(
            f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
        )
        assert response.status_code == 200
        assert response.json()["projection"] is None


# ---------------------------------------------------------------------------
# AC3 groundwork — no point estimates, no guarantee language on the wire.
# ---------------------------------------------------------------------------


class TestProjectionVocabularyOnTheWire:
    def _texts(self, projection: Dict[str, Any]) -> List[str]:
        texts = [projection["manualStepReplaced"], projection["magnitudeBand"]["label"]]
        texts += [s["conceptLabel"] for s in projection["affectedSignals"]]
        for assumption in projection["assumptionLedger"]:
            texts.append(assumption["label"])
            texts.append(assumption["description"])
        return texts

    def test_no_savings_or_guarantee_language_on_projection_surfaces(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        responses = [
            client.get(f"/api/runs/{run}/opportunities", headers=_auth(org)).json()[0][
                "projection"
            ],
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/enrichment",
                headers=_auth(org),
            ).json()["projection"],
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/blueprint",
                headers=_auth(org),
            ).json()["projection"],
        ]
        for projection in responses:
            for text in self._texts(projection):
                lowered = text.lower()
                for phrase in FORBIDDEN_PHRASES:
                    assert phrase not in lowered, (
                        f"projection text {text!r} contains forbidden phrase "
                        f"{phrase!r} — a projection is not a savings claim"
                    )

    def test_band_is_a_range_on_the_wire(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        band = response.json()[0]["projection"]["magnitudeBand"]
        assert band["lowPct"] < band["highPct"]
        assert "–" in band["label"], "the band label must render as a range"

    def test_capped_finding_is_labelled_on_the_wire(self, client):
        """AC4: a single-source finding's projection says so."""
        from app.materialize_t2 import _apply_intervention_projection

        org, run = _ids()
        db.run_set(run, {"id": run, "runId": run, "status": "complete", "org_id": org})
        capped = _stored_opp()
        capped["corroboration_sources"] = []
        capped["corroboration_rule_ids"] = ["COR-08"]
        capped["corroboration_label"] = None
        opps = [capped]
        db.run_kv_set("opps", run, opps)
        _apply_intervention_projection(run, opps)

        response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
        projection = response.json()[0]["projection"]
        assert projection["confidenceCapped"] is True
        assert projection["basis"]["corroborationStatus"] == "single_source"

    def test_capped_band_is_not_narrower_than_corroborated(self, client):
        """AC4: a capped finding never looks stronger on projection alone."""
        from app.materialize_t2 import _apply_intervention_projection

        widths = {}
        for label, mutate in (
            ("corroborated", lambda o: o),
            (
                "capped",
                lambda o: o.update(
                    {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]}
                )
                or o,
            ),
        ):
            org, run = _ids()
            db.run_set(
                run, {"id": run, "runId": run, "status": "complete", "org_id": org}
            )
            opps = [mutate(_stored_opp())]
            db.run_kv_set("opps", run, opps)
            _apply_intervention_projection(run, opps)
            band = client.get(
                f"/api/runs/{run}/opportunities", headers=_auth(org)
            ).json()[0]["projection"]["magnitudeBand"]
            widths[label] = band["highPct"] - band["lowPct"]

        assert widths["capped"] >= widths["corroborated"]

    def test_assumption_ledger_is_visible_on_projection_api_surfaces(self, client):
        org, run = _ids()
        _seed_run_with_projection(org, run)

        responses = [
            client.get(f"/api/runs/{run}/opportunities", headers=_auth(org)).json()[0][
                "projection"
            ],
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/enrichment",
                headers=_auth(org),
            ).json()["projection"],
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/blueprint",
                headers=_auth(org),
            ).json()["projection"],
        ]
        for projection in responses:
            labels = [a["label"] for a in projection["assumptionLedger"]]
            assert labels == [
                "Agent handles the identified recurring cases",
                "Adoption is complete for those cases",
                "Upstream volume remains within its observed range",
                "Residual cases still require human judgement",
                "Projection applies only to the measured signal and horizon shown",
            ]


# ---------------------------------------------------------------------------
# The adapter carries the measured numbers that make a projection auditable.
# ---------------------------------------------------------------------------


class TestAdapterCarriesMeasuredSignals:
    def test_adapter_stores_numeric_raw_evidence_under_debug(self):
        from discovery.track_a_adapter import to_track_a_opportunities

        payload = {
            "opportunities": [
                {
                    "detector_id": "HANDOFF_FRICTION",
                    "signal_source": "salesforce",
                    "metric_value": 2.4,
                    "threshold": 1.5,
                    "impact": 8,
                    "effort": 3,
                    "confidence": "HIGH",
                    "tier": "Quick Win",
                    "roadmap_stage": "NEXT_30",
                    "evidenceIds": ["ev_sf_aaa111"],
                    "raw_evidence": {
                        "owner_changes_90d": 240,
                        "total_cases_90d": 800,
                        "handoff_score": 2.4,
                        "degraded_signal": False,
                        "top_categories": [{"category": "Billing", "handoff_score": 3.1}],
                    },
                }
            ]
        }
        stored = to_track_a_opportunities(payload)[0]
        raw = stored["_debug"]["raw_evidence"]

        assert raw["owner_changes_90d"] == 240
        assert raw["total_cases_90d"] == 800
        assert "degraded_signal" not in raw, "booleans are gates, not measurements"
        assert "top_categories" not in raw, "instance lists are evidence, not signal"

    def test_adapter_output_projects(self):
        """The adapter's stored shape is sufficient input for a projection."""
        from discovery.projection import build_projection
        from discovery.track_a_adapter import to_track_a_opportunities

        payload = {
            "opportunities": [
                {
                    "detector_id": "GITHUB_PR_REVIEW_BOTTLENECK",
                    "signal_source": "github",
                    "metric_value": 12.0,
                    "threshold": 5.0,
                    "impact": 7,
                    "effort": 4,
                    "confidence": "MEDIUM",
                    "tier": "Strategic",
                    "roadmap_stage": "NEXT_60",
                    "corroboration_sources": ["Jira"],
                    "corroboration_rule_ids": ["COR-02"],
                    "raw_evidence": {
                        "prs_over_threshold": 12,
                        "avg_days_open": 9.5,
                        "max_days_open": 31.0,
                        "open_pr_count": 44,
                    },
                }
            ]
        }
        stored = to_track_a_opportunities(payload)[0]
        projection = build_projection(stored)

        assert projection is not None
        _assert_shape(projection)
        assert projection["movementSignal"]["signalName"] == "avg_days_open"
        assert projection["movementSignal"]["currentValue"] == 9.5
        assert projection["basis"]["observedPopulation"] == 44
