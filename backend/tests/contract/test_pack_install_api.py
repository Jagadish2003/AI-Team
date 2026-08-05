"""2.0-C3 T4 (AT-839) — installing an authored pack over HTTP.

Parent-story criteria exercised here:

  * **AC4** — a tampered bundle fails installation, over the real API.
  * **AC5** — installation validation runs the manifest schema and the author's
    fixtures; a failing pack cannot be activated and the response reports
    SPECIFIC failures.

Plus the properties that make this an ownership-level control rather than a
convenience endpoint: the Owner-only write boundary, org isolation, the audit
entry, and the fail-closed certification floor (503 when the policy cannot be
read — "could not determine compliance" is not the same as "non-compliant", and
an operator has to be able to tell them apart).

The gates themselves are pinned DB-free in ``tests/unit/test_pack_installation.py``.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Dict, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pack_certification_policy import (
    InMemoryPackCertificationPolicyStore,
    set_certification_policy,
    set_policy_store,
)
from app.pack_installation import (
    InMemoryInstalledPackStore,
    set_installed_pack_store,
)
from app.rbac import seed_owner, seed_static_token_members
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from discovery.packs.pack_certification import LEVEL_CERTIFIED
from discovery.packs.sdk.bundle import build_bundle, set_trusted_publisher_keys
from discovery.packs.sdk.scaffold import scaffold_pack

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PACK_ID = "acme_service_desk"


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_stores():
    """Isolate the installed-pack registry and the policy per test.

    Critical here for the same reason as the policy suite: a leaked
    "Certified only" floor would refuse activations across every other suite
    sharing the contract database.
    """
    set_installed_pack_store(InMemoryInstalledPackStore())
    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_installed_pack_store(None)
    set_policy_store(None)


@pytest.fixture()
def signing_key():
    private = Ed25519PrivateKey.generate()
    seed = base64.b64encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()
    public = base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    set_trusted_publisher_keys({"acme-2026": public})
    yield seed
    set_trusted_publisher_keys(None)


@pytest.fixture()
def org(monkeypatch) -> Iterator[str]:
    """A fresh org per test, so nothing this suite installs or restricts leaks."""
    org_id = f"pack_install_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    # The static analyst/viewer tokens need a membership row in THIS org, or the
    # role-boundary assertions below would pass for the wrong reason (403 because
    # the token is a stranger here, not because the role is too low).
    seed_static_token_members(org_id)
    yield org_id


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def bundle_b64(tmp_path, signing_key) -> str:
    project = tmp_path / "pack"
    scaffold_pack(
        project, pack_id=PACK_ID, author_name="Acme Ltd", author_contact="packs@acme.test"
    )
    output = tmp_path / "pack.aiqpack"
    build_bundle(project, output, signing_key=signing_key, key_id="acme-2026")
    return base64.b64encode(output.read_bytes()).decode()


def auth(org_id: str, token: str = OWNER_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def install(
    client: TestClient,
    org_id: str,
    payload: str,
    *,
    token: str = OWNER_TOKEN,
    activate: bool = False,
):
    return client.post(
        "/api/packs/install",
        json={"bundleBase64": payload, "activate": activate},
        headers=auth(org_id, token),
    )


# ── Install ───────────────────────────────────────────────────────────────────


def test_owner_installs_a_signed_bundle(client, org, bundle_b64):
    response = install(client, org, bundle_b64)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["packId"] == PACK_ID
    assert body["status"] == "installed"
    assert body["active"] is False
    assert body["certificationLevel"] == "community"
    assert body["bundleDigest"]
    assert body["signingKeyId"] == "acme-2026"
    assert body["compatibility"]["compatible"] is True


def test_installed_pack_is_listed(client, org, bundle_b64):
    install(client, org, bundle_b64)
    response = client.get("/api/packs/installed", headers=auth(org))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["packs"][0]["packId"] == PACK_ID


def test_a_viewer_can_read_the_installed_list(client, org, bundle_b64):
    """A viewer looking at a finding attributed to a partner pack must be able to
    see which pack that is and who published it."""
    install(client, org, bundle_b64)
    response = client.get("/api/packs/installed", headers=auth(org, VIEWER_TOKEN))
    assert response.status_code == 200
    assert response.json()["packs"][0]["publisher"] == "Acme Ltd"


@pytest.mark.parametrize("token", [ANALYST_TOKEN, VIEWER_TOKEN])
def test_installing_is_owner_only(client, org, bundle_b64, token):
    assert install(client, org, bundle_b64, token=token).status_code == 403


def test_installing_requires_authentication(client, org, bundle_b64):
    response = client.post(
        "/api/packs/install", json={"bundleBase64": bundle_b64, "activate": False}
    )
    assert response.status_code in (401, 403)


# ── AC4: a tampered bundle fails installation ─────────────────────────────────


def test_a_tampered_bundle_is_refused(client, org, bundle_b64):
    raw = bytearray(base64.b64decode(bundle_b64))
    raw[len(raw) // 2] ^= 0xFF
    response = install(client, org, base64.b64encode(bytes(raw)).decode())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "bundle_unverified"
    assert client.get("/api/packs/installed", headers=auth(org)).json()["count"] == 0


def test_a_non_bundle_body_is_refused(client, org):
    response = install(client, org, base64.b64encode(b"not a bundle").decode())
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "bundle_unverified"


def test_a_non_base64_body_is_a_400(client, org):
    response = install(client, org, "!!! not base64 !!!")
    assert response.status_code == 400


# ── AC5: validation reports specific failures ─────────────────────────────────


def test_a_pack_failing_lint_is_refused_with_specific_reasons(
    client, org, tmp_path, signing_key
):
    project = tmp_path / "linty"
    scaffold_pack(project, pack_id="linty_pack", author_name="A", author_contact="a@b.test")
    document = json.loads((project / "pack.json").read_text("utf-8"))
    document["detectors"][0]["labels"]["summary"] = "Ranked by assignee."
    (project / "pack.json").write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "linty.aiqpack"
    build_bundle(project, output, signing_key=signing_key, key_id="acme-2026")

    response = install(client, org, base64.b64encode(output.read_bytes()).decode())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert detail["failures"], "a refusal must name what failed"
    assert any("individual_naming" in failure for failure in detail["failures"])


def test_an_incompatible_pack_is_refused_naming_the_requirement(
    client, org, tmp_path, signing_key
):
    project = tmp_path / "future"
    scaffold_pack(project, pack_id="future_pack", author_name="A", author_contact="a@b.test")
    document = json.loads((project / "pack.json").read_text("utf-8"))
    document["compatibility"]["minPlatformVersion"] = "99.0.0"
    (project / "pack.json").write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "future.aiqpack"
    build_bundle(project, output, signing_key=signing_key, key_id="acme-2026")

    response = install(client, org, base64.b64encode(output.read_bytes()).decode())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "incompatible_with_platform"
    assert "99.0.0" in detail["message"]


# ── Certification policy (2.0-C2) enforced at install and activation ──────────


def test_a_certified_only_org_refuses_the_install(client, org, bundle_b64):
    set_certification_policy(org, LEVEL_CERTIFIED, actor_id="owner")
    response = install(client, org, bundle_b64)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "certification_policy_violation"


def test_activation_is_refused_after_the_floor_is_raised(client, org, bundle_b64):
    assert install(client, org, bundle_b64).status_code == 201
    set_certification_policy(org, LEVEL_CERTIFIED, actor_id="owner")
    response = client.put(
        f"/api/packs/installed/{PACK_ID}/activation",
        json={"active": True},
        headers=auth(org),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "certification_policy_violation"


# ── Activation ────────────────────────────────────────────────────────────────


def test_activation_round_trip(client, org, bundle_b64):
    install(client, org, bundle_b64)
    activated = client.put(
        f"/api/packs/installed/{PACK_ID}/activation",
        json={"active": True},
        headers=auth(org),
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"

    withdrawn = client.put(
        f"/api/packs/installed/{PACK_ID}/activation",
        json={"active": False},
        headers=auth(org),
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "inactive"
    # Withdrawal is a status write, never a delete.
    listed = client.get("/api/packs/installed", headers=auth(org)).json()
    assert listed["count"] == 1


def test_activating_an_uninstalled_pack_is_a_404(client, org):
    response = client.put(
        "/api/packs/installed/nope_pack/activation",
        json={"active": True},
        headers=auth(org),
    )
    assert response.status_code == 404


@pytest.mark.parametrize("token", [ANALYST_TOKEN, VIEWER_TOKEN])
def test_activation_is_owner_only(client, org, bundle_b64, token):
    install(client, org, bundle_b64)
    response = client.put(
        f"/api/packs/installed/{PACK_ID}/activation",
        json={"active": True},
        headers=auth(org, token),
    )
    assert response.status_code == 403


# ── Audit ─────────────────────────────────────────────────────────────────────


def test_installing_writes_an_audit_event(client, org, bundle_b64):
    """Provenance has to survive in the org-wide trail: which bytes were installed,
    by whom, and on whose signature."""
    install(client, org, bundle_b64)
    entries = client.get("/api/audit-log", headers=auth(org)).json()
    installs = [
        entry for entry in entries if entry.get("event_type") == "pack_installed"
    ]
    assert installs, "no pack_installed audit entry was written"
    payload = installs[0].get("payload") or {}
    assert payload.get("pack_id") == PACK_ID
    assert payload.get("bundle_digest")
    assert payload.get("signing_key_id") == "acme-2026"
    assert installs[0].get("user_id")


def test_activation_writes_its_own_audit_event(client, org, bundle_b64):
    """Distinct from pack_state_changed: 'a partner pack went live here' and 'a
    first-party pack was re-enabled' must not be indistinguishable in the trail."""
    install(client, org, bundle_b64)
    client.put(
        f"/api/packs/installed/{PACK_ID}/activation",
        json={"active": True},
        headers=auth(org),
    )
    entries = client.get("/api/audit-log", headers=auth(org)).json()
    changes = [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_activation_changed"
    ]
    assert changes, "no pack_activation_changed audit entry was written"
    assert (changes[0].get("payload") or {}).get("status") == "active"
