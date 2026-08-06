"""Contract tests for 2.0-D4 T4 — the envelope reaches a customer-facing surface.

The subtask is explicit that this is half the deliverable: *"A budget that is
reported into a JSON blob nobody renders satisfies the letter of 'loud' and none
of its intent."* MSP-B7 has recorded budgets and deferrals on the run record
since 1.9; until now nothing served them.

The envelope arithmetic and the at/past-limit behaviour are load-tested in
``tests/unit/test_scale_envelope_load.py`` where they need no database. What is
tested here is what only a live stack shows: the route exists, is access
controlled, serves the envelope with its honesty labels intact, and reports a
real run against it.
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.scale_envelope import (
    DIM_DOCUMENTS_PER_RUN,
    DIM_EVENTS_PER_RUN,
    DIM_FINDINGS_PER_RUN,
    DIM_SYSTEMS_PER_DEPLOYMENT,
)

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/run-health/volume"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_member(org_id: str, user_id: str, role: str = "owner") -> None:
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
    org_id = f"org-d4t4-{uuid4().hex[:8]}"
    _seed_member(org_id, DEV_TOKEN)
    _seed_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


# ---------------------------------------------------------------------------
# The envelope is served, with its honesty intact
# ---------------------------------------------------------------------------


class TestTheEnvelopeIsServed:
    def test_the_route_returns_all_four_dimensions(self, client):
        body = client.get(BASE, headers=_auth(_org())).json()
        dims = body["envelope"]["dimensions"]
        assert set(dims) == {
            DIM_EVENTS_PER_RUN,
            DIM_DOCUMENTS_PER_RUN,
            DIM_SYSTEMS_PER_DEPLOYMENT,
            DIM_FINDINGS_PER_RUN,
        }

    def test_every_served_dimension_declares_its_basis(self, client):
        """The point of the whole exercise: a reader can tell a measured number
        from a first guess without opening any code."""
        dims = client.get(BASE, headers=_auth(_org())).json()["envelope"]["dimensions"]
        for key, d in dims.items():
            assert d["basis"] in ("measured", "operationally_justified", "provisional"), key
            assert d["derivation"], f"{key} states no derivation"

    def test_the_summary_says_how_many_numbers_are_actually_measured(self, client):
        env = client.get(BASE, headers=_auth(_org())).json()["envelope"]
        assert env["measuredCount"] == 1
        assert env["totalCount"] == 4
        assert "reproducible measurement" in env["honestyNote"]

    def test_declared_gaps_are_served_rather_than_hidden(self, client):
        """A dimension with no enforcement must say so on the customer surface,
        not only in a code comment."""
        env = client.get(BASE, headers=_auth(_org())).json()["envelope"]
        assert DIM_FINDINGS_PER_RUN in env["declaredGaps"]
        assert env["dimensions"][DIM_FINDINGS_PER_RUN]["isEnforced"] is False

    def test_every_dimension_states_what_happens_at_its_edge(self, client):
        dims = client.get(BASE, headers=_auth(_org())).json()["envelope"]["dimensions"]
        for key, d in dims.items():
            assert d["degradation"] in (
                "defer_and_count", "refuse_with_reason", "report_only"
            ), key
            assert d["degradationDetail"], key

    def test_the_degradation_rule_is_stated_once_for_the_whole_envelope(self, client):
        env = client.get(BASE, headers=_auth(_org())).json()["envelope"]
        assert "silently" in env["degradationRule"]


# ---------------------------------------------------------------------------
# A real run is reported against it
# ---------------------------------------------------------------------------


class TestARunIsReportedAgainstTheEnvelope:
    def test_a_named_run_produces_a_volume_report(self, client):
        from app.db import run_kv_set
        from app.run_store import start_run_

        org = _org()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        body = client.get(
            BASE, params={"run_id": run_id}, headers=_auth(org)
        ).json()
        assert body["run"] is not None
        assert body["run"]["runId"] == run_id
        assert len(body["run"]["dimensions"]) == 4

    def test_the_report_carries_a_plain_headline(self, client):
        from app.run_store import start_run_

        org = _org()
        run_id = start_run_({"pack": "service_cloud"})["runId"]
        report = client.get(BASE, params={"run_id": run_id}, headers=_auth(org)).json()["run"]
        assert report["headline"]

    def test_the_envelope_is_served_even_when_no_run_is_named(self, client):
        """The envelope is useful on its own — a customer asking "what volumes do
        you support?" should not need a run id."""
        body = client.get(BASE, headers=_auth(_org())).json()
        assert body["envelope"]["dimensions"]

    def test_an_unknown_run_does_not_break_the_envelope(self, client):
        body = client.get(BASE, params={"run_id": "run_nope"}, headers=_auth(_org())).json()
        assert body["envelope"]["dimensions"], "the envelope must still be served"

    def test_the_response_is_json_serialisable_end_to_end(self, client):
        import json

        body = client.get(BASE, headers=_auth(_org())).json()
        assert json.loads(json.dumps(body)) == body


# ---------------------------------------------------------------------------
# Access control and isolation
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_the_route_requires_analyst(self, client):
        assert client.get(BASE, headers=_auth(_org(), VIEWER_TOKEN)).status_code == 403

    def test_an_unauthenticated_request_is_rejected(self, client):
        assert client.get(BASE).status_code in (401, 403)

    def test_the_response_is_scoped_to_the_calling_org(self, client):
        org = _org()
        assert client.get(BASE, headers=_auth(org)).json()["org_id"] == org
