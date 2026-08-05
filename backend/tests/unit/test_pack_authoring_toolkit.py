"""2.0-C3 T3 (AT-838) — the authoring toolkit, DB-free.

Sub-task scope: *local scaffold, schema validation, a fixture-based test harness
(author supplies seeded signal → asserts findings), and a lint pass covering the
platform's non-negotiables (no individual naming, causal-gate wording,
aggregation floors, evidence completeness).*

Parent-story criteria this discharges:

  * AC3 — lint catches each non-negotiable violation, one seeded case each.
  * AC5 (author-side half) — validation runs manifest schema + author fixtures,
    and a failing pack reports SPECIFIC failures.
  * AC1 (author-side half) — a pack authored with the toolkit and no platform
    code changes produces findings carrying all four parts.

The load-bearing test in this file is
``test_a_scaffolded_pack_passes_the_whole_toolkit``: a scaffold whose own output
fails its own tooling teaches authors that the errors are noise, and from then on
they read past every real one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs.sdk.harness import (  # noqa: E402
    FIXTURES_DIRNAME,
    PACK_MANIFEST_FILENAME,
    HarnessError,
    load_cases,
    run_case,
    run_pack_directory,
    validate_case,
)
from discovery.packs.sdk.lint import (  # noqa: E402
    FLOOR_PARAMETERS,
    LINT_RULES,
    RULE_AGGREGATION_FLOOR,
    RULE_CAUSAL_WORDING,
    RULE_INCOMPLETE_EVIDENCE,
    RULE_INDIVIDUAL_NAMING,
    SEVERITY_ERROR,
    lint_pack,
    lint_rule_reference,
)
from discovery.packs.sdk.manifest import parse_manifest  # noqa: E402
from discovery.packs.sdk.scaffold import (  # noqa: E402
    ScaffoldError,
    build_case_documents,
    build_manifest_document,
    scaffold_pack,
)
from discovery.packs.sdk.toolkit import (  # noqa: E402
    check_manifest_document,
    check_pack_directory,
)

EXAMPLES = (
    Path(__file__).resolve().parents[2] / "discovery" / "packs" / "sdk" / "examples"
)


@pytest.fixture()
def example_document():
    return json.loads((EXAMPLES / "example_partner_pack.json").read_text("utf-8"))


@pytest.fixture()
def example_manifest(example_document):
    return parse_manifest(example_document)


@pytest.fixture()
def scaffolded(tmp_path):
    scaffold_pack(
        tmp_path / "acme_pack",
        pack_id="acme_service_desk",
        pack_name="Acme Service Desk",
        author_name="Acme Ltd",
        author_contact="packs@acme.test",
    )
    return tmp_path / "acme_pack"


def rules_in(report) -> set:
    return {finding.rule for finding in report.findings}


# ── Scaffold ──────────────────────────────────────────────────────────────────


def test_scaffold_writes_a_complete_pack_project(scaffolded):
    assert (scaffolded / PACK_MANIFEST_FILENAME).is_file()
    assert (scaffolded / "README.md").is_file()
    cases = sorted(path.name for path in (scaffolded / FIXTURES_DIRNAME).glob("*.json"))
    assert len(cases) == 3


def test_a_scaffolded_pack_passes_the_whole_toolkit(scaffolded):
    """The property that makes a scaffold worth having."""
    report = check_pack_directory(scaffolded)
    assert report.ok, report.reasons()
    assert report.harness.passed
    assert report.lint.ok
    assert report.manifest.pack_id == "acme_service_desk"


def test_scaffold_includes_a_negative_case(scaffolded):
    """A detector that fires on everything passes a positive-only suite forever."""
    cases = load_cases(scaffolded / FIXTURES_DIRNAME)
    quiet = [
        case
        for case in cases
        if all(
            expectation.get("fires") is False
            for expectation in case["expect"]["detectors"].values()
        )
    ]
    assert quiet, "scaffold must teach the quiet case"


def test_scaffolded_fixtures_actually_exercise_both_detectors(scaffolded):
    """Guards against a suite that passes because nothing ever fires."""
    result = run_pack_directory(scaffolded)
    fired = {finding.detector_id for finding in result.findings}
    assert fired == {"repeated_work_item", "queue_ageing"}


def test_scaffold_derives_its_platform_floor_from_its_concepts():
    document = build_manifest_document(
        pack_id="probe_pack",
        pack_name="Probe Pack",
        author_name="Probe Ltd",
        author_contact="probe@example.test",
        concept="operational_event",  # introduced in 1.9.0
    )
    assert document["compatibility"]["minPlatformVersion"] == "1.9.0"
    assert parse_manifest(document).pack_id == "probe_pack"


def test_scaffold_refuses_to_overwrite_without_force(scaffolded):
    """Silently replacing an author's manifest would be the worst bug here."""
    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_pack(scaffolded, pack_id="acme_service_desk")
    assert PACK_MANIFEST_FILENAME in str(excinfo.value)
    scaffold_pack(scaffolded, pack_id="acme_service_desk", force=True)


