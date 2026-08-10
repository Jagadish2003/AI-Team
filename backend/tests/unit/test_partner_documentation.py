"""2.0-C3 T5 (AT-840) — partner documentation, and the worked example it describes.

Sub-task scope: *concept vocabulary (B4), primitive reference, the discipline
rules stated as requirements, and a worked example pack built end to end with the
toolkit.*

Parent-story criterion discharged here:

  * **AC6** — "the worked example pack in the documentation builds and passes end
    to end in CI, so the docs cannot rot." Deliberately read in its strongest
    form: not only does the example build, but every reference block in the
    published documentation is re-rendered from the platform's own declarations
    and compared. A concept added, a primitive parameter re-bounded, or a lint
    floor raised without regenerating the docs fails this suite.
  * **AC1** (authoring half) — a pack authored entirely with the toolkit installs,
    activates, and produces findings carrying all four contract parts.

The properties these tests exist to hold:

  * documentation that names a list the platform owns is generated from that
    list, never transcribed;
  * the worked example is a real project that passes validate + fixtures + lint,
    packages into a signed bundle, and installs through the C1/C2 gates;
  * it teaches the negative case — a positive-only example is an example of the
    wrong habit;
  * every command and cross-reference the documents tell a partner to use exists.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from app.pack_certification_policy import (  # noqa: E402
    InMemoryPackCertificationPolicyStore,
    set_policy_store,
)
from app.pack_installation import (  # noqa: E402
    STATUS_ACTIVE,
    InMemoryInstalledPackStore,
    install_pack_bundle,
    installed_pack_config,
    set_installed_pack_activation,
    set_installed_pack_store,
)
from discovery.packs.platform_capabilities import NORMALISED_CONCEPTS  # noqa: E402
from discovery.packs.sdk import reference_docs  # noqa: E402
from discovery.packs.sdk.bundle import (  # noqa: E402
    build_bundle,
    set_trusted_publisher_keys,
    verify_bundle,
)
from discovery.packs.sdk.contract import (  # noqa: E402
    FOUR_PART_CONTRACT_FIELDS,
    find_causal_language,
    find_individual_references,
)
from discovery.packs.sdk.execution import run_manifest  # noqa: E402
from discovery.packs.sdk.harness import load_cases  # noqa: E402
from discovery.packs.sdk.manifest import load_manifest  # noqa: E402
from discovery.packs.sdk.primitives import PRIMITIVE_LIBRARY  # noqa: E402
from discovery.packs.sdk.signals import ConceptRecord, signal_set_from_dicts  # noqa: E402
from discovery.packs.sdk.toolkit import check_pack_directory  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
PARTNER_DOCS = REPO_ROOT / "docs" / "partner"
EXAMPLE_PACK = (
    REPO_ROOT
    / "backend"
    / "discovery"
    / "packs"
    / "sdk"
    / "examples"
    / "example_service_desk"
)

ORG = "org-partner-docs"
ACTOR = "owner@example.test"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def manifest():
    return load_manifest(EXAMPLE_PACK / "pack.json")


@pytest.fixture()
def cases():
    return load_cases(EXAMPLE_PACK / "fixtures")


@pytest.fixture()
def findings(manifest, cases):
    produced = []
    for case in cases:
        result = run_manifest(manifest, signal_set_from_dicts(case["signal"]))
        produced.extend(result.findings)
    return produced


@pytest.fixture()
def stores():
    set_installed_pack_store(InMemoryInstalledPackStore())
    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_installed_pack_store(None)
    set_policy_store(None)


@pytest.fixture()
def signing():
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
    set_trusted_publisher_keys({"example-partner-2026": public})
    yield seed
    set_trusted_publisher_keys(None)


def doc_text(name: str) -> str:
    return (PARTNER_DOCS / name).read_text(encoding="utf-8")


def all_docs_text() -> str:
    return "\n".join(doc_text(name) for name in reference_docs.PARTNER_DOC_FILES)


# ── The documentation set exists and hangs together ───────────────────────────


def test_every_partner_document_is_published():
    missing = [
        name for name in reference_docs.PARTNER_DOC_FILES
        if not (PARTNER_DOCS / name).is_file()
    ]
    assert missing == []


def test_every_relative_link_resolves():
    """A partner following a link in our docs must land on a file, not a 404."""
    broken = []
    for name in reference_docs.PARTNER_DOC_FILES:
        for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", doc_text(name)):
            if not (PARTNER_DOCS / target).resolve().is_file():
                broken.append(f"{name} -> {target}")
    assert broken == []


def test_the_documentation_states_the_governing_constraint():
    """2.0-C3's whole design follows from one sentence; a partner must meet it
    before they meet anything else."""
    assert (
        "No partner-supplied executable code runs inside a customer deployment"
        in doc_text("README.md")
    )


# ── The docs cannot rot (AC6) ─────────────────────────────────────────────────


def test_generated_reference_blocks_match_the_platform():
    """The anti-rot guard. A concept added, a parameter re-bounded, or a floor
    raised without `pack_sdk.py docs --write` fails the build here rather than
    misleading a partner."""
    report = reference_docs.sync_docs(PARTNER_DOCS)
    assert report["ok"], report["stale"]


def test_a_hand_edited_generated_block_is_detected(tmp_path):
    """The guard is only worth having if it actually notices an edit."""
    path = tmp_path / "concept_vocabulary.md"
    path.write_text(
        doc_text("concept_vocabulary.md").replace(
            "| `incident_workflow` |", "| `incident_workflow_typo` |", 1
        ),
        encoding="utf-8",
    )
    assert reference_docs.check_document(path)
    repaired = reference_docs.apply_sections(path.read_text(encoding="utf-8"))
    path.write_text(repaired, encoding="utf-8")
    assert reference_docs.check_document(path) == []


def test_a_document_naming_an_unknown_section_is_refused():
    with pytest.raises(reference_docs.ReferenceDocsError):
        reference_docs.apply_sections(
            "<!-- generated:not_a_section -->\nx\n<!-- /generated:not_a_section -->"
        )
    with pytest.raises(reference_docs.ReferenceDocsError):
        reference_docs.render_section("not_a_section")


def test_every_generated_section_is_actually_published():
    """A section nobody publishes is a section nobody checks."""
    published = set()
    for name in reference_docs.PARTNER_DOC_FILES:
        published.update(reference_docs.section_names(doc_text(name)))
    assert published == set(reference_docs.SECTIONS)


def test_every_concept_and_primitive_is_documented():
    text = all_docs_text()
    assert [c for c in NORMALISED_CONCEPTS if f"`{c}`" not in text] == []
    assert [p for p in PRIMITIVE_LIBRARY if f"`{p}`" not in text] == []


def test_the_documented_record_shape_matches_the_signal_model():
    """The record table is hand-written prose around a real dataclass; if a field
    is added or renamed, the table must move with it."""
    table = doc_text("concept_vocabulary.md").split("## 2. What a record looks like")[1]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))
    assert documented == set(ConceptRecord.__dataclass_fields__)


def test_every_cli_command_the_docs_teach_exists():
    """Every `pack_sdk.py <command>` a partner is told to run must be a real
    subcommand — a documented command that does not exist is the first thing they
    will try."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pack_sdk_cli", REPO_ROOT / "backend" / "scripts" / "pack_sdk.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    taught = set(re.findall(r"pack_sdk\.py\s+([a-z]+)", all_docs_text()))
    assert taught, "the documentation teaches no commands at all"
    for command in sorted(taught):
        # argparse exits 0 for a known command's --help and 2 for an unknown one.
        with pytest.raises(SystemExit) as exit_info:
            module.main([command, "--help"])
        assert exit_info.value.code == 0, f"{command} is not a pack_sdk command"


