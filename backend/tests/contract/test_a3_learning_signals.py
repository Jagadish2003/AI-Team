"""Contract tests for 2.0-A3 T1 — the learning signal set, end to end.

The subtask's deliverables, one class each:

* accept / dismiss / **defer with reason** are recordable and durable;
* the record is append-only and keyed on the stable cross-run identity;
* A2 outcome results join to decisions on that identity and outweigh them;
* the set reports its own cold-start state (AC4);
* every signal links to the decision or measurement behind it (AC2);
* one org's decisions never reach another's signal set (AC6);
* the review decision flow feeds learning with no frontend change.

The weighting arithmetic is unit-tested in ``tests/unit/test_learning_signals.py``
where it needs no database. What is tested here is what only a live stack can
show: that the two stores actually join, that the API enforces the vocabulary,
and that the org boundary holds through the HTTP edge as well as in the SQL.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/learning"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    """Ensure the feedback table, and FAIL LOUDLY here if it is still absent.

    ``ensure_opportunity_feedback_table`` swallows its exception by design — a
    startup safety net must not crash the app. That is wrong for a test: a
    missing table then surfaces as an ``UndefinedTable`` on some later INSERT,
    which reads as a bug in the code under test rather than in the schema. This
    suite shares one PostgreSQL schema for the whole session (see
    ``conftest._heal_shared_auth_schema``), so schema state genuinely can be
    disturbed by an earlier test — worth diagnosing at setup, not at a random
    write.
    """
    from app.learning_feedback import ensure_opportunity_feedback_table

    ensure_opportunity_feedback_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT to_regclass('public.opportunity_feedback')")
            exists = cur.fetchone()[0]
    assert exists, (
        "opportunity_feedback is missing and could not be created — check that "
        "migration 0036 ran against the test database"
    )


def _table_exists(name: str) -> bool:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
            return cur.fetchone()[0] is not None


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
    org_id = f"org-a3t1-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN)
    _seed_workspace_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _post(client, org, identity, action, **body):
    payload = {"action": action}
    payload.update(body)
    return client.post(f"{BASE}/feedback/{identity}", json=payload, headers=_auth(org))


def _record(
    org: str,
    identity: str,
    action: str = "accept",
    *,
    detector_id: str = "HANDOFF_FRICTION",
    pack_id: str = "service_cloud",
    **kwargs,
):
    from app.learning_feedback import record_feedback

    return record_feedback(
        org,
        identity,
        action,
        actor_id="analyst_1",
        detector_id=detector_id,
        pack_id=pack_id,
        **kwargs,
    )


def _seed_movement(
    org: str,
    identity: str,
    *,
    verdict: str = "within_band",
    detector_id: str = "HANDOFF_FRICTION",
) -> None:
    """Seed one A2 movement record.

    Deriving a real measurement would need a frozen baseline and two runs — all
    covered by A2's own contract tests. What this subtask must show is that the
    signal set READS them, so the record itself is written at the store boundary.

    The LIFECYCLE action, however, is recorded through A2 T1's real path rather
    than seeded: ``list_movements`` filters out any record whose action date no
    longer matches a current lifecycle action, so a movement row inserted without
    one is invisible. Learning inherits that guard for free — a measurement whose
    action was reversed stops being a learning signal too — and short-cutting it
    here would have hidden that from the test.
    """
    from app.opportunity_lifecycle import (
        ensure_opportunity_lifecycle_tables,
        ensure_tracked,
        record_action,
    )
    from app.opportunity_movement import ensure_opportunity_movement_table

    ensure_opportunity_lifecycle_tables()
    ensure_opportunity_movement_table()
    now = datetime.now(timezone.utc)
    action_date = (now - timedelta(days=30)).date()

    ensure_tracked(org, identity, run_id="run_base")
    record_action(org, identity, action_date.isoformat(), "analyst_1")
    record = {
        "schemaVersion": "1.0.0",
        "orgId": org,
        "opportunityIdentity": identity,
        "detectorId": detector_id,
        "actionDate": action_date.isoformat(),
        "baselineRunId": "run_base",
        "currentRunId": "run_now",
        "movements": [],
        "comparability": {"verdict": "comparable"},
        "confounderSummary": {"count": 0, "materialCount": 0, "advisoryCount": 0},
        # The REAL shape built by projection_validation._projection_block: the
        # pack sits under `projected`, not at the top level. Seeding the flat
        # shape made the pack-grouping assertions pass while production data
        # would have carried packId=None.
        "projectionValidation": {
            "verdict": verdict,
            "projected": {"packId": "service_cloud", "packVersion": "1.2.0"},
        },
        "measuredAt": now.isoformat(),
    }
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_movements ("
                "  org_id, opportunity_identity, current_run_id, baseline_run_id,"
                "  detector_id, action_date, comparability_verdict, record,"
                "  measured_at, created_at, updated_at,"
                "  projection_validation_verdict"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (org_id, opportunity_identity, current_run_id)"
                " DO NOTHING",
                (
                    org,
                    identity,
                    "run_now",
                    "run_base",
                    detector_id,
                    action_date,
                    "comparable",
                    json.dumps(record),
                    now,
                    now,
                    now,
                    verdict,
                ),
            )
        con.commit()


# ---------------------------------------------------------------------------
# The three decisions the signal set learns from
# ---------------------------------------------------------------------------


class TestTheDecisionVocabulary:
    def test_accept_and_dismiss_are_recorded(self, client):
        org = _org()
        for action in ("accept", "dismiss"):
            response = _post(client, org, _identity(), action)
            assert response.status_code == 200, response.text
            assert response.json()["action"] == action

    def test_defer_with_a_reason_is_recorded(self, client):
        """The capability the review enum has no room for.

        ``APPROVED``/``REJECTED``/``UNREVIEWED`` is validated in two places in
        main.py, one of them the EVIDENCE decision where deferring is
        meaningless — so defer gets its own route rather than widening a shared
        contract with a state invalid at one of its call sites.
        """
        org = _org()
        response = _post(
            client, org, _identity(), "defer", reasonCode="lower_priority"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["action"] == "defer"
        assert body["reasonCode"] == "lower_priority"

    def test_a_defer_without_a_reason_is_refused(self, client):
        """'Not now' with no stated why carries nothing to learn from.

        Defaulting a reason would mean the layer invented the thing it then
        learned from.
        """
        response = _post(client, _org(), _identity(), "defer")
        assert response.status_code == 400
        assert "reason" in response.json()["detail"].lower()

    def test_a_free_text_reason_is_refused(self, client):
        """A reason the layer cannot group on teaches it nothing.

        Free text in a learning input is also an unbounded PII surface.
        """
        response = _post(
            client,
            _org(),
            _identity(),
            "defer",
            reasonCode="the quarter end is coming up and Dave is on leave",
        )
        assert response.status_code == 400
        assert "other" in response.json()["detail"]

    def test_elaboration_is_carried_but_kept_out_of_the_vocabulary(self, client):
        org, identity = _org(), _identity()
        response = _post(
            client,
            org,
            identity,
            "defer",
            reasonCode="other",
            reasonDetail="Waiting on the platform migration in Q4.",
        )
        assert response.status_code == 200
        assert "migration" in response.json()["reasonDetail"]

    def test_an_unknown_action_is_refused(self, client):
        response = _post(client, _org(), _identity(), "maybe_later")
        assert response.status_code == 400

    def test_the_vocabulary_is_advertised_rather_than_hardcoded_by_clients(
        self, client
    ):
        body = client.get(f"{BASE}/vocabulary", headers=_auth(_org())).json()
        assert set(body["actions"]) == {"accept", "dismiss", "defer"}
        assert body["deferRequiresReason"] is True
        reasons = {r["code"]: r for r in body["deferReasons"]}
        assert reasons["no_capacity"]["informsRanking"] is False, (
            "a resourcing fact about the team is not a judgement about the "
            "finding; the API must say so, so a client can show it"
        )
        assert reasons["lower_priority"]["informsRanking"] is True


# ---------------------------------------------------------------------------
# Durability — the reason this is not the review decision field
# ---------------------------------------------------------------------------


class TestTheRecordIsDurableAndAppendOnly:
    def test_changing_your_mind_appends_rather_than_overwrites(self, client):
        org, identity = _org(), _identity()
        _post(client, org, identity, "defer", reasonCode="timing_not_right")
        _post(client, org, identity, "accept")

        history = client.get(
            f"{BASE}/feedback/{identity}", headers=_auth(org)
        ).json()
        assert [entry["action"] for entry in history] == ["defer", "accept"], (
            "the earlier judgement must survive: what the team thought at the "
            "time is part of the record, and a store that edits its own history "
            "cannot answer 'why was this ranked higher last month?'"
        )

    def test_each_decision_gets_its_own_stable_id(self, client):
        """AC2's links resolve against these."""
        org, identity = _org(), _identity()
        first = _post(client, org, identity, "accept").json()["feedbackId"]
        second = _post(client, org, identity, "dismiss").json()["feedbackId"]
        assert first != second

        fetched = client.get(
            f"{BASE}/feedback/entry/{first}", headers=_auth(org)
        ).json()
        assert fetched["feedbackId"] == first
        assert fetched["action"] == "accept"

    def test_the_record_survives_a_wholesale_rewrite_of_the_run_opps_blob(self):
        """The failure the run-scoped `decision` field has and this does not.

        Materialization rewrites ``opps`` wholesale and replay resets
        ``decision``; a learning signal stored there would not survive to inform
        the next run.
        """
        from app.db import run_kv_set
        from app.learning_feedback import get_feedback_history

        org, identity = _org(), _identity()
        _record(org, identity, "accept")

        # The KV rewrite is the realistic TRIGGER, not the assertion — and the
        # `kv` table belongs to another subsystem in a session-shared schema, so
        # its absence would say nothing about this record. Skip the trigger if it
        # is not there; the claim being tested is the record's independence.
        if _table_exists("kv"):
            run_kv_set("opps", "run_x", [])  # what materialization does every run

        assert len(get_feedback_history(org, identity)) == 1

    def test_the_signal_set_counts_a_teams_current_position_not_its_clicks(self):
        """Repeat decisions on one finding are one data point, not many."""
        from app.learning_signals import collect_learning_signals

        org, identity = _org(), _identity()
        for _ in range(5):
            _record(org, identity, "accept")

        signal_set = collect_learning_signals(org)
        decisions = [s for s in signal_set.signals if s.source == "decision"]
        assert len(decisions) == 1

    def test_only_the_latest_position_is_weighted(self):
        from app.learning_signals import collect_learning_signals

        org, identity = _org(), _identity()
        _record(org, identity, "accept")
        _record(org, identity, "dismiss")

        signal_set = collect_learning_signals(org)
        decisions = [s for s in signal_set.signals if s.source == "decision"]
        assert [s.direction for s in decisions] == ["negative"]