def test_scaffold_requires_a_pack_id(tmp_path):
    with pytest.raises(ScaffoldError):
        scaffold_pack(tmp_path / "x", pack_id="  ")


def test_scaffold_fixtures_are_deterministic():
    assert build_case_documents() == build_case_documents()


# ── Harness ───────────────────────────────────────────────────────────────────


def signal_case(name="recurring restarts"):
    """A case whose signal fires the example pack's recurrence detector.

    Callers set ``expect`` themselves — the point of most of these tests is what
    the harness does with an expectation, not with the signal.
    """
    return {
        "name": name,
        "signal": {
            "records": [
                {
                    "concept": "resolution_signature",
                    "record_id": f"INC-{index}",
                    "source_system": "servicenow",
                    "observed_at": f"2026-06-{index + 10:02d}T09:00:00Z",
                    "signature": "restart_payment_worker",
                    "entity_reference": "svc-payments",
                }
                for index in range(6)
            ]
        },
        "expect": {},
    }


def test_harness_passes_when_expectations_hold(example_manifest):
    case = signal_case()
    case["expect"] = {
        "detectors": {
            "repeated_manual_resolution": {
                "fires": True,
                "findingCount": 1,
                "subjects": ["restart_payment_worker"],
                "minMetric": 6,
                "confidence": "MEDIUM",
                "corroboration": "single_source",
            }
        }
    }
    result = run_case(example_manifest, case)
    assert result.passed, result.failures


def test_harness_reports_the_specific_expectation_that_failed(example_manifest):
    case = signal_case()
    case["expect"] = {
        "detectors": {"repeated_manual_resolution": {"fires": True, "findingCount": 4}}
    }
    result = run_case(example_manifest, case)
    assert not result.passed
    assert any("expected 4 finding(s), got 1" in failure for failure in result.failures)


def test_harness_reports_every_failure_not_just_the_first(example_manifest):
    case = signal_case()
    case["expect"] = {
        "detectors": {
            "repeated_manual_resolution": {
                "findingCount": 9,
                "subjects": ["something-else"],
                "confidence": "HIGH",
            }
        }
    }
    result = run_case(example_manifest, case)
    assert len(result.failures) >= 3


def test_a_typod_detector_id_fails_the_case(example_manifest):
    """Otherwise the case silently asserts nothing and passes forever."""
    case = signal_case()
    case["expect"] = {"detectors": {"repeated_manual_resolutionn": {"fires": True}}}
    result = run_case(example_manifest, case)
    assert not result.passed
    assert any("does not declare" in failure for failure in result.failures)


