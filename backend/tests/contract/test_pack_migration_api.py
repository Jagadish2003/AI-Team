"""2.0-C4 T3 (AT-844) — the org-config pack migration over HTTP.

Parent-story criteria exercised here:

  * **AC2** — migration previews the config change, applies it on confirmation, and
    is reversible.
  * **AC4** (this transition's share) — apply and revert are audit events.

The domain rules are pinned DB-free in ``tests/unit/test_pack_migration.py``. What
this suite adds is everything that only exists at the edge: the role boundary
(preview is analyst+, apply and revert are Owner), org isolation, the confirmation
requirement, the status codes a UI branches on, and the audit entries — plus the
end-to-end property that the migrated configuration is what the Stack Builder
setup-state endpoint actually serves back.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pack_state import InMemoryPackStateStore, set_pack_state_store
from app.rbac import seed_owner, seed_static_token_members
from discovery.packs import pack_config
from discovery.packs.pack_config import DEPRECATION_KEY
from discovery.packs.pack_deprecation import STATUS_DEPRECATED

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PACK = "cloud_ops"
REPLACEMENT = "enterprise_ops"

_CURRENT_ORG: Dict[str, Any] = {"id": None}


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_pack_state():
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture
def isolated_org() -> Iterator[str]:
    previous = _CURRENT_ORG["id"]
    org_id = f"pack_mig_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    # The static analyst/viewer tokens need a membership row in THIS org, or
    # require_role 403s them before the route's own role check is exercised — which
    # would make every role assertion below pass for the wrong reason.
    seed_static_token_members(org_id)
    _CURRENT_ORG["id"] = org_id
    try:
        yield org_id
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture
def deprecated_pack(monkeypatch):
    """Deprecate a real registered pack for the duration of one test."""
    monkeypatch.setitem(
        pack_config.PACK_REGISTRY[PACK],
        DEPRECATION_KEY,
        {
            "status": STATUS_DEPRECATED,
            "reason": "Superseded by the Enterprise Operations pack.",
            "deprecatedOn": "2026-07-01",
            "graceEndsOn": "2099-09-29",
            "replacement": {"packId": REPLACEMENT},
        },
    )
    return PACK


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


def _save_config(client: TestClient, org_id: str, **overrides: Any) -> None:
    """Persist a Stack Builder setup state for the org — the migrated surface."""
    state: Dict[str, Any] = {
        "packId": PACK,
        "packIds": [PACK],
        "templateId": None,
        "templateIds": [],
        "selectedSystemIds": ["servicenow", "jira"],
        "currentStep": 4,
    }
    state.update(overrides)
    response = client.post(
        f"/api/stack-builder/setup-state/{org_id}",
        headers=_auth(),
        json={"state": state, "saved_at": "2026-08-06T00:00:00Z"},
    )
    assert response.status_code == 204


def _load_config(client: TestClient, org_id: str) -> Dict[str, Any]:
    response = client.get(
        f"/api/stack-builder/setup-state/{org_id}", headers=_auth()
    )
    assert response.status_code == 200
    return response.json()["state"]


def _preview(client: TestClient, pack_id: str = PACK, **kwargs) -> Dict[str, Any]:
    response = client.get(
        f"/api/packs/{pack_id}/migration/preview", headers=_auth(**kwargs)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, **body: Any):
    payload = {"confirm": True}
    payload.update(body)
    return client.post(
        f"/api/packs/{PACK}/migration/apply", headers=_auth(), json=payload
    )


# ── Preview ───────────────────────────────────────────────────────────────────


def test_preview_reports_the_change_set_without_applying_it(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org, packIds=[PACK, "service_cloud"])

    plan = _preview(client)

    assert plan["available"] is True
    assert plan["applicable"] is True
    assert plan["replacementPackId"] == REPLACEMENT
    fields = {change["field"]: change for change in plan["changes"]}
    assert fields["packId"]["newValue"] == REPLACEMENT
    assert fields["packIds"]["newValue"] == [REPLACEMENT, "service_cloud"]
    assert plan["fingerprint"]
    # Nothing was written.
    assert _load_config(client, isolated_org)["packId"] == PACK


def test_preview_carries_the_same_notice_the_pack_picker_shows(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org)
    plan = _preview(client)

    states = client.get("/api/packs/state", headers=_auth()).json()["packs"]
    picker = next(row for row in states if row["packId"] == PACK)["deprecation"]

    assert plan["deprecation"]["summary"] == picker["summary"]


def test_preview_of_a_healthy_pack_is_an_answer_not_an_error(client, isolated_org):
    plan = _preview(client)
    assert plan["available"] is False
    assert plan["applicable"] is False
    assert plan["reason"]


def test_preview_of_an_unknown_pack_is_404(client, isolated_org):
    response = client.get(
        "/api/packs/not_a_pack/migration/preview", headers=_auth()
    )
    assert response.status_code == 404


def test_preview_is_analyst_plus(client, isolated_org, deprecated_pack):
    """A viewer already learns the pack is going away from the notice; the preview
    quotes back the org's saved configuration, so it sits one level up."""
    _save_config(client, isolated_org)
    assert (
        client.get(
            f"/api/packs/{PACK}/migration/preview",
            headers=_auth(ANALYST_TOKEN),
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/packs/{PACK}/migration/preview",
            headers=_auth(VIEWER_TOKEN),
        ).status_code
        == 403
    )