# ---------------------------------------------------------------------------
# The join, and the weighting principle, over real stored data
# ---------------------------------------------------------------------------


class TestOutcomesJoinDecisionsOnIdentity:
    def test_a_decision_and_an_outcome_about_one_finding_both_appear(self):
        from app.learning_signals import collect_learning_signals

        org, identity = _org(), _identity()
        _record(org, identity, "accept")
        _seed_movement(org, identity)

        signals = collect_learning_signals(org).for_identity(identity)
        assert {s.source for s in signals} == {"decision", "outcome"}

    def test_the_outcome_outweighs_the_decision_over_stored_data(self):
        from app.learning_signals import collect_learning_signals

        org, identity = _org(), _identity()
        _record(org, identity, "accept")
        _seed_movement(org, identity)

        by_source = {
            s.source: s for s in collect_learning_signals(org).for_identity(identity)
        }
        assert by_source["outcome"].weight > by_source["decision"].weight

    def test_a_group_reports_that_it_has_measured_evidence(self):
        """What T2's explanation needs to say 'and one delivered improvement'."""
        from app.learning_signals import collect_learning_signals, group_by_similarity

        org = _org()
        _record(org, _identity(), "accept")
        _seed_movement(org, _identity())

        groups = group_by_similarity(collect_learning_signals(org))
        assert any(g.has_outcome_evidence for g in groups)

    def test_every_signal_links_to_what_produced_it(self):
        """AC2 groundwork: no signal without a resolvable reference."""
        from app.learning_signals import collect_learning_signals

        org, identity = _org(), _identity()
        recorded = _record(org, identity, "accept")
        _seed_movement(org, identity)

        signals = collect_learning_signals(org).for_identity(identity)
        assert len(signals) == 2
        for signal in signals:
            assert signal.evidence_ref.get("kind") in ("decision", "outcome")
        refs = {s.source: s.evidence_ref for s in signals}
        assert refs["decision"]["feedbackId"] == recorded["feedbackId"]
        assert refs["outcome"]["currentRunId"] == "run_now"


