"""2.0-C4 T4 (AT-845) — grace behaviour over the HTTP surface, end to end.

Parent-story criteria exercised here:

  * **AC3** — the pack runs normally during grace and moves to safe-disabled after
    it, with history intact.
  * **AC4** (this transition's share) — the safe-disable is an audit event.

The mechanism is pinned DB-free in ``tests/unit/test_pack_grace_behaviour.py``. What
this suite adds is what only exists at the edge: that a launch actually includes a
pack in grace and excludes an expired one, that the exclusion reaches the run record
and run health with the RIGHT reason, that the audit entry is written, and — the
half AC3 cares about most — that a run performed while the pack was still supported
is completely untouched by its later retirement.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.pack_grace import EXCLUSION_REASON_GRACE_EXPIRED, SYSTEM_ACTOR
from app.pack_state import (
    InMemoryPackStateStore,
    STATE_ACTIVE,
    STATE_DISABLED,
    set_pack_state_store,
)
from app.rbac import seed_owner
from discovery.packs import pack_config
from discovery.packs.pack_config import DEPRECATION_KEY
from discovery.packs.pack_deprecation import STATUS_DEPRECATED

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")

PACK = "cloud_ops"
RUNNABLE = "service_cloud"
REPLACEMENT = "enterprise_ops"

EXPIRED_ON = "2026-07-31"
OPEN_UNTIL = "2099-09-29"

_CURRENT_ORG: Dict[str, Any] = {"id": None}


@pytest.fixture(autouse=True)
def _in_memory_pack_state():
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture
def isolated_org() -> Iterator[str]:
    """A throwaway org. Required by anything reading `/api/run-health/packs`, which
    resolves the newest run for the org across the whole contract database."""
    previous = _CURRENT_ORG["id"]
    org_id = f"pack_grace_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    _CURRENT_ORG["id"] = org_id
    try:
        yield org_id
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture
def deprecate(monkeypatch):
    def _deprecate(*, grace_ends_on: str, replacement: str = REPLACEMENT):
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK],
            DEPRECATION_KEY,
            {
                "status": STATUS_DEPRECATED,
                "reason": "Superseded by the Enterprise Operations pack.",
                "deprecatedOn": "2026-07-01",
                "graceEndsOn": grace_ends_on,
                "replacement": {"packId": replacement} if replacement else {},
            },
        )
        return PACK

    return _deprecate


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(org_id: str | None = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    org = org_id or _CURRENT_ORG["id"]
    if org is not None:
        headers["X-Org-Id"] = org
    return headers


def _launch(client: TestClient, pack_ids):
    return client.post(
        "/api/stack-builder/launch",
        json={
            "org_id": _CURRENT_ORG["id"] or "default",
            "selected_system_ids": ["salesforce", "servicenow"],
            "weightings": {},
            "pack_ids": list(pack_ids),
        },
        headers=_auth(),
    )


# ── During grace, the pack runs normally ──────────────────────────────────────


def test_a_launch_during_grace_includes_the_pack(client, isolated_org, deprecate):
    deprecate(grace_ends_on=OPEN_UNTIL)

    response = _launch(client, [RUNNABLE, PACK])

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["packIds"]) == {RUNNABLE, PACK}
    assert body["excludedPacks"] == []


def test_a_launch_during_grace_leaves_the_pack_state_alone(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=OPEN_UNTIL)
    _launch(client, [RUNNABLE, PACK])

    rows = {
        row["packId"]: row
        for row in client.get("/api/packs/state", headers=_auth()).json()["packs"]
    }
    assert rows[PACK]["state"] == STATE_ACTIVE
    # …and the notice is still showing, which is the whole point of the grace.
    assert rows[PACK]["deprecation"]["phase"] == "grace"


# ── After grace, it moves to safe-disabled ────────────────────────────────────


def test_a_launch_after_grace_excludes_the_pack_and_names_the_reason(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=EXPIRED_ON)

    response = _launch(client, [RUNNABLE, PACK])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["packIds"] == [RUNNABLE]
    assert [row["packId"] for row in body["excludedPacks"]] == [PACK]
    # NOT "pack_disabled": the operator cannot re-enable their way out of this one.
    assert body["excludedPacks"][0]["reason"] == EXCLUSION_REASON_GRACE_EXPIRED


def test_the_pack_is_moved_to_disabled_through_c1s_state(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    rows = {
        row["packId"]: row
        for row in client.get("/api/packs/state", headers=_auth()).json()["packs"]
    }
    assert rows[PACK]["state"] == STATE_DISABLED
    assert EXPIRED_ON in (rows[PACK]["reason"] or "")
    assert rows[RUNNABLE]["state"] == STATE_ACTIVE


def test_the_retirement_is_on_the_append_only_transition_history(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    response = client.get(f"/api/packs/{PACK}/state/history", headers=_auth())

    assert response.status_code == 200, response.text
    transitions = response.json()["transitions"]
    assert [t["transition"] for t in transitions] == ["disable"]
    assert transitions[0]["actor_id"] == SYSTEM_ACTOR


def test_the_run_record_reports_the_exclusion(client, isolated_org, deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)
    run_id = _launch(client, [RUNNABLE, PACK]).json()["runId"]

    run = db.run_get(run_id)

    assert run["packIds"] == [RUNNABLE]
    assert run["excludedPacks"][0]["reason"] == EXCLUSION_REASON_GRACE_EXPIRED
    assert PACK not in run["packCompatibility"]
    assert db.run_kv_get("pack_ids", run_id, []) == [RUNNABLE]


def test_run_health_reports_the_exclusion_with_its_reason(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    body = client.get("/api/run-health/packs", headers=_auth()).json()

    excluded = {row["packId"]: row for row in body["excluded_packs"]}
    assert PACK in excluded
    assert excluded[PACK]["reason"] == EXCLUSION_REASON_GRACE_EXPIRED


def test_launching_only_an_expired_pack_is_refused_with_the_right_remedy(
    client, isolated_org, deprecate
):
    """A run with zero packs would report success having produced nothing. The
    refusal must not send the operator to the re-enable button."""
    deprecate(grace_ends_on=EXPIRED_ON)

    response = _launch(client, [PACK])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert PACK in detail
    assert "cannot be re-enabled" in detail


def test_re_enabling_does_not_bring_a_retired_pack_back(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    reenabled = client.put(
        f"/api/packs/{PACK}/state", json={"state": STATE_ACTIVE}, headers=_auth()
    )
    assert reenabled.status_code == 200, reenabled.text

    body = _launch(client, [RUNNABLE, PACK]).json()

    assert body["packIds"] == [RUNNABLE]
    assert [row["packId"] for row in body["excludedPacks"]] == [PACK]


# ── History intact (AC3's second half) ────────────────────────────────────────


def test_a_run_made_during_grace_survives_the_later_retirement(
    client, isolated_org, deprecate
):
    """The point of safe-disable rather than deletion: what the pack already
    produced is untouched when it is retired."""
    deprecate(grace_ends_on=OPEN_UNTIL)
    during_grace = _launch(client, [RUNNABLE, PACK]).json()["runId"]
    before = db.run_get(during_grace)
    assert PACK in before["packIds"]

    # The grace period ends…
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    after = db.run_get(during_grace)
    assert after["packIds"] == before["packIds"]
    assert after["packVersions"] == before["packVersions"]
    assert after["packCompatibility"] == before["packCompatibility"]
    # And the historical run is still served.
    served = client.get(f"/api/runs/{during_grace}", headers=_auth())
    assert served.status_code == 200, served.text


def test_the_retired_packs_earlier_run_is_still_listed(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=OPEN_UNTIL)
    during_grace = _launch(client, [RUNNABLE, PACK]).json()["runId"]

    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    runs = client.get("/api/runs", headers=_auth()).json()
    ids = {row.get("id") for row in (runs if isinstance(runs, list) else runs.get("runs", []))}
    assert during_grace in ids


# ── Auditability (parent-story AC4, this transition's share) ──────────────────


def test_the_retirement_reaches_the_audit_log(client, isolated_org, deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    entries = client.get("/api/audit-log", headers=_auth()).json()
    retirements = [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_deprecation_disabled"
    ]

    assert retirements, "no pack_deprecation_disabled audit entry was written"
    payload = retirements[0].get("payload") or {}
    assert payload.get("pack_id") == PACK
    assert payload.get("grace_ends_on") == EXPIRED_ON
    assert payload.get("replacement_pack_id") == REPLACEMENT


def test_a_launch_during_grace_writes_no_retirement_audit_entry(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=OPEN_UNTIL)
    _launch(client, [RUNNABLE, PACK])

    entries = client.get("/api/audit-log", headers=_auth()).json()
    assert not [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_deprecation_disabled"
    ]