def test_a_case_expecting_no_fire_passes_on_thin_signal(example_manifest):
    case = {
        "name": "thin",
        "signal": {
            "records": [
                {
                    "concept": "resolution_signature",
                    "record_id": "INC-1",
                    "source_system": "servicenow",
                    "observed_at": "2026-06-10T09:00:00Z",
                    "signature": "restart_payment_worker",
                }
            ]
        },
        "expect": {"detectors": {"repeated_manual_resolution": {"fires": False}}},
    }
    assert run_case(example_manifest, case).passed


def test_no_other_detectors_fire_is_enforced(example_manifest, example_document):
    case = signal_case()
    case["expect"] = {
        "detectors": {"repeated_manual_resolution": {"fires": True}},
        "noOtherDetectorsFire": True,
    }
    assert run_case(example_manifest, case).passed


def test_a_case_with_no_expectation_is_refused():
    problems = validate_case({"name": "x", "signal": {"records": [{}]}, "expect": {}})
    assert any("asserts nothing" in problem for problem in problems)


def test_unknown_case_fields_are_refused():
    problems = validate_case(
        {
            "name": "x",
            "signal": {"records": [{}]},
            "expect": {"detectors": {}, "typo": 1},
        }
    )
    assert any("case.expect.typo" in problem for problem in problems)


def test_signal_rejected_at_admission_reads_as_a_case_failure(example_manifest):
    """An author's most common early mistake must not surface as a traceback."""
    case = {
        "name": "individual in the signal",
        "signal": {
            "records": [
                {
                    "concept": "incident_workflow",
                    "record_id": "INC-1",
                    "source_system": "servicenow",
                    "observed_at": "2026-06-10T09:00:00Z",
                    "attributes": {"assignee": "someone"},
                }
            ]
        },
        "expect": {"detectors": {"service_desk_queue_ageing": {"fires": False}}},
    }
    result = run_case(example_manifest, case)
    assert not result.passed
    assert any("signal rejected" in failure for failure in result.failures)


def test_a_pinned_as_of_is_used(example_manifest):
    case = signal_case()
    case["asOf"] = "2027-01-01T00:00:00Z"  # far outside the 30-day window
    case["expect"] = {"detectors": {"repeated_manual_resolution": {"fires": False}}}
    assert run_case(example_manifest, case).passed


def test_an_unparseable_as_of_fails_the_case(example_manifest):
    case = signal_case()
    case["asOf"] = "yesterday"
    case["expect"] = {"detectors": {"repeated_manual_resolution": {"fires": True}}}
    result = run_case(example_manifest, case)
    assert not result.passed
    assert any("asOf" in failure for failure in result.failures)


def test_a_fixtures_directory_with_no_cases_is_refused(tmp_path):
    (tmp_path / FIXTURES_DIRNAME).mkdir()
    with pytest.raises(HarnessError) as excinfo:
        load_cases(tmp_path / FIXTURES_DIRNAME)
    assert "no test cases" in str(excinfo.value)


