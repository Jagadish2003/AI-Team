"""Contract tests for 2.0-A2 T1 — the persisted opportunity lifecycle.

The subtask's definition of done, one test class each:

* state is retrievable for any ``opportunity_identity``;
* a run that re-surfaces an opportunity does NOT reset its lifecycle;
* the transition history is append-only and queryable;
* every transition appears in the audit log with actor, org, target, timestamp;
* illegal transitions are refused with a named reason;
* there is no code path that sets ``actioned`` without a human-supplied date;
* two-org isolation holds for reads and writes.

Plus the orthogonality the subtask insists on: the review ``decision`` field and
the lifecycle ``state`` are different axes and neither touches the other.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")

BASE = "/api/opportunity-lifecycle"
YESTERDAY = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
TOMORROW = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


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
    org_id = f"org-a2t1-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN)
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.opportunity_lifecycle import ensure_opportunity_lifecycle_tables

    ensure_opportunity_lifecycle_tables()


def _track(client: TestClient, org: str, identity: str, run_id: str | None = None):
    body = {"runId": run_id} if run_id else {}
    response = client.post(f"{BASE}/{identity}/track", headers=_auth(org), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _audit_rows(org_id: str, identity: str) -> List[Dict[str, Any]]:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT event_type, user_id, payload, timestamp FROM audit_log "
                "WHERE org_id = %s AND event_type = %s ORDER BY timestamp ASC",
                (org_id, "opportunity_lifecycle_transitioned"),
            )
            rows = cur.fetchall()
    out = []
    for event_type, user_id, payload, ts in rows:
        parsed = json.loads(payload) if payload else {}
        if parsed.get("opportunity_identity") == identity:
            out.append(
                {
                    "event_type": event_type,
                    "user_id": user_id,
                    "payload": parsed,
                    "timestamp": ts,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Retrievable state
# ---------------------------------------------------------------------------


class TestStateIsRetrievable:
    def test_tracking_starts_at_open_and_is_retrievable(self, client):
        org, identity = _org(), _identity()
        tracked = _track(client, org, identity, run_id="run_1")

        assert tracked["state"] == "open"
        assert tracked["actionDate"] is None
        assert tracked["measurable"] is False
        assert tracked["revision"] == 0

        fetched = client.get(f"{BASE}/{identity}", headers=_auth(org))
        assert fetched.status_code == 200
        assert fetched.json()["state"] == "open"

    def test_an_untracked_identity_is_404(self, client):
        org = _org()
        response = client.get(f"{BASE}/{_identity()}", headers=_auth(org))
        assert response.status_code == 404

    def test_state_carries_its_legal_next_states(self, client):
        """So a UI never hard-codes its own copy of the transition table."""
        org, identity = _org(), _identity()
        state = _track(client, org, identity)
        assert set(state["legalNextStates"]) == {"actioned", "dismissed"}

    def test_the_state_machine_is_served(self, client):
        org = _org()
        response = client.get(f"{BASE}/states", headers=_auth(org))
        assert response.status_code == 200
        summary = response.json()
        assert summary["initialState"] == "open"
        assert "actioned" not in summary["systemReachableStates"]

    def test_the_org_list_is_filterable_by_state(self, client):
        org = _org()
        open_identity, actioned_identity = _identity(), _identity()
        _track(client, org, open_identity)
        _track(client, org, actioned_identity)
        client.post(
            f"{BASE}/{actioned_identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )

        listed = client.get(f"{BASE}?state=actioned", headers=_auth(org)).json()
        identities = {item["opportunityIdentity"] for item in listed["items"]}
        assert identities == {actioned_identity}

    def test_an_unknown_state_filter_is_refused_with_the_valid_set(self, client):
        org = _org()
        response = client.get(f"{BASE}?state=bogus", headers=_auth(org))
        assert response.status_code == 400
        assert "bogus" in response.json()["detail"]
        assert "open" in response.json()["detail"]


# ---------------------------------------------------------------------------
# A re-surfacing run must not reset the lifecycle
# ---------------------------------------------------------------------------


class TestRunsDoNotResetLifecycle:
    def test_re_tracking_an_actioned_opportunity_keeps_its_state(self, client):
        """The single most important persistence property in this subtask."""
        org, identity = _org(), _identity()
        _track(client, org, identity, run_id="run_1")
        client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )

        # A later run re-surfaces the same problem.
        re_tracked = _track(client, org, identity, run_id="run_2")

        assert re_tracked["state"] == "actioned", (
            "a run that re-surfaces an opportunity must not reset its lifecycle"
        )
        assert re_tracked["actionDate"] == YESTERDAY
        assert re_tracked["lastRunId"] == "run_2", "only the run pointer moves"
        assert re_tracked["firstSeenRunId"] == "run_1"

    def test_re_tracking_does_not_bump_the_revision(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity, run_id="run_1")
        client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )
        before = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["revision"]

        _track(client, org, identity, run_id="run_2")
        after = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["revision"]
        assert after == before, "tracking is not a transition and must not appear as one"

    def test_the_pipeline_helper_is_insert_only(self):
        from app.opportunity_lifecycle import ensure_tracked_many, record_action

        org, identity = _org(), _identity()
        assert ensure_tracked_many(org, [identity], run_id="run_1") == 1
        record_action(org, identity, YESTERDAY, "analyst@example.com")

        assert ensure_tracked_many(org, [identity], run_id="run_2") == 1
        from app.opportunity_lifecycle import get_lifecycle

        assert get_lifecycle(org, identity)["state"] == "actioned"

    def test_tracking_is_idempotent_and_never_raises_on_a_bad_identity(self):
        from app.opportunity_lifecycle import ensure_tracked_many

        org = _org()
        # Blank identities are skipped, not fatal — a lifecycle failure must
        # never break a discovery run.
        assert ensure_tracked_many(org, ["", None, _identity()], run_id="r") == 1


# ---------------------------------------------------------------------------
# The non-inference rule, on the wire
# ---------------------------------------------------------------------------


class TestNoActionWithoutAHumanSuppliedDate:
    def test_recording_an_action_requires_the_date(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)

        response = client.post(f"{BASE}/{identity}/action", headers=_auth(org), json={})
        assert response.status_code == 422, (
            "a missing action date must be refused by the request model, before "
            "any handler can default it"
        )

    def test_a_future_action_date_is_refused(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)

        response = client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": TOMORROW},
        )
        assert response.status_code == 400
        assert "future" in response.json()["detail"]

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "31/12/2026"])
    def test_a_malformed_action_date_is_refused(self, client, bad):
        org, identity = _org(), _identity()
        _track(client, org, identity)

        response = client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": bad}
        )
        assert response.status_code in (400, 422)

    def test_a_valid_action_records_the_date_and_the_actor(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)

        state = client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY, "note": "agent deployed to prod"},
        ).json()

        assert state["state"] == "actioned"
        assert state["actionDate"] == YESTERDAY
        assert state["actionedBy"]
        assert state["actionedAt"]
        assert state["measurable"] is True

    def test_no_route_can_request_a_platform_state(self, client):
        """monitoring / measured / stalled are the platform's own moves."""
        org, identity = _org(), _identity()
        _track(client, org, identity)
        for state in ("monitoring", "measured", "stalled"):
            response = client.post(f"{BASE}/{identity}/{state}", headers=_auth(org))
            assert response.status_code in (404, 405), (
                f"a client must not be able to request {state!r}"
            )

    def test_the_system_cannot_mark_something_actioned(self):
        from app.opportunity_lifecycle import ensure_tracked, system_transition
        from app.opportunity_lifecycle_states import LifecycleTransitionError

        org, identity = _org(), _identity()
        ensure_tracked(org, identity)
        with pytest.raises(LifecycleTransitionError) as excinfo:
            system_transition(org, identity, "actioned")
        assert "never infers" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Legality and refusals