# ---------------------------------------------------------------------------
# AC4 — cold-start honesty
# ---------------------------------------------------------------------------


class TestColdStartHonesty:
    def test_a_new_org_reports_learning_not_yet_active(self, client):
        body = client.get(f"{BASE}/signals", headers=_auth(_org())).json()
        assert body["isActive"] is False
        assert "not yet active" in body["inactiveReason"]

    def test_three_decisions_do_not_activate_learning(self, client):
        org = _org()
        for _ in range(3):
            _record(org, _identity(), "accept")

        body = client.get(f"{BASE}/signals", headers=_auth(org)).json()
        assert body["isActive"] is False, (
            "no pretending to personalise from three data points"
        )

    def test_enough_signals_across_enough_findings_activates_learning(self, client):
        from app.learning_signal_config import load_config

        org = _org()
        config = load_config()
        for _ in range(config.cold_start.minimum_signals):
            _record(org, _identity(), "accept")

        body = client.get(f"{BASE}/signals", headers=_auth(org)).json()
        assert body["isActive"] is True
        assert body["inactiveReason"] is None

    def test_the_threshold_is_reported_alongside_the_state(self, client):
        """The UI must be able to say how far off activation is."""
        body = client.get(f"{BASE}/signals", headers=_auth(_org())).json()
        assert body["thresholds"]["minimumSignals"] >= 1
        assert body["thresholds"]["minimumDistinctIdentities"] >= 1
        assert body["counts"]["weighted"] == 0


