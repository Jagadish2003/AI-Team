"""Contract tests for 2.0-A1 T6 — the projection is STORED with the opportunity.

AC6: *"The projection is stored with the opportunity so 2.0-A2 can later compare
it against measured outcome."*

The story's own note on this criterion: *"AC6 is small and load-bearing: without
a stored projection there is nothing to validate against, and the flywheel never
starts. Store the projection even if the UI does not display all of it."*

These tests pin exactly that, on the surfaces the task names:

  * ``/api/runs/{run_id}/opportunities``
  * ``/api/runs/{run_id}/roadmap``          ← was serving projection-less opps
  * ``/api/runs/{run_id}/executive-report``
  * the blueprint and enrichment surfaces the UI reads
  * the ``opportunity_instances`` row 2.0-A2 queries ACROSS runs

plus the two properties that make a stored projection actually usable: it is
fully identified (run, opportunity, stable identity, timestamp), and its core
still reproduces exactly (AC5) despite carrying that identification.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from discovery.projection.provenance import (
    REQUIRED_PROVENANCE_FIELDS,
    get_provenance,
    projection_core,
)

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")

#: Every part the task requires a STORED projection to include.
REQUIRED_STORED_PARTS = (
    "direction",
    "magnitudeBand",
    "observationHorizonDays",
    "assumptionLedger",
    "basis",
)

STABLE_IDENTITY = "opp_stable_t6_abc123"


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
    org_id = f"org-a1t6-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id)
    return org_id, f"run-a1t6-{uuid4().hex[:6]}"


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
        "opportunity_identity": STABLE_IDENTITY,
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


def _seed_run(
    org_id: str, run_id: str, opps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Seed a run and project through the REAL pipeline hook."""
    from app.materialize_t2 import _apply_intervention_projection

    db.run_set(
        run_id,
        {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id},
    )
    db.run_kv_set("opps", run_id, opps)
    db.run_kv_set("evidence", run_id, [])
    assert _apply_intervention_projection(run_id, opps, org_id=org_id) == len(opps)
    return opps


