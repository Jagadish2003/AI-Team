"""2.0-C3 T6 (AT-841) — sandbox validation before activation.

Sub-task scope: *installing a pack runs its manifest through validation and its
fixtures through the harness before activation; failures block activation with
specific reasons.*

Parent-story criterion discharged here:

  * **AC5** — installation validation runs the manifest schema + the author's
    fixtures, a failing pack cannot be activated, and the refusal reports the
    specific failures.

The properties these tests exist to hold:

  * **activation re-runs the check**, against today's platform, from the stored
    manifest and the stored fixtures — the install-time verdict is not carried
    forward, because the platform moves between install and activation;
  * a refusal names the **stage** it failed at and lists the **specific**
    reasons — "validation failed" is not actionable;
  * the run is **bounded**, and a breach is a named refusal rather than a slow
    request nobody can explain;
  * "too expensive to judge" and "judged and found wrong" are different verdicts
    and get different reasons;
  * withdrawal runs no gates — taking a pack out of service is never blocked.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import replace

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.pack_certification_policy import (  # noqa: E402
    InMemoryPackCertificationPolicyStore,
    set_policy_store,
)
from app.pack_installation import (  # noqa: E402
    REASON_SANDBOX_LIMIT,
    REASON_VALIDATION_FAILED,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    InMemoryInstalledPackStore,
    PackInstallRefused,
    get_installed_pack,
    get_installed_pack_store,
    get_installed_pack_validation,
    install_pack_bundle,
    revalidate_installed_pack,
    set_installed_pack_activation,
    set_installed_pack_store,
)
from app.pack_sandbox import (  # noqa: E402
    DEFAULT_MAX_CASES,
    STAGE_ADMISSION,
    STAGE_FIXTURES,
    STAGE_LINT,
    STAGE_PASSED,
    STAGE_VALIDATION,
    SandboxLimits,
    check_admission,
    run_sandbox_validation,
    sandbox_pack_directory,
)
from discovery.packs.sdk.bundle import build_bundle, set_trusted_publisher_keys  # noqa: E402
from discovery.packs.sdk.harness import load_cases  # noqa: E402
from discovery.packs.sdk.scaffold import scaffold_pack  # noqa: E402

ORG = "org-sandbox-tests"
ACTOR = "owner@example.test"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def stores():
    set_installed_pack_store(InMemoryInstalledPackStore())
    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_installed_pack_store(None)
    set_policy_store(None)


@pytest.fixture()
def signing():
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
def project(tmp_path):
    """A scaffolded pack project — known-good by construction (AT-838)."""
    result = scaffold_pack(
        tmp_path / "acme_service_desk",
        pack_id="acme_service_desk",
        author_name="Acme Ltd",
        author_contact="packs@acme.test",
    )
    return result.directory


@pytest.fixture()
def document(project):
    return json.loads((project / "pack.json").read_text("utf-8"))


@pytest.fixture()
def cases(project):
    return load_cases(project / "fixtures")


def bundle_of(project, seed, tmp_path, name="acme_service_desk") -> bytes:
    output = tmp_path / f"{name}.aiqpack"
    build_bundle(project, output, signing_key=seed, key_id="acme-2026")
    return output.read_bytes()


def seeded_case(name: str, records: int) -> dict:
    return {
        "name": name,
        "signal": {
            "records": [
                {
                    "concept": "incident_workflow",
                    "record_id": f"INC-{index}",
                    "source_system": "servicenow",
                    "observed_at": "2026-06-01T00:00:00Z",
                    "actor_group": "service-desk",
                    "state": "open",
                }
                for index in range(records)
            ]
        },
        "expect": {"findingCount": 0},
    }


# ── Limits ────────────────────────────────────────────────────────────────────


def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PACK_SANDBOX_MAX_CASES", "7")
    monkeypatch.setenv("PACK_SANDBOX_TIMEOUT_SECONDS", "2.5")
    limits = SandboxLimits.from_env()
    assert limits.max_cases == 7
    assert limits.timeout_seconds == 2.5


@pytest.mark.parametrize("value", ["0", "-1", "banana", ""])
def test_a_bad_limit_falls_back_to_the_default_rather_than_removing_the_bound(
    monkeypatch, value
):
    """A bound an operator can delete with a typo is not a bound."""
    monkeypatch.setenv("PACK_SANDBOX_MAX_CASES", value)
    assert SandboxLimits.from_env().max_cases == DEFAULT_MAX_CASES


def test_admission_reports_every_breach_not_the_first():
    """An author who shrinks their suite once per rejection submits five times."""
    limits = SandboxLimits(max_cases=1, max_records_per_case=2, max_total_records=3)
    problems = check_admission(
        [seeded_case("a", 5), seeded_case("b", 5)], limits
    )
    assert len(problems) >= 3
    assert any("fixture cases exceeds" in problem for problem in problems)
    assert any("per-case sandbox limit" in problem for problem in problems)
    assert any("in total" in problem for problem in problems)


def test_an_oversized_suite_is_refused_before_a_single_case_runs(document):
    limits = SandboxLimits(max_records_per_case=10)
    report = run_sandbox_validation(
        document, [seeded_case("huge", 500)], limits=limits
    )
    assert report.ok is False
    assert report.stage == STAGE_ADMISSION
    assert report.duration_ms == 0        # nothing was executed
    assert "500 records" in report.reasons[0]


def test_a_suite_over_its_byte_budget_is_refused(document):
    report = run_sandbox_validation(
        document, [seeded_case("wide", 200)], limits=SandboxLimits(max_fixture_bytes=64)
    )
    assert report.stage == STAGE_ADMISSION
    assert any("bytes" in reason for reason in report.reasons)


def test_a_suite_past_its_time_budget_is_refused_as_a_limit_not_a_failure(
    document, cases
):
    """Out of time is not the same verdict as wrong, and says so."""
    # A zero budget, so the assertion does not depend on the platform's clock
    # resolution: at the deadline you are out of time, and the check is >=.
    report = run_sandbox_validation(
        document, cases, limits=SandboxLimits(timeout_seconds=0.0)
    )
    assert report.ok is False
    assert report.stage == STAGE_ADMISSION
    assert "time budget" in report.reasons[0]


# ── Stages ────────────────────────────────────────────────────────────────────


def test_a_good_pack_passes_and_reports_what_it_cost(document, cases):
    report = run_sandbox_validation(document, cases)
    assert report.ok is True
    assert report.stage == STAGE_PASSED
    assert report.reasons == []
    assert report.case_count == len(cases)
    assert report.record_count > 0
    assert report.manifest is not None


def test_a_malformed_manifest_fails_at_validation(document, cases):
    document["detectors"][0]["primitive"] = "not_a_primitive"
    report = run_sandbox_validation(document, cases)
    assert report.stage == STAGE_VALIDATION
    assert any("not_a_primitive" in reason for reason in report.reasons)


def test_a_failing_fixture_fails_at_fixtures_with_the_case_named(document, cases):
    broken = copy.deepcopy(cases)
    detector_id = document["detectors"][0]["detectorId"]
    broken[-1]["expect"].setdefault("detectors", {})[detector_id] = {"fires": True}
    report = run_sandbox_validation(document, broken)
    assert report.ok is False
    assert report.stage == STAGE_FIXTURES
    assert any(detector_id in reason for reason in report.reasons)


def test_a_dishonest_pack_fails_at_lint(document, cases):
    """Valid, fixtures pass, and still not activatable — lint is the third stage."""
    document["terminology"]["glossary"]["owner"] = "The assignee who resolved it."
    report = run_sandbox_validation(document, cases)
    assert report.stage == STAGE_LINT
    assert any("individual" in reason for reason in report.reasons)


def test_a_pack_with_no_fixtures_says_so_rather_than_reporting_a_full_pass(document):
    report = run_sandbox_validation(document, [])
    assert report.notes and "no fixtures" in report.notes[0]
    assert report.case_count == 0


def test_a_project_with_no_fixtures_directory_is_refused(project):
    for path in (project / "fixtures").glob("*.json"):
        path.unlink()
    report = sandbox_pack_directory(project)
    assert report.ok is False
    assert report.stage == STAGE_FIXTURES
    assert "no test cases" in report.reasons[0]


def test_the_report_is_json_serialisable_without_the_manifest(document, cases):
    report = run_sandbox_validation(document, cases)
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["ok"] is True
    assert payload["limits"]["maxCases"] == DEFAULT_MAX_CASES
    assert "manifest" not in payload


# ── Installation: the fixtures are kept, and re-run before activation ─────────


def test_install_stores_the_fixtures_and_the_verdict(tmp_path, project, signing):
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")
    assert len(record.fixtures) == 3
    assert record.validation["ok"] is True
    assert record.validation["stage"] == STAGE_PASSED


def test_activation_re_runs_the_fixtures_and_blocks_on_failure(
    tmp_path, project, signing
):
    """The point of the whole task: the install-time verdict is not carried
    forward. A pack whose stored manifest no longer holds up cannot activate."""
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")

    broken = copy.deepcopy(dict(record.manifest))
    broken["terminology"]["glossary"]["owner"] = "The assignee who resolved it."
    get_installed_pack_store().upsert(replace(record, manifest=broken))

    with pytest.raises(PackInstallRefused) as refusal:
        set_installed_pack_activation(
            ORG, "acme_service_desk", active=True, actor_id=ACTOR
        )
    assert refusal.value.reason == REASON_VALIDATION_FAILED
    assert refusal.value.failures, "a refusal with no reasons is not actionable"
    assert any("individual" in failure for failure in refusal.value.failures)
    assert get_installed_pack(ORG, "acme_service_desk").status != STATUS_ACTIVE


def test_a_blocked_activation_records_why(tmp_path, project, signing):
    """An operator must be able to read the reasons without re-uploading."""
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")
    broken = copy.deepcopy(dict(record.manifest))
    broken["detectors"][0]["primitive"] = "not_a_primitive"
    get_installed_pack_store().upsert(replace(record, manifest=broken))

    with pytest.raises(PackInstallRefused):
        set_installed_pack_activation(
            ORG, "acme_service_desk", active=True, actor_id=ACTOR
        )
    stored = get_installed_pack_validation(ORG, "acme_service_desk")
    assert stored["ok"] is False
    assert stored["stage"] == STAGE_VALIDATION
    assert stored["reasons"]


def test_a_good_pack_still_activates(tmp_path, project, signing):
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = set_installed_pack_activation(
        ORG, "acme_service_desk", active=True, actor_id=ACTOR
    )
    assert record.status == STATUS_ACTIVE
    assert record.validation["ok"] is True


def test_withdrawal_runs_no_gates(tmp_path, project, signing):
    """Taking a pack OUT of service must never be blocked by its own condition."""
    install_pack_bundle(
        ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR, activate=True
    )
    record = get_installed_pack(ORG, "acme_service_desk")
    broken = copy.deepcopy(dict(record.manifest))
    broken["detectors"][0]["primitive"] = "not_a_primitive"
    get_installed_pack_store().upsert(replace(record, manifest=broken))

    withdrawn = set_installed_pack_activation(
        ORG, "acme_service_desk", active=False, actor_id=ACTOR
    )
    assert withdrawn.status == STATUS_INACTIVE


def test_oversized_fixtures_are_refused_as_a_limit_not_as_a_bad_pack(
    tmp_path, project, signing, monkeypatch
):
    """'Too expensive to judge' and 'judged and found wrong' need different
    reasons, because they need different actions from the author."""
    monkeypatch.setenv("PACK_SANDBOX_MAX_TOTAL_RECORDS", "1")
    with pytest.raises(PackInstallRefused) as refusal:
        install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    assert refusal.value.reason == REASON_SANDBOX_LIMIT
    assert refusal.value.failures
    assert get_installed_pack(ORG, "acme_service_desk") is None


def test_a_record_stored_before_this_feature_revalidates_without_its_fixtures(
    tmp_path, project, signing
):
    """A row written before AT-841 has no stored fixtures. It must degrade to
    'manifest and lint only, and say so' rather than either failing or pretending
    the fixtures passed."""
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")
    get_installed_pack_store().upsert(replace(record, fixtures=(), validation={}))

    report = revalidate_installed_pack(get_installed_pack(ORG, "acme_service_desk"))
    assert report.ok is True
    assert report.case_count == 0
    assert any("no fixtures" in note for note in report.notes)

    activated = set_installed_pack_activation(
        ORG, "acme_service_desk", active=True, actor_id=ACTOR
    )
    assert activated.status == STATUS_ACTIVE


def test_revalidation_can_be_run_without_persisting(tmp_path, project, signing):
    install_pack_bundle(ORG, bundle_of(project, signing, tmp_path), actor_id=ACTOR)
    record = get_installed_pack(ORG, "acme_service_desk")
    before = dict(record.validation)
    revalidate_installed_pack(record, persist=False)
    assert dict(get_installed_pack(ORG, "acme_service_desk").validation) == before


def test_the_validation_accessor_is_absent_rather_than_empty_for_an_unknown_pack():
    assert get_installed_pack_validation(ORG, "never_installed") is None