# ---------------------------------------------------------------------------
# AC6 — two-org isolation
# ---------------------------------------------------------------------------


class TestTwoOrgIsolation:
    def test_one_orgs_decisions_never_appear_in_anothers_signal_set(self, client):
        org_a, org_b = _org(), _org()
        for _ in range(12):
            _record(org_a, _identity(), "accept")

        a_body = client.get(f"{BASE}/signals", headers=_auth(org_a)).json()
        b_body = client.get(f"{BASE}/signals", headers=_auth(org_b)).json()

        assert a_body["counts"]["weighted"] == 12
        assert b_body["counts"]["weighted"] == 0
        assert b_body["isActive"] is False, (
            "one org's decisions must never activate another org's learning"
        )

    def test_the_same_identity_in_two_orgs_holds_two_independent_positions(self):
        from app.learning_signals import collect_learning_signals

        org_a, org_b = _org(), _org()
        identity = _identity()
        _record(org_a, identity, "accept")
        _record(org_b, identity, "dismiss")

        a = collect_learning_signals(org_a).for_identity(identity)[0]
        b = collect_learning_signals(org_b).for_identity(identity)[0]
        assert a.direction == "positive"
        assert b.direction == "negative"

    def test_a_cross_org_feedback_id_is_not_readable(self, client):
        """404, identically to a missing one — never a 403 that confirms it exists."""
        org_a, org_b = _org(), _org()
        feedback_id = _record(org_a, _identity(), "accept")["feedbackId"]

        response = client.get(
            f"{BASE}/feedback/entry/{feedback_id}", headers=_auth(org_b)
        )
        assert response.status_code == 404

    def test_the_org_feedback_list_never_leaks_another_org(self, client):
        org_a, org_b = _org(), _org()
        _record(org_a, _identity(), "accept")
        _record(org_b, _identity(), "dismiss")

        listed = client.get(f"{BASE}/feedback", headers=_auth(org_a)).json()
        assert len(listed) == 1
        assert listed[0]["action"] == "accept"

    def test_an_outcome_in_one_org_never_reaches_anothers_signal_set(self):
        from app.learning_signals import collect_learning_signals

        org_a, org_b = _org(), _org()
        identity = _identity()
        _seed_movement(org_a, identity)

        assert collect_learning_signals(org_a).for_identity(identity)
        assert not collect_learning_signals(org_b).for_identity(identity)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_recording_a_decision_requires_analyst(self, client):
        response = client.post(
            f"{BASE}/feedback/{_identity()}",
            json={"action": "accept"},
            headers=_auth(_org(), VIEWER_TOKEN),
        )
        assert response.status_code == 403

    def test_reading_the_signal_set_requires_analyst(self, client):
        response = client.get(f"{BASE}/signals", headers=_auth(_org(), VIEWER_TOKEN))
        assert response.status_code == 403, (
            "what a team has accepted and dismissed is customer-operational "
            "information, not public"
        )

    def test_an_unauthenticated_request_is_rejected(self, client):
        assert client.get(f"{BASE}/signals").status_code in (401, 403)