# ── Apply ─────────────────────────────────────────────────────────────────────


def test_apply_migrates_the_configuration_the_setup_endpoint_serves(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org, packIds=[PACK, "service_cloud"])

    response = _apply(client, reason="ahead of the grace period")

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["changed"] is True
    assert record["kind"] == "apply"

    state = _load_config(client, isolated_org)
    assert state["packId"] == REPLACEMENT
    assert state["packIds"] == [REPLACEMENT, "service_cloud"]
    # The rest of the frontend-owned blob is untouched.
    assert state["selectedSystemIds"] == ["servicenow", "jira"]


def test_apply_requires_explicit_confirmation(client, isolated_org, deprecated_pack):
    _save_config(client, isolated_org)

    response = client.post(
        f"/api/packs/{PACK}/migration/apply", headers=_auth(), json={}
    )

    assert response.status_code == 400
    assert _load_config(client, isolated_org)["packId"] == PACK


def test_apply_refuses_a_stale_fingerprint(client, isolated_org, deprecated_pack):
    """AC2's "previewed before applying" — enforced, not assumed."""
    _save_config(client, isolated_org)
    previewed = _preview(client)["fingerprint"]
    _save_config(client, isolated_org, packIds=[PACK, "service_cloud"])

    response = _apply(client, fingerprint=previewed)

    assert response.status_code == 409
    assert _load_config(client, isolated_org)["packId"] == PACK


def test_apply_with_nothing_to_migrate_is_409(client, isolated_org):
    _save_config(client, isolated_org)
    response = _apply(client)
    assert response.status_code == 409


def test_apply_is_owner_only(client, isolated_org, deprecated_pack):
    _save_config(client, isolated_org)
    response = client.post(
        f"/api/packs/{PACK}/migration/apply",
        headers=_auth(ANALYST_TOKEN),
        json={"confirm": True},
    )
    assert response.status_code == 403
    assert _load_config(client, isolated_org)["packId"] == PACK


def test_apply_does_not_touch_another_org(client, isolated_org, deprecated_pack):
    other_org = f"pack_mig_other_{uuid4().hex[:8]}"
    seed_owner(other_org, OWNER_TOKEN)
    _save_config(client, isolated_org)
    response = client.post(
        f"/api/stack-builder/setup-state/{other_org}",
        headers=_auth(org_id=other_org),
        json={
            "state": {"packId": PACK, "packIds": [PACK]},
            "saved_at": "2026-08-06T00:00:00Z",
        },
    )
    assert response.status_code == 204

    assert _apply(client).status_code == 200

    other = client.get(
        f"/api/stack-builder/setup-state/{other_org}",
        headers=_auth(org_id=other_org),
    ).json()["state"]
    assert other["packId"] == PACK