def test_malformed_case_json_reports_the_file_and_reason(tmp_path):
    fixtures = tmp_path / FIXTURES_DIRNAME
    fixtures.mkdir()
    (fixtures / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(HarnessError) as excinfo:
        load_cases(fixtures)
    assert "broken.json" in str(excinfo.value)


# ── Lint: one seeded case per non-negotiable (AC3) ────────────────────────────


def test_lint_is_clean_on_the_worked_example(example_manifest):
    report = lint_pack(example_manifest)
    assert report.ok, [finding.to_dict() for finding in report.findings]


def test_lint_catches_individual_naming(example_document):
    example_document["detectors"][0]["labels"]["summary"] = (
        "Shows which assignee resolved the most incidents."
    )
    report = lint_pack(parse_manifest(example_document))
    assert RULE_INDIVIDUAL_NAMING in rules_in(report)
    assert not report.ok


def test_lint_catches_causal_wording(example_document):
    example_document["detectors"][2]["labels"]["summary"] = (
        "Incidents across these services are caused by the shared dependency."
    )
    report = lint_pack(parse_manifest(example_document))
    assert RULE_CAUSAL_WORDING in rules_in(report)


def test_lint_catches_a_missing_aggregation_floor(example_document):
    example_document["detectors"][1]["parameters"]["min_items"] = 1
    report = lint_pack(parse_manifest(example_document))
    assert RULE_AGGREGATION_FLOOR in rules_in(report)
    assert any("single record" in finding.message for finding in report.findings)


def test_lint_catches_incomplete_evidence(example_document):
    del example_document["detectors"][1]["labels"]["summary"]
    report = lint_pack(parse_manifest(example_document))
    assert RULE_INCOMPLETE_EVIDENCE in rules_in(report)


def test_lint_catches_all_four_non_negotiables_at_once(example_document):
    example_document["detectors"][0]["labels"]["summary"] = "Ranked by assignee."
    example_document["detectors"][1]["parameters"]["min_items"] = 1
    del example_document["detectors"][1]["labels"]["summary"]
    example_document["terminology"]["llmContext"] = "Queues age because of the backlog."
    report = lint_pack(parse_manifest(example_document))
    assert rules_in(report) == set(LINT_RULES)


def test_lint_does_not_fire_on_the_platforms_own_guardrail_sentence(example_document):
    """'Humans remain responsible for every action' is accountability, not a causal
    claim — and it is the sentence every first-party pack carries."""
    example_document["terminology"]["llmContext"] = (
        "Describe concentration as observed. Humans remain responsible for every action."
    )
    assert lint_pack(parse_manifest(example_document)).ok


def test_lint_still_fires_on_real_causation_beside_a_guardrail_sentence(example_document):
    example_document["terminology"]["llmContext"] = (
        "The backlog is caused by the shared dependency. Humans remain responsible "
        "for every action."
    )
    report = lint_pack(parse_manifest(example_document))
    assert RULE_CAUSAL_WORDING in rules_in(report)


def test_lint_runtime_leg_reads_emitted_findings(example_manifest):
    """A finding can leak through a subject the manifest never mentions."""

    class FakeFinding:
        detector_id = "leaky"
        statement = "Incidents recur because of the queue owner."
        contract = {
            "evidence": {"count": 3, "assignee": "someone"},
            "confidence": {"level": "MEDIUM"},
            "corroboration": {"status": "single_source"},
            "source_trace": {"systems": ["servicenow"], "artifacts": [{"id": "1"}]},
        }

    report = lint_pack(example_manifest, [FakeFinding()])
    assert {RULE_INDIVIDUAL_NAMING, RULE_CAUSAL_WORDING} <= rules_in(report)


def test_lint_runtime_leg_catches_an_incomplete_contract(example_manifest):
    class Truncated:
        detector_id = "truncated"
        statement = "Work recurs."
        contract = {"evidence": {"count": 3}}

    report = lint_pack(example_manifest, [Truncated()])
    assert RULE_INCOMPLETE_EVIDENCE in rules_in(report)


def test_every_floor_parameter_belongs_to_its_primitive():
    """A floor naming a parameter the primitive does not have would never fire."""
    from discovery.packs.sdk.primitives import get_primitive

    for primitive_id, (parameter, _minimum) in FLOOR_PARAMETERS.items():
        spec = get_primitive(primitive_id)
        assert spec is not None, primitive_id
        assert spec.parameter(parameter) is not None, f"{primitive_id}.{parameter}"


def test_lint_rule_reference_is_serialisable_and_complete():
    reference = lint_rule_reference()
    json.dumps(reference)
    assert {entry["rule"] for entry in reference["rules"]} == set(LINT_RULES)
    assert set(reference["aggregationFloors"]) == set(FLOOR_PARAMETERS)


def test_lint_findings_are_all_actionable():
    """Every finding names a path and a reason — 'invalid pack' helps nobody."""
    document = json.loads((EXAMPLES / "example_partner_pack.json").read_text("utf-8"))
    document["detectors"][0]["labels"]["summary"] = "Grouped by assignee."
    for finding in lint_pack(parse_manifest(document)).findings:
        assert finding.path and finding.message
        assert finding.severity in (SEVERITY_ERROR, "warning")


# ── The combined check (what installation will run) ───────────────────────────


def test_check_reports_specific_reasons_for_a_failing_pack(example_document):
    example_document["detectors"][1]["parameters"]["min_items"] = 1
    report = check_manifest_document(example_document, [])
    assert not report.ok
    assert any("min_items" in reason for reason in report.reasons())


def test_check_stops_at_validation_rather_than_cascading(example_document):
    """A malformed document cannot be linted or run; downstream noise would bury
    the actual error."""
    example_document["detectors"][0]["primitive"] = "telepathy"
    report = check_manifest_document(example_document, [])
    assert not report.ok
    assert report.manifest is None
    assert report.lint is None and report.harness is None
    assert any("telepathy" in reason for reason in report.reasons())


def test_check_requires_fixtures(tmp_path, example_document):
    pack_dir = tmp_path / "nofixtures"
    pack_dir.mkdir()
    (pack_dir / PACK_MANIFEST_FILENAME).write_text(
        json.dumps(example_document), encoding="utf-8"
    )
    report = check_pack_directory(pack_dir)
    assert not report.ok
    assert any("fixtures" in reason for reason in report.reasons())
    assert check_pack_directory(pack_dir, require_fixtures=False).ok


def test_check_fails_when_a_fixture_fails(scaffolded):
    case_path = scaffolded / FIXTURES_DIRNAME / "01_recurring_work_fires.json"
    case = json.loads(case_path.read_text("utf-8"))
    case["expect"]["detectors"]["repeated_work_item"]["findingCount"] = 99
    case_path.write_text(json.dumps(case), encoding="utf-8")
    report = check_pack_directory(scaffolded)
    assert not report.ok
    assert any("fixture" in reason for reason in report.reasons())


def test_check_report_is_json_serialisable(scaffolded):
    json.dumps(check_pack_directory(scaffolded).to_dict())


def test_check_on_a_missing_directory_reports_a_manifest_error(tmp_path):
    report = check_pack_directory(tmp_path / "nope")
    assert not report.ok
    assert report.reasons()


# ── CLI ───────────────────────────────────────────────────────────────────────


def run_cli(*argv) -> int:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import pack_sdk  # noqa: WPS433 - CLI module under test

    return pack_sdk.main(list(argv))


def test_cli_scaffold_then_check_round_trip(tmp_path, capsys):
    target = tmp_path / "cli_pack"
    assert run_cli("scaffold", str(target), "--pack-id", "cli_demo_pack") == 0
    assert run_cli("check", str(target)) == 0
    assert "all pass" in capsys.readouterr().out


def test_cli_check_exits_non_zero_on_a_lint_violation(scaffolded, capsys):
    manifest_path = scaffolded / PACK_MANIFEST_FILENAME
    document = json.loads(manifest_path.read_text("utf-8"))
    document["detectors"][0]["labels"]["summary"] = "Ranked by assignee."
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    assert run_cli("check", str(scaffolded)) == 1
    assert "individual" in capsys.readouterr().out


def test_cli_validate_reports_specific_errors(tmp_path, capsys):
    manifest_path = tmp_path / PACK_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps({"manifestVersion": "nope"}), encoding="utf-8")
    assert run_cli("validate", str(tmp_path)) == 1
    assert "manifestVersion" in capsys.readouterr().out


def test_cli_reference_commands_run(capsys):
    assert run_cli("primitives") == 0
    assert "recurrence" in capsys.readouterr().out
    assert run_cli("rules") == 0
    assert "aggregation" in capsys.readouterr().out.lower()
    assert run_cli("schema") == 0
    assert "manifestVersion" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(scaffolded, capsys):
    assert run_cli("--json", "check", str(scaffolded)) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert payload["ok"] is True
    assert payload["packId"] == "acme_service_desk"