# ---------------------------------------------------------------------------
# The weighting is inspectable — A3's "never invisible drift" discipline
# ---------------------------------------------------------------------------


class TestTheWeightingIsInspectable:
    def test_the_config_endpoint_exposes_the_weights_in_force(self, client):
        body = client.get(f"{BASE}/config", headers=_auth(_org())).json()
        assert body["outcomeSignals"]["within_band"]["weight"] > 0
        assert body["decisionSignals"]["accept"]["weight"] > 0

    def test_the_config_endpoint_declares_how_well_founded_each_part_is(self, client):
        """A customer is entitled to know most of these are still first guesses."""
        body = client.get(f"{BASE}/config", headers=_auth(_org())).json()
        assert body["bases"]["outcome_signals"] == "provisional"
        assert body["bases"]["comparability"] == "operationally_justified"

    def test_the_served_weights_preserve_the_governing_principle(self, client):
        body = client.get(f"{BASE}/config", headers=_auth(_org())).json()
        outcomes = [
            v["weight"] for v in body["outcomeSignals"].values() if v["weight"] > 0
        ]
        decisions = [v["weight"] for v in body["decisionSignals"].values()]
        assert min(outcomes) > max(decisions)

    def test_a_signal_reports_the_multipliers_behind_its_weight(self, client):
        org, identity = _org(), _identity()
        _record(org, identity, "accept")

        body = client.get(f"{BASE}/signals", headers=_auth(org)).json()
        signal = next(s for s in body["signals"] if s["source"] == "decision")
        assert "recency" in signal["multipliers"]
        assert signal["label"], "every signal carries plain-language wording"

    def test_the_set_can_be_narrowed_to_one_finding_type(self, client):
        """What an explainability surface asks: what do SIMILAR findings say?"""
        org = _org()
        _record(org, _identity(), "accept", detector_id="HANDOFF_FRICTION")
        _record(org, _identity(), "accept", detector_id="APPROVAL_BOTTLENECK")

        body = client.get(
            f"{BASE}/signals",
            params={"detectorId": "HANDOFF_FRICTION", "packId": "service_cloud"},
            headers=_auth(org),
        ).json()
        assert len(body["similarTo"]["signals"]) == 1
        assert body["similarTo"]["signals"][0]["similarityScore"] == 1.0


