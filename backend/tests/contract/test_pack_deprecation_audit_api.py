"""2.0-C4 T5 (AT-846) — the deprecation lifecycle audit over HTTP, end to end.

Parent-story criterion this discharges:

  * **AC4** — all three transitions are audit events.

This is the suite that proves AC4 as a whole rather than one transition at a time:
it drives a pack through its entire lifecycle over the real API — deprecated,
migrated, retired — and then asserts that all three landed in the audit log and come
back together on the trail, in order, attributed, and org-scoped.

The mechanism is pinned DB-free in ``tests/unit/test_pack_deprecation_audit.py``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pack_deprecation_audit import (
    DEPRECATION_AUDIT_EVENT_TYPES,
    InMemoryAnnouncementLedger,
    TRANSITION_DEPRECATED,
    TRANSITION_MIGRATED,
    TRANSITION_RETIRED,
    set_announcement_ledger,
)
from app.pack_state import InMemoryPackStateStore, set_pack_state_store
from app.rbac import seed_owner, seed_static_token_members
from discovery.packs import pack_config
from discovery.packs.pack_config import DEPRECATION_KEY
from discovery.packs.pack_deprecation import STATUS_DEPRECATED

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PACK = "cloud_ops"
RUNNABLE = "service_cloud"
REPLACEMENT = "enterprise_ops"

DEPRECATED_ON = "2026-07-01"
OPEN_UNTIL = "2099-09-29"
EXPIRED_ON = "2026-07-31"

_CURRENT_ORG: Dict[str, Any] = {"id": None}


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_stores():
    set_pack_state_store(InMemoryPackStateStore())
    set_announcement_ledger(InMemoryAnnouncementLedger())
    yield
    set_pack_state_store(None)
    set_announcement_ledger(None)


@pytest.fixture
def isolated_org() -> Iterator[str]:
    previous = _CURRENT_ORG["id"]
    org_id = f"pack_audit_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    seed_static_token_members(org_id)
    _CURRENT_ORG["id"] = org_id
    try:
        yield org_id
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture
def deprecate(monkeypatch):
    def _deprecate(*, grace_ends_on: str = OPEN_UNTIL, replacement: str = REPLACEMENT):
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK],
            DEPRECATION_KEY,
            {
                "status": STATUS_DEPRECATED,
                "reason": "Superseded by the Enterprise Operations pack.",
                "deprecatedOn": DEPRECATED_ON,
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


def _auth(token: str = OWNER_TOKEN, org_id: str | None = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
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


def _save_config(client: TestClient, org_id: str) -> None:
    response = client.post(
        f"/api/stack-builder/setup-state/{org_id}",
        headers=_auth(),
        json={
            "state": {
                "packId": PACK,
                "packIds": [PACK],
                "selectedSystemIds": ["servicenow"],
            },
            "saved_at": "2026-08-06T00:00:00Z",
        },
    )
    assert response.status_code == 204


def _trail(client: TestClient, **params) -> Dict[str, Any]:
    response = client.get(
        "/api/packs/deprecation/audit", headers=_auth(), params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── AC4, whole ────────────────────────────────────────────────────────────────


def test_all_three_transitions_are_audit_events(client, isolated_org, deprecate):
    """The criterion itself: drive one pack through its entire lifecycle and check
    every transition left a record."""
    # 1. Deprecated — the org comes under the terms when its selection is resolved.
    deprecate(grace_ends_on=OPEN_UNTIL)
    _launch(client, [RUNNABLE, PACK])

    # 2. Migrated — the org's saved configuration moves to the replacement.
    _save_config(client, isolated_org)
    applied = client.post(
        f"/api/packs/{PACK}/migration/apply", headers=_auth(), json={"confirm": True}
    )
    assert applied.status_code == 200, applied.text

    # 3. Retired — the grace period ends.
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    entries = client.get("/api/audit-log", headers=_auth()).json()
    emitted = {entry.get("event_type") for entry in entries}

    assert "pack_deprecation_announced" in emitted
    assert "pack_migration_applied" in emitted
    assert "pack_deprecation_disabled" in emitted


def test_the_trail_returns_the_three_transitions_together(
    client, isolated_org, deprecate
):
    deprecate(grace_ends_on=OPEN_UNTIL)
    _launch(client, [RUNNABLE, PACK])
    _save_config(client, isolated_org)
    client.post(
        f"/api/packs/{PACK}/migration/apply", headers=_auth(), json={"confirm": True}
    )
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    body = _trail(client)

    transitions = {entry["transition"] for entry in body["entries"]}
    assert transitions == {
        TRANSITION_DEPRECATED,
        TRANSITION_MIGRATED,
        TRANSITION_RETIRED,
    }
    assert set(body["eventTypes"]) == set(DEPRECATION_AUDIT_EVENT_TYPES)
    # Each row names WHICH transition it is, so a reader does not have to know that
    # applied/reverted are two halves of one thing.
    assert all(entry["transition"] for entry in body["entries"])


def test_the_trail_is_newest_first(client, isolated_org, deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    body = _trail(client)
    stamps = [entry["at"] for entry in body["entries"]]

    assert stamps == sorted(stamps, reverse=True)
    # Told, then retired — so newest-first puts the retirement on top.
    assert body["entries"][0]["transition"] == TRANSITION_RETIRED


def test_the_trail_can_be_narrowed_to_one_pack(client, isolated_org, deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    body = _trail(client, packId=PACK)
    assert body["entries"]
    assert all(entry["packId"] == PACK for entry in body["entries"])

    empty = _trail(client, packId=RUNNABLE)
    assert empty["entries"] == []


def test_a_clean_org_has_an_empty_trail(client, isolated_org):
    body = _trail(client)
    assert body["entries"] == []
    # …but the vocabulary is still reported, so a UI can render the legend.
    assert set(body["eventTypes"]) == set(DEPRECATION_AUDIT_EVENT_TYPES)


# ── The announcement, specifically ────────────────────────────────────────────


def test_a_repeat_launch_does_not_re_announce(client, isolated_org, deprecate):
    deprecate(grace_ends_on=OPEN_UNTIL)
    _launch(client, [RUNNABLE, PACK])
    _launch(client, [RUNNABLE, PACK])
    _launch(client, [RUNNABLE, PACK])

    body = _trail(client)
    announced = [
        entry
        for entry in body["entries"]
        if entry["eventType"] == "pack_deprecation_announced"
    ]
    assert len(announced) == 1


def test_changed_terms_announce_again(client, isolated_org, deprecate):
    deprecate(grace_ends_on="2099-01-01")
    _launch(client, [RUNNABLE, PACK])

    deprecate(grace_ends_on="2099-12-31")
    _launch(client, [RUNNABLE, PACK])

    announced = [
        entry
        for entry in _trail(client)["entries"]
        if entry["eventType"] == "pack_deprecation_announced"
    ]
    assert len(announced) == 2


def test_a_launch_with_no_deprecated_pack_announces_nothing(client, isolated_org):
    _launch(client, [RUNNABLE])
    assert _trail(client)["entries"] == []


# ── Access ────────────────────────────────────────────────────────────────────


def test_the_trail_is_owner_only(client, isolated_org, deprecate):
    """The same bar as /api/audit-log, whose rows these are. Serving them to a lower
    role through a narrower query would be a privilege bypass."""
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])

    assert (
        client.get(
            "/api/packs/deprecation/audit", headers=_auth(ANALYST_TOKEN)
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/packs/deprecation/audit", headers=_auth(VIEWER_TOKEN)
        ).status_code
        == 403
    )


def test_the_trail_is_org_scoped(client, isolated_org, deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)
    _launch(client, [RUNNABLE, PACK])
    assert _trail(client)["entries"]

    other_org = f"pack_audit_other_{uuid4().hex[:8]}"
    seed_owner(other_org, OWNER_TOKEN)
    response = client.get(
        "/api/packs/deprecation/audit", headers=_auth(org_id=other_org)
    )
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == []
