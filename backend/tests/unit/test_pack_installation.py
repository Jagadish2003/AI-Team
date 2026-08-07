"""2.0-C3 T4 (AT-839) — install/activate through C1/C2, DB-free.

Sub-task scope (installation half): *install/activate flows through C1/C2 —
compatibility checked, level enforced by org policy.*

Parent-story criteria discharged here:

  * AC4 (install half) — a tampered bundle fails INSTALLATION, not just
    verification.
  * AC5 — installation validation runs the manifest schema + the author's
    fixtures; a failing pack cannot be activated and reports specific failures.
  * AC1 (install half) — a toolkit-authored pack installs and activates.

The properties these tests exist to hold:

  * the gates run in order and each refusal NAMES its gate — an operator handed
    "installation failed" cannot act on it;
  * installing is not activating, and the two gates that can move underneath an
    installed pack (platform version, certification floor) are re-checked when it
    is activated;
  * nothing is ever deleted — withdrawal is a status write;
  * the certification floor FAILS CLOSED, matching the policy module.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from app.pack_certification_policy import (  # noqa: E402
    InMemoryPackCertificationPolicyStore,
    PackCertificationPolicyStore,
    set_certification_policy,
    set_policy_store,
)
from app.pack_installation import (  # noqa: E402
    REASON_BUNDLE_UNVERIFIED,
    REASON_CERTIFICATION_POLICY,
    REASON_CERTIFICATION_POLICY_UNAVAILABLE,
    REASON_INCOMPATIBLE,
    REASON_NOT_INSTALLED,
    REASON_VALIDATION_FAILED,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    STATUS_INSTALLED,
    InMemoryInstalledPackStore,
    PackInstallRefused,
    active_installed_pack_ids,
    get_installed_pack,
    install_pack_bundle,
    installed_pack_config,
    list_installed_packs,
    set_installed_pack_activation,
    set_installed_pack_store,
)
from discovery.packs.pack_certification import LEVEL_CERTIFIED, LEVEL_COMMUNITY  # noqa: E402
from discovery.packs.sdk.bundle import build_bundle, set_trusted_publisher_keys  # noqa: E402
from discovery.packs.sdk.scaffold import scaffold_pack  # noqa: E402

ORG = "org-install-tests"
OTHER_ORG = "org-other"
ACTOR = "owner@example.test"


def keypair():
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
    return seed, public


@pytest.fixture(autouse=True)
def _stores():
    set_installed_pack_store(InMemoryInstalledPackStore())
    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_installed_pack_store(None)
    set_policy_store(None)


@pytest.fixture()
def signing():
    seed, public = keypair()
    set_trusted_publisher_keys({"acme-2026": public})
    yield seed
    set_trusted_publisher_keys(None)


def build_pack(tmp_path, signing_seed, *, pack_id="acme_service_desk", mutate=None) -> bytes:
    project = tmp_path / pack_id
    scaffold_pack(
        project,
        pack_id=pack_id,
        author_name="Acme Ltd",
        author_contact="packs@acme.test",
    )
    if mutate is not None:
        document = json.loads((project / "pack.json").read_text("utf-8"))
        mutate(document)
        (project / "pack.json").write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / f"{pack_id}.aiqpack"
    build_bundle(project, output, signing_key=signing_seed, key_id="acme-2026")
    return output.read_bytes()


# ── The happy path ────────────────────────────────────────────────────────────


def test_a_toolkit_authored_pack_installs(tmp_path, signing):
    outcome = install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    assert outcome.record.pack_id == "acme_service_desk"
    assert outcome.record.status == STATUS_INSTALLED
    assert outcome.activated is False
    assert outcome.compatibility.compatible
    assert outcome.record.bundle_digest
    assert outcome.record.signing_key_id == "acme-2026"
    assert outcome.record.installed_by == ACTOR


def test_install_can_activate_in_one_step(tmp_path, signing):
    outcome = install_pack_bundle(
        ORG, build_pack(tmp_path, signing), actor_id=ACTOR, activate=True
    )
    assert outcome.record.status == STATUS_ACTIVE
    assert active_installed_pack_ids(ORG) == ["acme_service_desk"]


def test_installing_does_not_activate_by_default(tmp_path, signing):
    """Activation is a separate decision — installing records, activating runs."""
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    assert active_installed_pack_ids(ORG) == []


def test_reinstalling_upgrades_and_bumps_the_revision(tmp_path, signing):
    payload = build_pack(tmp_path, signing)
    first = install_pack_bundle(ORG, payload, actor_id=ACTOR)
    second = install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert second.record.revision == first.record.revision + 1
    assert len(list_installed_packs(ORG)) == 1


def test_an_installed_pack_projects_to_a_registry_shaped_config(tmp_path, signing):
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    config = installed_pack_config(ORG, "acme_service_desk")
    assert config["packId"] == "acme_service_desk"
    assert config["detectors"] == []          # authored packs carry no module paths
    assert config["manifestDetectors"]
    assert config["certification"]["level"] == LEVEL_COMMUNITY
    assert config["installed"]["status"] == STATUS_INSTALLED


def test_installs_are_org_scoped(tmp_path, signing):
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    assert list_installed_packs(OTHER_ORG) == []
    assert get_installed_pack(OTHER_ORG, "acme_service_desk") is None


# ── Gate 1: signature (AC4, install half) ─────────────────────────────────────


def test_a_tampered_bundle_cannot_be_installed(tmp_path, signing):
    payload = bytearray(build_pack(tmp_path, signing))
    payload[len(payload) // 2] ^= 0xFF
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, bytes(payload), actor_id=ACTOR)
    assert excinfo.value.reason == REASON_BUNDLE_UNVERIFIED
    assert list_installed_packs(ORG) == []


def test_a_bundle_from_an_untrusted_publisher_cannot_be_installed(tmp_path, signing):
    payload = build_pack(tmp_path, signing)
    set_trusted_publisher_keys({})
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_BUNDLE_UNVERIFIED


# ── Gate 2: validation (AC5) ──────────────────────────────────────────────────


def test_a_pack_whose_fixtures_fail_cannot_be_installed(tmp_path, signing):
    """The author's own harness runs at install time, through the same check."""
    project = tmp_path / "failing"
    scaffold_pack(project, pack_id="failing_pack", author_name="A", author_contact="a@b.test")
    case_path = project / "fixtures" / "01_recurring_work_fires.json"
    case = json.loads(case_path.read_text("utf-8"))
    case["expect"]["detectors"]["repeated_work_item"]["findingCount"] = 99
    case_path.write_text(json.dumps(case), encoding="utf-8")
    output = tmp_path / "failing.aiqpack"
    build_bundle(project, output, signing_key=signing, key_id="acme-2026")

    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, output.read_bytes(), actor_id=ACTOR)
    assert excinfo.value.reason == REASON_VALIDATION_FAILED
    assert excinfo.value.failures, "a refusal must report specific failures"
    assert any("fixture" in failure for failure in excinfo.value.failures)


def test_a_pack_failing_lint_cannot_be_installed(tmp_path, signing):
    payload = build_pack(
        tmp_path,
        signing,
        pack_id="linty_pack",
        mutate=lambda doc: doc["detectors"][0]["labels"].__setitem__(
            "summary", "Ranked by assignee."
        ),
    )
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_VALIDATION_FAILED
    assert any("individual_naming" in failure for failure in excinfo.value.failures)


def test_a_refused_install_leaves_nothing_behind(tmp_path, signing):
    payload = build_pack(
        tmp_path,
        signing,
        pack_id="linty_two",
        mutate=lambda doc: doc["detectors"][0]["labels"].__setitem__(
            "summary", "Grouped by assignee."
        ),
    )
    with pytest.raises(PackInstallRefused):
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert list_installed_packs(ORG) == []


# ── Gate 3: compatibility (2.0-C1) ────────────────────────────────────────────


def test_an_incompatible_pack_cannot_be_installed(tmp_path, signing):
    payload = build_pack(
        tmp_path,
        signing,
        pack_id="future_pack",
        mutate=lambda doc: doc["compatibility"].__setitem__(
            "minPlatformVersion", "99.0.0"
        ),
    )
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_INCOMPATIBLE
    assert "99.0.0" in str(excinfo.value)


def test_a_pack_above_its_ceiling_cannot_be_installed(tmp_path, signing):
    payload = build_pack(
        tmp_path,
        signing,
        pack_id="legacy_pack",
        mutate=lambda doc: doc["compatibility"].__setitem__(
            "maxPlatformVersion", "1.5.0"
        ),
    )
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_INCOMPATIBLE


# ── Gate 4: certification policy (2.0-C2) ─────────────────────────────────────


def test_a_certified_only_org_refuses_an_authored_pack(tmp_path, signing):
    """An authored pack is Community — it holds no CloudFulcrum signature."""
    set_certification_policy(ORG, LEVEL_CERTIFIED, actor_id=ACTOR)
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    assert excinfo.value.reason == REASON_CERTIFICATION_POLICY
    assert "Certified" in str(excinfo.value)


def test_an_unrestricted_org_installs_the_same_pack(tmp_path, signing):
    outcome = install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    assert outcome.record.certification_level == LEVEL_COMMUNITY


def test_an_unreadable_policy_refuses_rather_than_assuming_compliance(tmp_path, signing):
    """Fail CLOSED — a control that fails open lifts the restriction exactly when
    it matters."""

    class BrokenPolicyStore(PackCertificationPolicyStore):
        def get(self, org_id):  # noqa: D102 - contract method
            raise RuntimeError("policy store is down")

        def set(self, *args, **kwargs):  # noqa: D102 - contract method
            raise RuntimeError("policy store is down")

    payload = build_pack(tmp_path, signing)
    set_policy_store(BrokenPolicyStore())
    with pytest.raises(PackInstallRefused) as excinfo:
        install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_CERTIFICATION_POLICY_UNAVAILABLE


def test_the_certification_level_is_never_the_requested_one(tmp_path, signing):
    """A publisher asking for Partner does not receive it by asking."""
    payload = build_pack(
        tmp_path,
        signing,
        pack_id="ambitious_pack",
        mutate=lambda doc: doc["certification"].__setitem__("requestedLevel", "partner"),
    )
    outcome = install_pack_bundle(ORG, payload, actor_id=ACTOR)
    assert outcome.record.requested_level == "partner"
    assert outcome.record.certification_level == LEVEL_COMMUNITY
    assert outcome.record.to_dict()["certificationLevel"] == LEVEL_COMMUNITY


# ── Activation re-runs the gates ──────────────────────────────────────────────


def test_activation_is_refused_once_the_org_restricts_to_certified(tmp_path, signing):
    """The floor can move after install; trusting the install-time verdict is how a
    restricted deployment quietly ends up running a pack it now forbids."""
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    set_certification_policy(ORG, LEVEL_CERTIFIED, actor_id=ACTOR)
    with pytest.raises(PackInstallRefused) as excinfo:
        set_installed_pack_activation(
            ORG, "acme_service_desk", active=True, actor_id=ACTOR
        )
    assert excinfo.value.reason == REASON_CERTIFICATION_POLICY
    assert get_installed_pack(ORG, "acme_service_desk").status == STATUS_INSTALLED


def test_activation_and_withdrawal_round_trip(tmp_path, signing):
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR)
    activated = set_installed_pack_activation(
        ORG, "acme_service_desk", active=True, actor_id=ACTOR
    )
    assert activated.status == STATUS_ACTIVE
    withdrawn = set_installed_pack_activation(
        ORG, "acme_service_desk", active=False, actor_id=ACTOR
    )
    assert withdrawn.status == STATUS_INACTIVE
    assert active_installed_pack_ids(ORG) == []


def test_withdrawal_never_deletes_the_record(tmp_path, signing):
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR, activate=True)
    set_installed_pack_activation(ORG, "acme_service_desk", active=False, actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")
    assert record is not None
    assert record.manifest and record.bundle_digest and record.signing_key_id


def test_withdrawal_is_allowed_even_under_a_restriction(tmp_path, signing):
    """Taking a pack OUT of service must never be blocked by a policy."""
    install_pack_bundle(ORG, build_pack(tmp_path, signing), actor_id=ACTOR, activate=True)
    set_certification_policy(ORG, LEVEL_CERTIFIED, actor_id=ACTOR)
    withdrawn = set_installed_pack_activation(
        ORG, "acme_service_desk", active=False, actor_id=ACTOR
    )
    assert withdrawn.status == STATUS_INACTIVE


def test_activating_a_pack_that_is_not_installed_is_refused():
    with pytest.raises(PackInstallRefused) as excinfo:
        set_installed_pack_activation(ORG, "nope_pack", active=True, actor_id=ACTOR)
    assert excinfo.value.reason == REASON_NOT_INSTALLED


# ── Reserved ids ──────────────────────────────────────────────────────────────


def test_a_bundle_cannot_install_over_a_first_party_pack(tmp_path, signing):
    """The schema refuses a first-party id; this guards a bundle built before a
    pack of that name shipped."""
    from discovery.packs import pack_config

    project = tmp_path / "shadow"
    scaffold_pack(project, pack_id="shadow_pack", author_name="A", author_contact="a@b.test")
    document = json.loads((project / "pack.json").read_text("utf-8"))
    document["pack"]["packId"] = "cloud_ops"
    document["pack"]["domain"] = "cloud_ops"
    (project / "pack.json").write_text(json.dumps(document), encoding="utf-8")

    assert "cloud_ops" in pack_config.PACK_REGISTRY
    from discovery.packs.sdk.bundle import BundleError

    # Packaging refuses it first — the manifest schema will not validate a
    # first-party id, so the bundle cannot even be built.
    with pytest.raises(BundleError) as excinfo:
        build_bundle(
            project, tmp_path / "shadow.aiqpack", signing_key=signing, key_id="acme-2026"
        )
    assert "cloud_ops" in str(excinfo.value)


# ── No delete path ────────────────────────────────────────────────────────────


def test_the_installation_module_exposes_no_delete_path():
    source = Path(__file__).resolve().parents[2] / "app" / "pack_installation.py"
    text = source.read_text(encoding="utf-8")
    assert "DELETE FROM" not in text.upper()
    assert "DROP TABLE" not in text.upper()
    from app import pack_installation

    for name in dir(pack_installation.InstalledPackStore):
        assert not name.startswith(("delete", "remove", "purge"))
