"""Org scoping on ``GET /api/runs/{runId}/cloud-ops/event-signatures``.

The rows this route serves are a tenant's operational estate — resource ids,
event classes, incident ids. The guard that keeps them tenant-private is the run
record's org, so the interesting cases are the ones where that org does not match
or is not there at all.

A run with NO recorded org used to pass the guard: `if run_org and run_org != …`
is False when `run_org` is None, so any authenticated analyst in any org could
read it. Ownership that cannot be established is not ownership — a missing org is
now a 404, the same answer as another tenant's run, so the response does not
confirm the run exists either.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.rbac import seed_owner

TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
KV_KEY = "cloud_ops_event_signatures"

_ROWS = [
    {
        "signature": "1:" + "a" * 32,
        "event_count": 4,
        "recurring": True,
        "resource_id": "/subscriptions/x/resourceGroups/rg/providers/app",
        "resource_type": "compute",
        "event_class": "error",
    }
]


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def org() -> str:
    org_id = f"cloud_ops_sig_{uuid4().hex[:8]}"
    seed_owner(org_id, TOKEN)
    return org_id


def auth(org_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Org-Id": org_id}


def stub_run(monkeypatch, run: Optional[Dict[str, Any]], *, rows=_ROWS) -> str:
    """Serve one run record and its stored rows, without writing a real run."""
    run_id = f"run-{uuid4().hex[:8]}"
    monkeypatch.setattr(db, "run_get", lambda rid: run if rid == run_id else None)
    monkeypatch.setattr(
        db,
        "run_kv_get",
        lambda key, rid, default=None: (
            {"capturedAt": "2026-08-01T00:00:00Z", "rows": rows}
            if key == KV_KEY and rid == run_id
            else default
        ),
    )
    return run_id


def get(client: TestClient, org_id: str, run_id: str):
    return client.get(
        f"/api/runs/{run_id}/cloud-ops/event-signatures", headers=auth(org_id)
    )


def test_the_owning_org_reads_its_rows(client, org, monkeypatch):
    run_id = stub_run(monkeypatch, {"id": "r", "orgId": org})
    response = get(client, org, run_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["count"] == 1
    assert body["signatures"] == [_ROWS[0]["signature"]]


def test_another_orgs_run_is_not_found(client, org, monkeypatch):
    run_id = stub_run(monkeypatch, {"id": "r", "orgId": "some-other-org"})
    assert get(client, org, run_id).status_code == 404


def test_a_run_with_no_recorded_org_is_not_found(client, org, monkeypatch):
    """The review finding: a legacy run missing orgId/org_id must not fall through
    the guard just because there is nothing to compare against."""
    run_id = stub_run(monkeypatch, {"id": "r"})
    assert get(client, org, run_id).status_code == 404


@pytest.mark.parametrize("empty", ["", None])
def test_a_blank_org_is_treated_the_same_as_a_missing_one(client, org, monkeypatch, empty):
    run_id = stub_run(monkeypatch, {"id": "r", "orgId": empty, "org_id": empty})
    assert get(client, org, run_id).status_code == 404


def test_a_missing_run_is_not_found(client, org, monkeypatch):
    stub_run(monkeypatch, None)
    assert get(client, org, "run-does-not-exist").status_code == 404


def test_an_owned_run_with_nothing_recorded_reports_unavailable(client, org, monkeypatch):
    """Still 200 — the run is this org's, it simply recorded no rows. `available`
    is what distinguishes that from a fabricated empty set."""
    run_id = stub_run(monkeypatch, {"id": "r", "org_id": org}, rows=None)
    monkeypatch.setattr(db, "run_kv_get", lambda key, rid, default=None: default)

    response = get(client, org, run_id)
    assert response.status_code == 200, response.text
    assert response.json()["available"] is False
    assert response.json()["count"] == 0
