"""Contract tests for 2.0-A3 T2 — the bounded adjustment layer, end to end.

The subtask's deliverables, one class each:

* the state is STORED per org, with history, rather than derived at read time;
* the served list is reordered within the cap (AC1);
* base scores are unchanged and retrievable (AC1);
* evidence, confidence and corroboration are never modified (AC3);
* the cold-start gate is honoured through the real stack (AC4);
* one org's learning never reaches another's ranking (AC6).

The cap arithmetic and the placement bound are unit-tested in
``tests/unit/test_learning_adjustment.py`` where they need no database. What is
tested here is what only a live stack can show: that the state round-trips, that
the serve path actually routes through the one adjustment function, and that the
org boundary holds through the HTTP edge.
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
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/learning/adjustment"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    """Ensure the tables, and FAIL LOUDLY here if they are still absent.

    The suite shares one PostgreSQL schema for the whole session, so schema
    state can genuinely be disturbed by an earlier test. Diagnosing that at
    setup beats an ``UndefinedTable`` surfacing from a random write and reading
    as a bug in the code under test.
    """
    from app.learning_adjustment_state import ensure_ranking_adjustment_tables
    from app.learning_feedback import ensure_opportunity_feedback_table

    ensure_opportunity_feedback_table()
    ensure_ranking_adjustment_tables()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.ranking_adjustments'),"
                "       to_regclass('public.ranking_adjustment_history')"
            )
            current, history = cur.fetchone()
    assert current and history, (
        "ranking adjustment tables are missing and could not be created — check "
        "that migration 0037 ran against the test database"
    )


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a3t2-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN)
    _seed_workspace_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _record_feedback(org: str, identity: str, action: str, detector: str = "HANDOFF_FRICTION"):
    from app.learning_feedback import record_feedback

    return record_feedback(
        org, identity, action,
        actor_id="analyst_1", detector_id=detector, pack_id="service_cloud",
    )


def _seed_enough_to_activate(org: str, detector: str = "HANDOFF_FRICTION", n: int = 12):
    """Enough accepts across enough distinct findings to clear cold start."""
    for _ in range(n):
        _record_feedback(org, _identity(), "accept", detector=detector)


def _run_with_opps(org: str, opps: List[Dict[str, Any]]) -> str:
    from app.db import run_kv_set, upsert_run
    from app.run_store import read_run, start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    run = read_run(run_id)
    run["org_id"] = org
    run["orgId"] = org
    upsert_run(run_id, run)
    run_kv_set("opps", run_id, opps)
    return run_id


def _opp(index: int, detector: str = "HANDOFF_FRICTION", impact: int = 7):
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"ident_{index:03d}",
        "title": f"Finding {index}",
        "category": "Workflow",
        "tier": "Quick Win",
        "impact": impact,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "seeded",
        "evidenceIds": [f"ev_{index}"],
        "corroboration_sources": ["servicenow", "jira"],
        "corroboration_label": "Corroborated",
        "decision": "UNREVIEWED",
        "override": {"isLocked": False, "rationaleOverride": "", "overrideReason": "",
                     "updatedAt": None},
        "packId": "service_cloud",
        "_debug": {"detector_id": detector},
    }


# ---------------------------------------------------------------------------
# The state is stored, not derived
# ---------------------------------------------------------------------------


class TestTheStateIsStored:
    def test_recompute_writes_a_row_per_similarity_group(self, client):
        org = _org()
        _seed_enough_to_activate(org)

        body = client.post(f"{BASE}/recompute", headers=_auth(org)).json()
        assert body["groupsWritten"] >= 1
        assert body["learningActive"] is True

        state = client.get(BASE, headers=_auth(org)).json()
        assert state["groups"], "no adjustment state persisted"
        assert state["groups"][0]["detectorId"] == "handoff_friction"

    def test_the_state_is_not_recomputed_by_serving_a_list(self, client):
        """A ranking that shifted because someone opened a page is the drift
        this story exists to prevent."""
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        before = client.get(BASE, headers=_auth(org)).json()["groups"][0]

        run_id = _run_with_opps(org, [_opp(i) for i in range(5)])
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org))
        _record_feedback(org, _identity(), "dismiss")
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org))

        after = client.get(BASE, headers=_auth(org)).json()["groups"][0]
        assert after["netWeight"] == before["netWeight"], (
            "serving a list changed the stored adjustment — the state must only "
            "move on an explicit recomputation"
        )

    def test_every_recomputation_appends_to_history(self, client):
        """T4 reads this. History cannot be reconstructed retroactively."""
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        client.post(f"{BASE}/recompute", headers=_auth(org))

        history = client.get(f"{BASE}/history", headers=_auth(org)).json()
        assert len(history) >= 2
        kinds = {h["changeKind"] for h in history}
        assert "activated" in kinds
        assert "recomputed" in kinds
        assert history[0]["revision"] > history[-1]["revision"]

    def test_history_records_the_value_it_replaced(self, client):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        _record_feedback(org, _identity(), "dismiss")
        client.post(f"{BASE}/recompute", headers=_auth(org))

        history = client.get(f"{BASE}/history", headers=_auth(org)).json()
        assert history[0]["previousNetWeight"] is not None

    def test_a_cold_start_org_stores_its_state_as_inactive(self, client):
        """A zero that means "not enough evidence" and a zero that means
        "learning arrived at neutral" are different facts."""
        org = _org()
        _record_feedback(org, _identity(), "accept")

        body = client.post(f"{BASE}/recompute", headers=_auth(org)).json()
        assert body["changeKind"] == "recomputed"
        assert body["learningActive"] is False
        assert "not yet active" in (body["inactiveReason"] or "")
        assert body["learningState"]["status"] == "learning_not_yet_active"

    def test_the_state_records_the_config_version_it_was_computed_under(self, client):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        group = client.get(BASE, headers=_auth(org)).json()["groups"][0]
        assert group["configVersion"]


# ---------------------------------------------------------------------------
# AC1 — ranking adjusts within the cap; base scores unchanged and retrievable
# ---------------------------------------------------------------------------


class TestTheServedRankingAdjustsWithinTheCap:
    def test_the_served_list_carries_base_rank_and_base_impact(self, client):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        run_id = _run_with_opps(org, [_opp(i) for i in range(6)])

        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert served
        for item in served:
            assert "_ranking" in item
            # The RAW stored impact, not the display-shaped one: the adjustment
            # runs before with_display_scores adds its per-id matrix offset, so
            # the score cap is a fraction of the real score rather than of a
            # cosmetic one that varies by opportunity id.
            assert item["_ranking"]["baseImpact"] == 7
            assert isinstance(item["_ranking"]["baseRank"], int)

    def test_no_served_finding_moved_further_than_the_cap(self, client):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        run_id = _run_with_opps(org, [_opp(i) for i in range(12)])

        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        caps = served[0]["_ranking"]["caps"]
        for position, item in enumerate(served):
            assert abs(position - item["_ranking"]["baseRank"]) <= caps["maxRankMove"]

    def test_the_served_order_actually_changes(self, client):
        """AC1's other half — without this, every "within the cap" assertion
        above is satisfied by nothing moving at all.

        Two finding types with OPPOSITE signals: one the team keeps accepting,
        one it keeps dismissing. Seeding a single detector would give every
        finding the same delta, and a uniform shift reorders nothing.
        """
        org = _org()
        for _ in range(8):
            _record_feedback(org, _identity(), "accept", detector="FAVOURED")
            _record_feedback(org, _identity(), "dismiss", detector="DISFAVOURED")
        client.post(f"{BASE}/recompute", headers=_auth(org))

        # Disfavoured findings first, favoured last: learning should pull the
        # favoured ones up and push the disfavoured ones down.
        opps = [_opp(i, detector="DISFAVOURED") for i in range(5)]
        opps += [_opp(i + 5, detector="FAVOURED") for i in range(5)]
        run_id = _run_with_opps(org, opps)

        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert [o["id"] for o in served] != [o["id"] for o in opps], (
            "learning was active with opposing signals and the order did not "
            "change — the layer is not reaching the serve path"
        )

        moves = {
            o["id"]: o["_ranking"]["moved"]
            for o in served
            if o["_ranking"].get("moved")
        }
        assert moves, "no finding recorded a movement"
        favoured = {o["id"] for o in opps if o["_debug"]["detector_id"] == "FAVOURED"}
        for opp_id, moved in moves.items():
            if opp_id in favoured:
                assert moved < 0, f"{opp_id} was favoured but moved down"
            else:
                assert moved > 0, f"{opp_id} was disfavoured but moved up"

    def test_the_base_order_endpoint_returns_the_stored_order(self, client):
        """"What would this have ranked without learning?" — with nothing to undo."""
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)

        body = client.get(f"{BASE}/base-order/{run_id}", headers=_auth(org)).json()
        assert [row["id"] for row in body["order"]] == [o["id"] for o in opps]
        assert [row["baseRank"] for row in body["order"]] == list(range(8))

    def test_base_scores_survive_the_adjustment(self, client):
        """Compared against an UNLEARNED org, not against the stored value.

        The serve path applies a pre-existing display offset to impact/effort
        (``with_display_scores`` spreads bubbles on the matrix chart), so
        comparing the served score with the stored one would fail whether or not
        this layer existed. Serving the same opportunity payload to an org with
        no learning state isolates exactly this layer's effect. Each org gets
        its own run because run reads are org-scoped.
        """
        org, unlearned = _org(), _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i, impact=9) for i in range(6)]
        run_id = _run_with_opps(org, opps)
        baseline_run_id = _run_with_opps(unlearned, opps)

        with_learning = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        without = client.get(
            f"/api/runs/{baseline_run_id}/opportunities", headers=_auth(unlearned)
        ).json()
        baseline = {o["id"]: o for o in without}
        for item in with_learning:
            assert item["impact"] == baseline[item["id"]]["impact"]

    def test_the_stored_findings_are_never_rewritten(self, client):
        """The layer applies at serve time; storage keeps the base order."""
        from app.db import run_kv_get

        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org))

        stored = run_kv_get("opps", run_id)
        assert [o["id"] for o in stored] == [o["id"] for o in opps]
        assert all("_ranking" not in o for o in stored), (
            "the serve-time annotation leaked into storage"
        )

    def test_the_preview_shows_what_the_layer_would_do_and_why(self, client):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        run_id = _run_with_opps(org, [_opp(i) for i in range(10)])

        body = client.get(f"{BASE}/preview/{run_id}", headers=_auth(org)).json()
        assert body["applied"] is True
        assert body["maxMovement"] <= body["caps"]["maxRankMove"]
        for record in body["adjustments"]:
            assert "requestedDelta" in record and "appliedDelta" in record
            assert "wasCapped" in record


# ---------------------------------------------------------------------------
# The roadmap surface — adjusted on serve, stored in base order
# ---------------------------------------------------------------------------


class TestTheRoadmapIsAdjustedOnServeNotOnBuild:
    def test_the_stored_roadmap_is_base_order(self, client):
        """``build_roadmap`` runs during materialization and its output is
        PERSISTED. Learning there would bake the adjusted order into storage,
        and disabling learning could then never restore what was stored."""
        from app.db import run_kv_get, run_kv_set
        from app.roadmap_engine import build_roadmap

        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))

        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)
        run_kv_set("roadmap", run_id, build_roadmap(opps))

        client.get(f"/api/runs/{run_id}/roadmap", headers=_auth(org))

        stored = run_kv_get("roadmap", run_id)
        for stage in stored["stages"]:
            for item in stage.get("opportunities") or []:
                assert "_ranking" not in item, (
                    "the serve-time annotation was written into the stored roadmap"
                )

    def test_the_served_roadmap_carries_base_ranks(self, client):
        from app.db import run_kv_set
        from app.roadmap_engine import build_roadmap

        org = _org()
        for _ in range(8):
            _record_feedback(org, _identity(), "accept", detector="FAVOURED")
            _record_feedback(org, _identity(), "dismiss", detector="DISFAVOURED")
        client.post(f"{BASE}/recompute", headers=_auth(org))

        opps = [_opp(i, detector="DISFAVOURED") for i in range(4)]
        opps += [_opp(i + 4, detector="FAVOURED") for i in range(4)]
        run_id = _run_with_opps(org, opps)
        run_kv_set("roadmap", run_id, build_roadmap(opps))

        served = client.get(
            f"/api/runs/{run_id}/roadmap", headers=_auth(org)
        ).json()
        annotated = [
            item
            for stage in served["stages"]
            for item in (stage.get("opportunities") or [])
            if "_ranking" in item
        ]
        assert annotated, "the roadmap serve path did not route through the layer"
        for item in annotated:
            caps = item["_ranking"]["caps"]
            assert abs(item["_ranking"]["moved"]) <= caps["maxRankMove"]

    def test_stage_membership_is_never_changed_by_learning(self, client):
        """Learning reorders WITHIN a stage; tier decides which stage."""
        from app.db import run_kv_set
        from app.roadmap_engine import build_roadmap

        org = _org()
        for _ in range(8):
            _record_feedback(org, _identity(), "accept", detector="FAVOURED")
        client.post(f"{BASE}/recompute", headers=_auth(org))

        opps = [_opp(i, detector="FAVOURED") for i in range(6)]
        run_id = _run_with_opps(org, opps)
        roadmap = build_roadmap(opps)
        run_kv_set("roadmap", run_id, roadmap)

        served = client.get(f"/api/runs/{run_id}/roadmap", headers=_auth(org)).json()
        for built_stage, served_stage in zip(roadmap["stages"], served["stages"]):
            built_ids = {o["id"] for o in built_stage.get("opportunities") or []}
            served_ids = {o["id"] for o in served_stage.get("opportunities") or []}
            assert built_ids == served_ids, "learning moved a finding between stages"


# ---------------------------------------------------------------------------
# AC3 — evidence, confidence and corroboration are never modified
# ---------------------------------------------------------------------------


class TestAdjustmentNeverModifiesEvidenceConfidenceOrCorroboration:
    @pytest.mark.parametrize(
        "field",
        ["confidence", "evidenceIds", "corroboration_sources", "corroboration_label"],
    )
    def test_the_field_is_identical_before_and_after_adjustment(self, client, field):
        org = _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)

        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        original = {o["id"]: o for o in opps}
        for item in served:
            assert item[field] == original[item["id"]][field]

    def test_scoring_fields_are_identical_with_and_without_learning(self, client):
        """The honest form of "adjustment changes nothing but order".

        Both sides go through the identical serve path with identical run
        payloads; only the learning state differs. Anything that differs beyond
        ORDER and the ``_ranking`` annotation is this layer modifying a finding,
        which it must never do.
        """
        org, unlearned = _org(), _org()
        _seed_enough_to_activate(org)
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)
        baseline_run_id = _run_with_opps(unlearned, opps)

        with_learning = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        without = client.get(
            f"/api/runs/{baseline_run_id}/opportunities", headers=_auth(unlearned)
        ).json()

        baseline = {o["id"]: o for o in without}
        for item in with_learning:
            other = baseline[item["id"]]
            differing = {
                key
                for key in set(item) | set(other)
                if item.get(key) != other.get(key)
            }
            assert differing <= {"_ranking"}, (
                f"adjustment changed {sorted(differing - {'_ranking'})} on "
                f"{item['id']} — it may only change ORDER"
            )


# ---------------------------------------------------------------------------
# AC4 — cold-start honesty through the real stack
# ---------------------------------------------------------------------------


class TestColdStartThroughTheRealStack:
    def test_a_cold_start_org_serves_base_order(self, client):
        org = _org()
        _record_feedback(org, _identity(), "accept")
        client.post(f"{BASE}/recompute", headers=_auth(org))
        opps = [_opp(i) for i in range(8)]
        run_id = _run_with_opps(org, opps)

        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert [o["id"] for o in served] == [o["id"] for o in opps]

    def test_the_preview_reports_the_cold_start_reason(self, client):
        org = _org()
        _record_feedback(org, _identity(), "accept")
        client.post(f"{BASE}/recompute", headers=_auth(org))
        run_id = _run_with_opps(org, [_opp(i) for i in range(4)])

        body = client.get(f"{BASE}/preview/{run_id}", headers=_auth(org)).json()
        assert body["learningActive"] is False
        assert "not yet active" in (body["inactiveReason"] or "")
        assert body["learningState"]["remaining"]["decisions"] > 0

    def test_falling_back_below_the_floor_neutralises_stale_state(self, client):
        """If the current signal set is cold again, old active rows must not apply."""
        from app.learning_adjustment_state import get_adjustments, recompute_adjustments
        from app.learning_signals import collect_learning_signals

        org = _org()
        _seed_enough_to_activate(org)
        activated = client.post(f"{BASE}/recompute", headers=_auth(org)).json()
        assert activated["changeKind"] == "activated"
        assert get_adjustments(org)

        cold_record = {
            "feedbackId": "fb_cold",
            "opportunityIdentity": _identity(),
            "action": "accept",
            "reasonCode": None,
            "actorId": "analyst_1",
            "detectorId": "HANDOFF_FRICTION",
            "packId": "service_cloud",
            "recordedAt": datetime.now(timezone.utc).isoformat(),
        }
        cold_set = collect_learning_signals(
            org,
            decision_records=[cold_record],
            outcome_records=[],
        )
        deactivated = recompute_adjustments(
            org, signal_set=cold_set, actor_id=DEV_TOKEN
        )

        assert deactivated["changeKind"] == "deactivated"
        assert deactivated["learningActive"] is False
        assert get_adjustments(org) == {}
        state = client.get(BASE, headers=_auth(org)).json()
        assert all(group["learningActive"] is False for group in state["groups"])
        assert all(group["netWeight"] == 0 for group in state["groups"])
        history = client.get(f"{BASE}/history", headers=_auth(org)).json()
        assert any(row["changeKind"] == "deactivated" for row in history)

    def test_an_org_that_never_recomputed_serves_base_order(self, client):
        org = _org()
        opps = [_opp(i) for i in range(6)]
        run_id = _run_with_opps(org, opps)
        served = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert [o["id"] for o in served] == [o["id"] for o in opps]


# ---------------------------------------------------------------------------
# AC6 — two-org isolation
# ---------------------------------------------------------------------------


class TestTwoOrgIsolation:
    def test_one_orgs_state_never_appears_in_anothers(self, client):
        org_a, org_b = _org(), _org()
        _seed_enough_to_activate(org_a)
        client.post(f"{BASE}/recompute", headers=_auth(org_a))
        client.post(f"{BASE}/recompute", headers=_auth(org_b))

        assert client.get(BASE, headers=_auth(org_a)).json()["groups"]
        assert client.get(BASE, headers=_auth(org_b)).json()["groups"] == []

    def test_one_orgs_learning_never_reorders_anothers_list(self, client):
        org_a, org_b = _org(), _org()
        _seed_enough_to_activate(org_a)
        client.post(f"{BASE}/recompute", headers=_auth(org_a))

        opps = [_opp(i) for i in range(10)]
        run_id = _run_with_opps(org_b, opps)
        served_b = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org_b)
        ).json()
        assert [o["id"] for o in served_b] == [o["id"] for o in opps], (
            "org A's decisions reordered org B's list"
        )

    def test_history_is_org_scoped(self, client):
        org_a, org_b = _org(), _org()
        _seed_enough_to_activate(org_a)
        client.post(f"{BASE}/recompute", headers=_auth(org_a))
        assert client.get(f"{BASE}/history", headers=_auth(org_b)).json() == []


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_recompute_requires_analyst(self, client):
        response = client.post(
            f"{BASE}/recompute", headers=_auth(_org(), VIEWER_TOKEN)
        )
        assert response.status_code == 403

    def test_reading_the_state_requires_analyst(self, client):
        assert client.get(BASE, headers=_auth(_org(), VIEWER_TOKEN)).status_code == 403

    def test_an_unauthenticated_request_is_rejected(self, client):
        assert client.get(BASE).status_code in (401, 403)

    def test_a_missing_run_is_a_404_on_both_run_scoped_routes(self, client):
        org = _org()
        for path in (f"{BASE}/preview/run_nope", f"{BASE}/base-order/run_nope"):
            assert client.get(path, headers=_auth(org)).status_code == 404