# ── Revert ────────────────────────────────────────────────────────────────────


def test_revert_restores_the_previous_configuration(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org, packIds=[PACK, "service_cloud"])
    applied = _apply(client).json()

    response = client.post(
        f"/api/packs/migrations/{applied['id']}/revert",
        headers=_auth(),
        json={"reason": "not ready yet"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["revertsMigrationId"] == applied["id"]
    state = _load_config(client, isolated_org)
    assert state["packId"] == PACK
    assert state["packIds"] == [PACK, "service_cloud"]


def test_reverting_twice_is_409(client, isolated_org, deprecated_pack):
    _save_config(client, isolated_org)
    applied = _apply(client).json()
    first = client.post(
        f"/api/packs/migrations/{applied['id']}/revert", headers=_auth(), json={}
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/packs/migrations/{applied['id']}/revert", headers=_auth(), json={}
    )
    assert second.status_code == 409


def test_revert_of_an_unknown_migration_is_404(client, isolated_org):
    response = client.post(
        "/api/packs/migrations/pmig_missing/revert", headers=_auth(), json={}
    )
    assert response.status_code == 404


def test_revert_is_owner_only(client, isolated_org, deprecated_pack):
    _save_config(client, isolated_org)
    applied = _apply(client).json()
    response = client.post(
        f"/api/packs/migrations/{applied['id']}/revert",
        headers=_auth(ANALYST_TOKEN),
        json={},
    )
    assert response.status_code == 403


# ── Ledger ────────────────────────────────────────────────────────────────────


def test_history_keeps_both_rows_newest_first(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org)
    applied = _apply(client).json()
    reverted = client.post(
        f"/api/packs/migrations/{applied['id']}/revert", headers=_auth(), json={}
    ).json()

    body = client.get("/api/packs/migrations", headers=_auth()).json()

    assert [row["id"] for row in body["migrations"]] == [
        reverted["id"], applied["id"]
    ]
    original = body["migrations"][1]
    assert original["reverted"] is True
    assert original["revertedAt"] == reverted["at"]


def test_history_is_org_scoped(client, isolated_org, deprecated_pack):
    _save_config(client, isolated_org)
    _apply(client)

    other_org = f"pack_mig_other_{uuid4().hex[:8]}"
    seed_owner(other_org, OWNER_TOKEN)
    body = client.get(
        "/api/packs/migrations", headers=_auth(org_id=other_org)
    ).json()

    assert body["migrations"] == []


# ── Auditability (parent-story AC4, this transition's share) ──────────────────


def test_apply_and_revert_both_reach_the_audit_log(
    client, isolated_org, deprecated_pack
):
    _save_config(client, isolated_org)
    applied = _apply(client, reason="planned migration").json()
    client.post(
        f"/api/packs/migrations/{applied['id']}/revert", headers=_auth(), json={}
    )

    entries = client.get("/api/audit-log", headers=_auth()).json()
    by_type = {entry.get("event_type"): entry for entry in entries}

    assert "pack_migration_applied" in by_type
    assert "pack_migration_reverted" in by_type
    payload = by_type["pack_migration_applied"].get("payload") or {}
    assert payload.get("pack_id") == PACK
    assert payload.get("replacement_pack_id") == REPLACEMENT
    assert "packId" in (payload.get("fields") or [])


def test_a_no_op_apply_is_not_an_audit_event(client, isolated_org, deprecated_pack):
    """Only a real configuration change is auditable — the pack_state rule."""
    _save_config(client, isolated_org, packId=REPLACEMENT, packIds=[REPLACEMENT])

    assert _apply(client).json()["changed"] is False

    entries = client.get("/api/audit-log", headers=_auth()).json()
    assert not [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_migration_applied"
    ]