# ---------------------------------------------------------------------------


class TestIllegalTransitionsAreRefused:
    def test_dismissing_twice_is_refused_with_a_reason(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        assert client.post(f"{BASE}/{identity}/dismiss", headers=_auth(org)).status_code == 200

        second = client.post(f"{BASE}/{identity}/dismiss", headers=_auth(org))
        assert second.status_code == 409
        assert "already the current state" in second.json()["detail"]

    def test_actioning_a_dismissed_opportunity_is_refused(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(f"{BASE}/{identity}/dismiss", headers=_auth(org))

        response = client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "dismissed" in detail and "actioned" in detail
        assert "Legal human targets" in detail, "the refusal must name the alternatives"

    def test_the_system_progression_is_legal_in_order_only(self):
        from app.opportunity_lifecycle import (
            ensure_tracked,
            record_action,
            system_transition,
        )
        from app.opportunity_lifecycle_states import LifecycleTransitionError

        org, identity = _org(), _identity()
        ensure_tracked(org, identity)

        # measured is not reachable straight from open.
        with pytest.raises(LifecycleTransitionError):
            system_transition(org, identity, "measured")

        record_action(org, identity, YESTERDAY, "analyst@example.com")
        assert system_transition(org, identity, "monitoring")["state"] == "monitoring"
        assert system_transition(org, identity, "measured")["state"] == "measured"

    def test_stalled_is_reachable_only_after_an_action(self):
        from app.opportunity_lifecycle import (
            ensure_tracked,
            record_action,
            system_transition,
        )
        from app.opportunity_lifecycle_states import LifecycleTransitionError

        org, identity = _org(), _identity()
        ensure_tracked(org, identity)
        with pytest.raises(LifecycleTransitionError):
            system_transition(org, identity, "stalled")

        record_action(org, identity, YESTERDAY, "analyst@example.com")
        assert system_transition(org, identity, "stalled")["state"] == "stalled"


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


class TestReversibility:
    def test_an_analyst_can_unwind_a_wrong_action(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )

        reopened = client.post(f"{BASE}/{identity}/reopen", headers=_auth(org))
        assert reopened.status_code == 200
        state = reopened.json()

        assert state["state"] == "open"
        assert state["actionDate"] is None, "the wrong pivot must not survive"
        assert state["measurable"] is False

    def test_the_unwind_is_visible_in_history_not_a_silent_rewrite(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org),
            json={"actionDate": YESTERDAY},
        )
        client.post(f"{BASE}/{identity}/reopen", headers=_auth(org))

        history = client.get(f"{BASE}/{identity}/history", headers=_auth(org)).json()
        transitions = history["transitions"]

        assert [t["toState"] for t in transitions] == ["actioned", "open"]
        assert transitions[0]["actionDate"] == YESTERDAY, (
            "the original mistake must still be in the record"
        )
        assert transitions[1]["actionDate"] is None
        assert "unwound" in transitions[1]["reason"]

    def test_re_actioning_after_an_unwind_needs_a_fresh_date(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        client.post(f"{BASE}/{identity}/reopen", headers=_auth(org))

        assert client.post(f"{BASE}/{identity}/action", headers=_auth(org), json={}).status_code == 422
        again = client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        assert again.status_code == 200
        assert again.json()["state"] == "actioned"


# ---------------------------------------------------------------------------
# Append-only history
# ---------------------------------------------------------------------------


class TestHistoryIsAppendOnly:
    def test_every_transition_appends_a_row_with_an_increasing_revision(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        from app.opportunity_lifecycle import system_transition

        system_transition(org, identity, "monitoring", run_id="run_2")
        system_transition(org, identity, "measured", run_id="run_3")

        history = client.get(f"{BASE}/{identity}/history", headers=_auth(org)).json()
        transitions = history["transitions"]

        assert [t["toState"] for t in transitions] == [
            "actioned",
            "monitoring",
            "measured",
        ]
        assert [t["revision"] for t in transitions] == [1, 2, 3]
        assert [t["actor"] for t in transitions] == ["human", "system", "system"]

    def test_history_records_the_actor_and_the_reason_for_each_move(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )

        transition = client.get(
            f"{BASE}/{identity}/history", headers=_auth(org)
        ).json()["transitions"][0]
        assert transition["fromState"] == "open"
        assert transition["actorId"]
        assert transition["reason"]
        assert transition["transitionedAt"]

    def test_the_action_date_is_carried_through_system_moves(self, client):
        """A system move must never disturb the human-recorded pivot."""
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        from app.opportunity_lifecycle import system_transition

        moved = system_transition(org, identity, "monitoring")
        assert moved["actionDate"] == YESTERDAY

    def test_history_is_404_for_an_untracked_identity(self, client):
        org = _org()
        response = client.get(f"{BASE}/{_identity()}/history", headers=_auth(org))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestEveryTransitionIsAudited:
    def test_a_human_transition_appears_in_the_audit_log(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )

        rows = _audit_rows(org, identity)
        assert rows, "no audit row for the transition"
        row = rows[-1]
        assert row["user_id"], "audit must name the actor"
        assert row["timestamp"], "audit must carry a timestamp"
        assert row["payload"]["target"] == identity
        assert row["payload"]["from_state"] == "open"
        assert row["payload"]["to_state"] == "actioned"
        assert row["payload"]["actor"] == "human"
        assert row["payload"]["action_date"] == YESTERDAY

    def test_a_system_transition_is_audited_too(self, client):
        """A state change must never appear in the portfolio unaudited."""
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        from app.opportunity_lifecycle import system_transition

        system_transition(org, identity, "monitoring", run_id="run_2")

        to_states = [r["payload"]["to_state"] for r in _audit_rows(org, identity)]
        assert "monitoring" in to_states

    def test_every_transition_has_exactly_one_audit_row(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        client.post(
            f"{BASE}/{identity}/action", headers=_auth(org), json={"actionDate": YESTERDAY}
        )
        client.post(f"{BASE}/{identity}/reopen", headers=_auth(org))

        history = client.get(f"{BASE}/{identity}/history", headers=_auth(org)).json()
        assert len(_audit_rows(org, identity)) == len(history["transitions"])


# ---------------------------------------------------------------------------
# RBAC and two-org isolation
# ---------------------------------------------------------------------------


class TestRbacAndTenancy:
    def test_reads_and_writes_require_authentication(self, client):
        org, identity = _org(), _identity()
        assert client.get(f"{BASE}/{identity}").status_code in (401, 403)
        assert client.post(
            f"{BASE}/{identity}/action", json={"actionDate": YESTERDAY}
        ).status_code in (401, 403)

    def test_a_viewer_cannot_transition(self, client):
        org, identity = _org(), _identity()
        _track(client, org, identity)
        _seed_workspace_member(org, VIEWER_TOKEN, role="viewer")

        response = client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org, VIEWER_TOKEN),
            json={"actionDate": YESTERDAY},
        )
        assert response.status_code == 403

    def test_one_org_cannot_read_anothers_lifecycle(self, client):
        org_a, org_b = _org(), _org()
        identity = _identity()
        _track(client, org_a, identity)

        response = client.get(f"{BASE}/{identity}", headers=_auth(org_b))
        assert response.status_code == 404, (
            "a cross-org read must answer 404, never confirm the identity exists "
            "in another tenant"
        )

    def test_one_org_cannot_transition_anothers_lifecycle(self, client):
        org_a, org_b = _org(), _org()
        identity = _identity()
        _track(client, org_a, identity)

        response = client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org_b),
            json={"actionDate": YESTERDAY},
        )
        assert response.status_code == 404

        from app.opportunity_lifecycle import get_lifecycle

        assert get_lifecycle(org_a, identity)["state"] == "open", (
            "org A's state must be untouched by org B's attempt"
        )

    def test_the_same_identity_is_independent_in_two_orgs(self, client):
        """The identity hash can legitimately collide across orgs.

        It includes org_id, so in practice it will not — but the store must be
        keyed on both regardless, and this proves it.
        """
        org_a, org_b = _org(), _org()
        identity = _identity()
        _track(client, org_a, identity)
        _track(client, org_b, identity)
        client.post(
            f"{BASE}/{identity}/action",
            headers=_auth(org_a),
            json={"actionDate": YESTERDAY},
        )

        assert client.get(f"{BASE}/{identity}", headers=_auth(org_a)).json()["state"] == "actioned"
        assert client.get(f"{BASE}/{identity}", headers=_auth(org_b)).json()["state"] == "open"

    def test_the_org_list_never_leaks_another_orgs_rows(self, client):
        org_a, org_b = _org(), _org()
        _track(client, org_a, _identity())
        _track(client, org_b, _identity())

        listed = client.get(BASE, headers=_auth(org_a)).json()
        assert listed["count"] == 1
        assert all(item["orgId"] == org_a for item in listed["items"])


# ---------------------------------------------------------------------------
# Orthogonality with the review decision
# ---------------------------------------------------------------------------


class TestLifecycleIsOrthogonalToDecision:
    def test_the_lifecycle_state_is_not_a_decision_value(self, client):
        """The two axes answer different questions and share no vocabulary."""
        from app.opportunity_lifecycle_states import ALL_STATES

        assert not ({"APPROVED", "REJECTED", "UNREVIEWED"} & set(ALL_STATES))

    def test_an_approved_opportunity_can_sit_at_open(self, client):
        """Agreeing a finding is real is not acting on it."""
        org, identity = _org(), _identity()
        state = _track(client, org, identity)
        assert state["state"] == "open"
        # Nothing in the lifecycle payload mentions the review decision.
        assert "decision" not in state

    def test_the_lifecycle_store_never_reads_or_writes_decision(self):
        from pathlib import Path

        import app.opportunity_lifecycle as store

        source = Path(store.__file__).read_text(encoding="utf-8")
        for token in ("APPROVED", "REJECTED", "UNREVIEWED", '"decision"'):
            assert token not in source, (
                f"{token!r} appears in the lifecycle store — the review decision "
                "and the lifecycle are orthogonal axes and must not be collapsed"
            )