# ── The worked example builds end to end (AC6, AC1) ───────────────────────────


def test_the_worked_example_passes_the_whole_toolkit():
    """validate -> fixtures -> lint, exactly as installation runs them."""
    report = check_pack_directory(EXAMPLE_PACK)
    assert report.ok, report.reasons()


def test_the_worked_example_is_the_pack_the_documentation_describes(manifest):
    walkthrough = doc_text("worked_example.md")
    assert manifest.pack_id in walkthrough
    for declaration in manifest.detectors:
        assert declaration.detector_id in walkthrough or declaration.primitive in walkthrough
    for case_file in sorted((EXAMPLE_PACK / "fixtures").glob("*.json")):
        assert case_file.name in walkthrough


def test_the_worked_example_exercises_several_primitives(manifest):
    """One primitive used four times would teach a partner one thing."""
    assert len({d.primitive for d in manifest.detectors}) >= 4


def test_the_worked_example_teaches_the_negative_case(cases):
    """A detector that fires on everything passes a positive-only suite forever."""
    quiet = [
        case
        for case in cases
        if all(
            expectation.get("fires") is False
            for expectation in (case["expect"].get("detectors") or {}).values()
        )
    ]
    assert len(quiet) >= 2


def test_the_worked_example_declares_the_concepts_it_binds(manifest):
    declared = set(manifest.required_concepts) | set(manifest.optional_concepts)
    for declaration in manifest.detectors:
        assert set(declaration.concepts) <= declared


def test_every_finding_the_example_produces_carries_the_four_parts(findings):
    assert findings, "the worked example produced no findings at all"
    for finding in findings:
        missing = [p for p in FOUR_PART_CONTRACT_FIELDS if not finding.contract.get(p)]
        assert missing == [], f"{finding.detector_id}: missing {missing}"


def test_no_finding_the_example_produces_names_an_individual_or_a_cause(findings):
    for finding in findings:
        assert find_individual_references(dict(finding.contract)) == []
        assert find_causal_language(finding.statement) == []


def test_the_example_shows_both_a_capped_and_a_corroborated_finding(findings):
    """The confidence derivation is the thing partners most often expect to
    control; the example has to show both outcomes of it."""
    levels = {finding.confidence_level for finding in findings}
    assert {"MEDIUM", "HIGH"} <= levels


def test_the_example_requests_a_level_and_never_asserts_one():
    document = json.loads((EXAMPLE_PACK / "pack.json").read_text("utf-8"))
    certification = document.get("certification", {})
    assert certification.get("requestedLevel")
    assert not {"level", "signature", "certifyingEntity"} & set(certification)


# ── ...and installs through the C1/C2 gates (AC1) ─────────────────────────────


def test_the_worked_example_packages_installs_and_activates(tmp_path, signing, stores):
    """The end of the walkthrough, run for real: package -> verify -> install ->
    activate."""
    bundle = tmp_path / "example_service_desk.aiqpack"
    built = build_bundle(
        EXAMPLE_PACK, bundle, signing_key=signing, key_id="example-partner-2026"
    )
    assert built.ok, built.detail
    assert verify_bundle(bundle).ok

    outcome = install_pack_bundle(
        ORG, bundle.read_bytes(), actor_id=ACTOR, activate=True
    )
    assert outcome.record.status == STATUS_ACTIVE
    assert outcome.compatibility.compatible

    record = set_installed_pack_activation(
        ORG, outcome.record.pack_id, active=True, actor_id=ACTOR
    )
    assert record.status == STATUS_ACTIVE

    config = installed_pack_config(ORG, outcome.record.pack_id)
    assert config["detectors"] == []           # no module paths — it ships no code
    assert config["manifestDetectors"]         # ...but its detectors are all there


def test_a_tampered_worked_example_bundle_is_refused(tmp_path, signing, stores):
    """The documentation shows this failure; it has to be the real one."""
    import zipfile

    from app.pack_installation import REASON_BUNDLE_UNVERIFIED, PackInstallRefused

    original = tmp_path / "original.aiqpack"
    build_bundle(
        EXAMPLE_PACK, original, signing_key=signing, key_id="example-partner-2026"
    )
    tampered = tmp_path / "tampered.aiqpack"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "pack.json":
                payload = payload.replace(
                    b'"min_occurrences": 4', b'"min_occurrences": 1'
                )
            target.writestr(info, payload)

    with pytest.raises(PackInstallRefused) as refusal:
        install_pack_bundle(ORG, tampered.read_bytes(), actor_id=ACTOR)
    assert refusal.value.reason == REASON_BUNDLE_UNVERIFIED