# ---------------------------------------------------------------------------
# The existing review flow feeds learning
# ---------------------------------------------------------------------------


class TestTheReviewDecisionFeedsLearning:
    def test_approving_an_opportunity_records_a_learning_accept(self, client):
        """No frontend change required for the common case."""
        from app.db import run_kv_set
        from app.learning_feedback import get_feedback_history
        from app.run_store import start_run_

        org, identity = _org(), _identity()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        run_kv_set(
            "opps",
            run_id,
            [
                {
                    "id": "opp_001",
                    "opportunity_identity": identity,
                    "packId": "service_cloud",
                    "decision": "UNREVIEWED",
                    "_debug": {"detector_id": "HANDOFF_FRICTION"},
                }
            ],
        )

        response = client.post(
            f"/api/runs/{run_id}/opportunities/opp_001/decision",
            json={"decision": "APPROVED"},
            headers=_auth(org),
        )
        assert response.status_code == 200, response.text

        history = get_feedback_history(org, identity)
        assert [entry["action"] for entry in history] == ["accept"]
        assert history[0]["detectorId"] == "HANDOFF_FRICTION"

    def test_clearing_a_decision_is_not_recorded_as_a_judgement(self, client):
        """UNREVIEWED is the absence of a judgement, not a third kind of one."""
        from app.db import run_kv_set
        from app.learning_feedback import get_feedback_history
        from app.run_store import start_run_

        org, identity = _org(), _identity()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        run_kv_set(
            "opps",
            run_id,
            [
                {
                    "id": "opp_001",
                    "opportunity_identity": identity,
                    "packId": "service_cloud",
                    "decision": "APPROVED",
                    "_debug": {"detector_id": "HANDOFF_FRICTION"},
                }
            ],
        )

        client.post(
            f"/api/runs/{run_id}/opportunities/opp_001/decision",
            json={"decision": "UNREVIEWED"},
            headers=_auth(org),
        )
        assert get_feedback_history(org, identity) == []

    def test_a_finding_without_a_stable_identity_is_skipped_not_mis_keyed(
        self, client
    ):
        """Better no signal than one that cannot be matched on the next run.

        A signal keyed on a run-scoped id would count towards the cold-start
        threshold while informing nothing — worse than its absence.
        """
        from app.db import run_kv_set
        from app.learning_feedback import count_feedback
        from app.run_store import start_run_

        org = _org()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        run_kv_set(
            "opps",
            run_id,
            [{"id": "opp_001", "packId": "service_cloud", "decision": "UNREVIEWED"}],
        )

        response = client.post(
            f"/api/runs/{run_id}/opportunities/opp_001/decision",
            json={"decision": "APPROVED"},
            headers=_auth(org),
        )
        assert response.status_code == 200, "review must not break"
        assert count_feedback(org) == 0

    def test_a_learning_failure_never_breaks_the_review_decision(
        self, client, monkeypatch
    ):
        from app.db import run_kv_set
        from app.run_store import start_run_

        org = _org()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        run_kv_set(
            "opps",
            run_id,
            [
                {
                    "id": "opp_001",
                    "opportunity_identity": _identity(),
                    "packId": "service_cloud",
                    "decision": "UNREVIEWED",
                }
            ],
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("learning store is down")

        monkeypatch.setattr("app.learning_feedback.record_feedback", _boom)

        response = client.post(
            f"/api/runs/{run_id}/opportunities/opp_001/decision",
            json={"decision": "APPROVED"},
            headers=_auth(org),
        )
        assert response.status_code == 200
