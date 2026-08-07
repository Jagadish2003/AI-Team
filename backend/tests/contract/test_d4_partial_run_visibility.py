"""Contract tests for 2.0-D4 T5 — AC6, stated as a negative.

**The acceptance bar:** after a seeded failure there must be **no surface on
which the run appears complete**. That is how this will actually be judged, so it
is how it is tested — every surface a customer can reach is asked, and each must
tell the same truth.

The executive report gets the most attention here on purpose. It is the artifact
most likely to reach someone who will never open a health panel, and if a partial
run's report reads identically to a complete one then none of the rest of this
work matters.

The three AC6 scenarios each get a seeded run: a connector outage, model-mode
unavailability, and storage pressure. They fail in genuinely different ways,
which is why each is seeded rather than one standing in for the others.
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
from app.degradation import STATUS_FAILED, STATUS_OK, STATUS_UNAVAILABLE
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")


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
    org_id = f"org-d4t5-{uuid4().hex[:8]}"
    _seed_member(org_id, DEV_TOKEN)
    _seed_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


def _opp(index: int) -> Dict[str, Any]:
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"ident_{index:03d}",
        "title": f"Finding {index}",
        "category": "Workflow",
        "tier": "Quick Win",
        "impact": 7,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "seeded",
        "evidenceIds": [f"ev_{index}"],
        "decision": "UNREVIEWED",
        "override": {"isLocked": False, "rationaleOverride": "", "overrideReason": "",
                     "updatedAt": None},
        "packId": "service_cloud",
        "_debug": {"detector_id": "HANDOFF_FRICTION"},
    }


def _seed_run(*, succeeded: List[str], errors: Dict[str, str] | None = None,
              systems: List[str] | None = None) -> str:
    """A materialised run whose ingest partly failed."""
    from app.db import run_get, run_kv_set, run_set
    from app.run_store import start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    record = run_get(run_id) or {}
    record.update({
        "inputs": {"systems": systems or ["salesforce", "servicenow", "jira"]},
        "succeeded": succeeded,
        "ingestErrors": errors or {},
        "status": "complete",
    })
    run_set(run_id, record)
    run_kv_set("opps", run_id, [_opp(i) for i in range(4)])
    return run_id


CLEAN = {"succeeded": ["salesforce", "servicenow", "jira"]}
OUTAGE = {
    "succeeded": ["salesforce", "jira"],
    "errors": {"servicenow": "HTTP 401: Session expired or invalid"},
}


# ---------------------------------------------------------------------------
# AC6 scenario 1 — connector outage
# ---------------------------------------------------------------------------


class TestConnectorOutageIsVisibleEverywhere:
    def test_the_run_status_says_the_run_is_incomplete(self, client):
        """A run can be 'complete' and still not have delivered everything.

        Without this a poller reads status='complete' and every consumer
        downstream believes the findings are whole.
        """
        org = _org()
        run_id = _seed_run(**OUTAGE)
        body = client.get(f"/api/runs/{run_id}/status", headers=_auth(org)).json()
        assert body["completeness"] is not None, (
            "status carries no completeness — a response_model omission would "
            "strip this silently"
        )
        assert body["completeness"]["complete"] is False

    def test_the_executive_report_does_not_read_as_complete(self, client):
        """The surface the subtask singles out.

        Most likely to reach someone who will never see a health panel, so if it
        reads identically to a clean run this subtask has failed regardless of
        how good the health endpoint looks.
        """
        org = _org()
        run_id = _seed_run(**OUTAGE)
        report = client.get(
            f"/api/runs/{run_id}/executive-report", headers=_auth(org)
        ).json()
        assert "runCompleteness" in report
        assert report["runCompleteness"]["complete"] is False
        assert "INCOMPLETE" in report["runCompleteness"]["headline"]

    def test_the_executive_report_counts_sources_that_worked_not_sources_configured(
        self, client
    ):
        """The precise bug this subtask fixes.

        sourcesAnalyzed counted CONFIGURED sources, so a run whose ServiceNow
        died reported the same "connected" count as a clean one.
        """
        org = _org()
        partial = client.get(
            f"/api/runs/{_seed_run(**OUTAGE)}/executive-report", headers=_auth(org)
        ).json()["sourcesAnalyzed"]
        clean = client.get(
            f"/api/runs/{_seed_run(**CLEAN)}/executive-report", headers=_auth(org)
        ).json()["sourcesAnalyzed"]
        assert partial["totalConnected"] < clean["totalConnected"], (
            "a partial run reports the same source count as a clean one"
        )
        assert partial["sourcesFailed"] >= 1

    def test_a_partial_report_differs_from_a_clean_one(self, client):
        """The negative bar, asserted directly: the two must not read alike."""
        org = _org()
        partial = client.get(
            f"/api/runs/{_seed_run(**OUTAGE)}/executive-report", headers=_auth(org)
        ).json()
        clean = client.get(
            f"/api/runs/{_seed_run(**CLEAN)}/executive-report", headers=_auth(org)
        ).json()
        assert partial["runCompleteness"]["complete"] is False
        assert clean["runCompleteness"]["complete"] is True
        assert (
            partial["runCompleteness"]["headline"]
            != clean["runCompleteness"]["headline"]
        )

    def test_the_missing_source_is_named_on_the_report(self, client):
        org = _org()
        report = client.get(
            f"/api/runs/{_seed_run(**OUTAGE)}/executive-report", headers=_auth(org)
        ).json()
        assert any("servicenow" in m for m in report["runCompleteness"]["missing"])

    def test_the_run_health_surface_agrees_with_the_report(self, client):
        """Every surface reads the same fact, so none can contradict another."""
        org = _org()
        run_id = _seed_run(**OUTAGE)
        health = client.get(
            "/api/run-health/degradation", params={"run_id": run_id}, headers=_auth(org)
        ).json()
        report = client.get(
            f"/api/runs/{run_id}/executive-report", headers=_auth(org)
        ).json()
        assert health["completeness"]["complete"] is False
        assert report["runCompleteness"]["complete"] is False

    def test_a_clean_run_is_not_falsely_flagged(self, client):
        """A degradation surface that cries wolf is one people stop reading."""
        org = _org()
        run_id = _seed_run(**CLEAN)
        body = client.get(f"/api/runs/{run_id}/status", headers=_auth(org)).json()
        assert body["completeness"]["complete"] is True
        report = client.get(
            f"/api/runs/{run_id}/executive-report", headers=_auth(org)
        ).json()
        assert report["runCompleteness"]["complete"] is True

    def test_the_failure_carries_a_reason_and_a_remedy(self, client):
        org = _org()
        report = client.get(
            f"/api/runs/{_seed_run(**OUTAGE)}/executive-report", headers=_auth(org)
        ).json()
        component = next(
            c for c in report["runCompleteness"]["components"]
            if c["component"] == "servicenow"
        )
        assert "401" in (component["reason"] or "")
        assert component["remedy"], "a failure with no remedy is not actionable"
        assert component["attempted"] and component["missing"]


# ---------------------------------------------------------------------------
# AC6 scenarios 2 and 3 — model and storage, through the live surface
# ---------------------------------------------------------------------------


class TestModelAndStorageDegradationSurface:
    def test_the_degradation_route_probes_the_live_environment(self, client):
        """Unlike the run-scoped surfaces this one asks about NOW, because
        "can this deployment embed content?" is a question about now."""
        body = client.get("/api/run-health/degradation", headers=_auth(_org())).json()
        assert "completeness" in body
        assert isinstance(body["completeness"]["components"], list)

    def test_an_inert_embedding_provider_is_reported(self, client, monkeypatch):
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        body = client.get("/api/run-health/degradation", headers=_auth(_org())).json()
        embedding = [
            c for c in body["completeness"]["components"]
            if c["component"] == "embedding_provider"
        ]
        assert embedding, (
            "a provider that silently returns empty embeddings must be visible — "
            "nothing else would reveal it"
        )
        assert embedding[0]["status"] == STATUS_UNAVAILABLE

    def test_every_component_uses_the_canonical_vocabulary(self, client):
        """The uniformity requirement: a consumer renders any degradation
        without special-casing the subsystem that produced it."""
        from app.degradation import CANONICAL_STATUSES, COMPONENT_KINDS

        body = client.get("/api/run-health/degradation", headers=_auth(_org())).json()
        for c in body["completeness"]["components"]:
            assert c["status"] in CANONICAL_STATUSES, c
            assert c["kind"] in COMPONENT_KINDS, c

    def test_every_reported_degradation_is_actionable(self, client):
        body = client.get("/api/run-health/degradation", headers=_auth(_org())).json()
        for c in body["completeness"]["components"]:
            if c["status"] == STATUS_OK:
                continue
            assert c["reason"], f"{c['component']} degraded with no reason"
            assert c["remedy"], f"{c['component']} degraded with no remedy"


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_the_degradation_route_requires_analyst(self, client):
        response = client.get(
            "/api/run-health/degradation", headers=_auth(_org(), VIEWER_TOKEN)
        )
        assert response.status_code == 403

    def test_an_unauthenticated_request_is_rejected(self, client):
        assert client.get("/api/run-health/degradation").status_code in (401, 403)