def _seed_run_with_roadmap(
    org_id: str, run_id: str, opps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Seed a run the way materialization does: roadmap BEFORE projections.

    This ordering is the bug T6 fixes — the roadmap artifact is built early,
    before a projection can exist, so it must be re-stored afterwards.
    """
    from app.materialize_t2 import (
        _apply_intervention_projection,
        _rebuild_roadmap_with_projections,
    )
    from app.roadmap_engine import build_roadmap

    db.run_set(
        run_id,
        {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id},
    )
    db.run_kv_set("opps", run_id, opps)
    db.run_kv_set("evidence", run_id, [])
    # Roadmap built and stored BEFORE projections exist — as materialization does.
    db.run_kv_set("roadmap", run_id, build_roadmap(opps))

    _apply_intervention_projection(run_id, opps, org_id=org_id)
    _rebuild_roadmap_with_projections(run_id, opps)
    return opps


def _stored_projection(run_id: str, opp_id: str = "opp_001") -> Dict[str, Any]:
    for opp in db.run_kv_get("opps", run_id, []) or []:
        if opp.get("id") == opp_id:
            return opp["projection"]
    raise AssertionError(f"no stored projection for {opp_id}")


# ---------------------------------------------------------------------------
# AC6 — the stored record is complete.
# ---------------------------------------------------------------------------


class TestStoredProjectionIsComplete:
    def test_every_required_part_is_stored(self):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        projection = _stored_projection(run)

        for part in REQUIRED_STORED_PARTS:
            assert projection.get(part) is not None, f"stored projection missing {part}"

    def test_the_stored_projection_is_fully_identified(self):
        """Run reference, opportunity id, stable identity, timestamp."""
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        provenance = get_provenance(_stored_projection(run))

        missing = [f for f in REQUIRED_PROVENANCE_FIELDS if not provenance.get(f)]
        assert not missing, f"stored projection is not identifiable: missing {missing}"
        assert provenance["runId"] == run
        assert provenance["oppId"] == "opp_001"
        assert provenance["orgId"] == org
        assert provenance["opportunityIdentity"] == STABLE_IDENTITY
        assert provenance["crossRunComparable"] is True

    def test_the_evidence_and_corroboration_label_is_stored(self):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        basis = _stored_projection(run)["basis"]

        assert basis["corroborationLabel"] == "Corroborated by ServiceNow incidents"
        assert basis["corroborationStatus"]
        assert basis["corroborationSources"] == ["ServiceNow", "Jira"]
        assert basis["evidenceLabel"]

    def test_the_created_timestamp_is_a_real_iso_timestamp(self):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        created_at = get_provenance(_stored_projection(run))["createdAt"]

        parsed = datetime.fromisoformat(created_at)
        assert parsed.tzinfo is not None, "a stored timestamp must be unambiguous"

    def test_storage_does_not_depend_on_the_ui_showing_every_field(self):
        """The story's own note: store it even if the UI does not display it all.

        bandWidth drivers, projection strength, and the recommendation parts are
        all stored whether or not a given screen renders them.
        """
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        projection = _stored_projection(run)

        assert projection["bandWidth"]["drivers"]
        assert projection["projectionStrength"]["value"] is not None
        assert projection["recommendation"]["parts"]
        assert projection["basis"]["packVersion"] == "1.2.0"


# ---------------------------------------------------------------------------
# AC5 survives AC6 — identification does not break reproducibility.
# ---------------------------------------------------------------------------


class TestStorageDoesNotBreakReproducibility:
    def test_recomputed_core_matches_the_stored_core(self):
        from discovery.projection import build_projection

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored_opp = db.run_kv_get("opps", run, [])[0]

        recomputed = build_projection(stored_opp)
        assert projection_core(recomputed) == projection_core(stored_opp["projection"])

    def test_two_runs_of_the_same_finding_share_a_core_and_differ_in_provenance(self):
        org_a, run_a = _ids()
        org_b, run_b = _ids()
        _seed_run(org_a, run_a, [_seeded_opp()])
        _seed_run(org_b, run_b, [_seeded_opp()])

        first = _stored_projection(run_a)
        second = _stored_projection(run_b)

        assert projection_core(first) == projection_core(second)
        assert get_provenance(first)["runId"] != get_provenance(second)["runId"]

    def test_the_store_helper_compares_cores_not_payloads(self):
        """The rule 2.0-A2 must not rediscover the hard way."""
        from app.projection_store import projection_matches_stored
        from discovery.projection import build_projection

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored_opp = db.run_kv_get("opps", run, [])[0]

        recomputed = build_projection(stored_opp)
        assert projection_matches_stored(stored_opp["projection"], recomputed)
        assert stored_opp["projection"] != recomputed, (
            "the payloads legitimately differ — only the cores must match"
        )


# ---------------------------------------------------------------------------
# The named API surfaces.
# ---------------------------------------------------------------------------


class TestProjectionReachesEverySurface:
    def test_opportunities_api_serves_the_stored_projection(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = _stored_projection(run)

        served = client.get(
            f"/api/runs/{run}/opportunities", headers=_auth(org)
        ).json()[0]["projection"]
        assert served == stored

    def test_roadmap_api_serves_opportunities_carrying_their_projection(self, client):
        """The T6 fix.

        The roadmap artifact is built EARLY in materialization — before temporal
        enrichment, and therefore before any projection can exist. Without the
        rebuild it stored projection-less opportunities, and this endpoint served
        them that way: the Agent Roadmap screen showed no projection at all, and
        T4's capped-confidence ordering had nothing to order on.
        """
        org, run = _ids()
        _seed_run_with_roadmap(org, run, [_seeded_opp()])

        roadmap = client.get(f"/api/runs/{run}/roadmap", headers=_auth(org)).json()
        stage = next(s for s in roadmap["stages"] if s["id"] == "NEXT_30")
        assert stage["opportunities"], "the seeded quick win is missing from the stage"

        projection = stage["opportunities"][0].get("projection")
        assert projection, "the roadmap must carry the stored projection"
        assert get_provenance(projection)["runId"] == run
        for part in REQUIRED_STORED_PARTS:
            assert projection.get(part) is not None

    def test_stored_roadmap_artifact_itself_carries_the_projection(self):
        """Not just the served response — the persisted artifact."""
        org, run = _ids()
        _seed_run_with_roadmap(org, run, [_seeded_opp()])

        roadmap = db.run_kv_get("roadmap", run, None)
        assert roadmap is not None
        stage = next(s for s in roadmap["stages"] if s["id"] == "NEXT_30")
        assert stage["opportunities"][0].get("projection")

    def test_executive_report_serves_the_stored_projection(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        report = client.get(
            f"/api/runs/{run}/executive-report", headers=_auth(org)
        ).json()
        projection = report["topQuickWins"][0]["projection"]
        assert get_provenance(projection)["runId"] == run
        for part in REQUIRED_STORED_PARTS:
            assert projection.get(part) is not None

    def test_enrichment_and_blueprint_serve_the_stored_projection(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = _stored_projection(run)

        for path in (
            f"/api/runs/{run}/opportunities/opp_001/enrichment",
            f"/api/runs/{run}/opportunities/opp_001/blueprint",
        ):
            served = client.get(path, headers=_auth(org)).json()["projection"]
            assert served == stored, f"{path} did not serve the stored projection"


# ---------------------------------------------------------------------------
# The 2.0-A2 read surface.
# ---------------------------------------------------------------------------


class TestOutcomeTrackingReadSurface:
    def test_get_stored_projection_returns_the_serving_copy(self):
        from app.projection_store import get_stored_projection

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        assert get_stored_projection(run, "opp_001") == _stored_projection(run)
        assert get_stored_projection(run, "nope") is None

    def test_get_projections_for_run_returns_every_projection(self):
        from app.projection_store import get_projections_for_run

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp("opp_001"), _seeded_opp("opp_002")])

        projections = get_projections_for_run(run)
        assert set(projections) == {"opp_001", "opp_002"}

    def test_projection_is_recorded_on_the_opportunity_instance_row(self):
        """The cross-run tracking copy 2.0-A2 queries by identity.

        Run KV cannot answer "every projection ever made about this problem" —
        it is scoped to one run. The instance row can.
        """
        from app.opportunity_instances import (
            ensure_opportunity_instances_table,
            record_opportunity_instances,
        )
        from app.projection_store import record_projections_on_instances

        org, run = _ids()
        opps = _seed_run(org, run, [_seeded_opp()])

        ensure_opportunity_instances_table()
        record_opportunity_instances(run, opps, org_id=org)
        assert record_projections_on_instances(opps, run) == 1

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT metadata FROM opportunity_instances "
                    "WHERE opportunity_identity = %s AND run_id = %s",
                    (STABLE_IDENTITY, run),
                )
                row = cur.fetchone()

        assert row is not None, "no opportunity_instance row was written"
        metadata = json.loads(row[0])
        assert metadata["projection"]["direction"]
        assert get_provenance(metadata["projection"])["runId"] == run

    def test_projection_history_walks_the_series_across_runs(self):
        """What 2.0-A2 is built on: the same problem, projected over time."""
        from app.opportunity_instances import (
            ensure_opportunity_instances_table,
            record_opportunity_instances,
        )
        from app.projection_store import (
            get_projection_history,
            record_projections_on_instances,
        )

        org, _ = _ids()
        ensure_opportunity_instances_table()

        runs = []
        for _ in range(2):
            run = f"run-a1t6-{uuid4().hex[:6]}"
            opps = _seed_run(org, run, [_seeded_opp()])
            record_opportunity_instances(run, opps, org_id=org)
            record_projections_on_instances(opps, run)
            runs.append(run)

        history = get_projection_history(STABLE_IDENTITY, org_id=org)
        assert len(history) == len(runs), (
            "each run must contribute one projection to the identity's series"
        )
        for entry in history:
            assert entry["runId"] in runs
            assert entry["createdAt"]
            assert entry["projection"]["direction"]

    def test_history_is_empty_for_an_unknown_identity(self):
        from app.projection_store import get_projection_history

        assert get_projection_history("opp_does_not_exist") == []

    def test_an_opportunity_without_a_stable_identity_still_stores_its_projection(self):
        """No cross-run row to attach to, but the run's own copy is intact."""
        from app.projection_store import record_projections_on_instances

        org, run = _ids()
        opps = _seed_run(
            org, run, [_seeded_opp(opportunity_identity=None)]
        )

        assert record_projections_on_instances(opps, run) == 0
        projection = _stored_projection(run)
        assert projection["direction"]
        assert get_provenance(projection)["crossRunComparable"] is False


# ---------------------------------------------------------------------------
# Non-blocking contract.
# ---------------------------------------------------------------------------


class TestStorageIsNonBlocking:
    def test_a_stamping_failure_never_loses_an_opportunity(self):
        from app.materialize_t2 import _apply_intervention_projection

        org, run = _ids()
        db.run_set(
            run, {"id": run, "runId": run, "status": "complete", "org_id": org}
        )
        opps: List[Any] = [{"id": "opp_bad", "_debug": None}, None]
        db.run_kv_set("opps", run, [{"id": "opp_bad"}])

        assert _apply_intervention_projection(run, opps, org_id=org) == 0
        assert len(opps) == 2, "an opportunity must never be dropped"

    def test_roadmap_rebuild_is_non_blocking(self):
        """A rebuild failure leaves the earlier roadmap in place, never raises."""
        from app.materialize_t2 import _rebuild_roadmap_with_projections

        org, run = _ids()
        db.run_set(
            run, {"id": run, "runId": run, "status": "complete", "org_id": org}
        )
        _rebuild_roadmap_with_projections(run, [None, {"id": "x"}])  # must not raise
