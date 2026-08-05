"""2.0-C3 T2 (AT-837) — the detector primitive library, DB-free.

Sub-task scope: *the documented, versioned set of composable primitives partners
build from — recurrence, threshold-vs-baseline, ageing, oscillation,
concentration/traversal (depth-bounded), co-occurrence within window — each with
parameter contracts and evidence/corroboration semantics built in, so the
four-part criterion is inherited rather than re-implemented.*

Parent-story criteria this discharges (the primitive-level halves):

  * AC1 — a pack authored with no platform code changes produces findings
    carrying evidence, confidence, corroboration status, and source trace.
  * AC2 (execution half) — a detector can only run a library primitive, bound to
    declared concepts, inside its parameter contract.

The tests concentrate on what would be quietly wrong rather than loudly broken:

  * an author who could ASSERT confidence would make every badge meaningless, so
    the derivation is tested from both directions (single-source caps,
    conversation ceiling, lowering-only pack caps);
  * a join outside its window must contribute NOTHING — a weaker-but-present
    contribution is how coincidence inflates confidence;
  * a primitive reading the wall clock would make the authoring harness
    impossible, so determinism is pinned explicitly.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs.sdk import primitives  # noqa: E402
from discovery.packs.sdk.contract import (  # noqa: E402
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    FOUR_PART_CONTRACT_FIELDS,
    STATUS_CORROBORATED,
    STATUS_SINGLE_SOURCE,
    PackContractViolation,
    derive_confidence,
    enforce_pack_contract,
    find_causal_language,
)
from discovery.packs.sdk.execution import (  # noqa: E402
    run_detector,
    run_manifest,
    to_detector_results,
)
from discovery.packs.sdk.manifest import parse_manifest  # noqa: E402
from discovery.packs.sdk.primitive_library import (  # noqa: E402
    PRIMITIVE_IMPLEMENTATIONS,
    PrimitiveContext,
    PrimitiveExecutionError,
    implemented_primitive_ids,
    run_primitive,
)
from discovery.packs.sdk.signals import (  # noqa: E402
    SignalError,
    concept_record,
    records_from_dicts,
    signal_set,
    signal_set_from_dicts,
)

EXAMPLES = (
    Path(__file__).resolve().parents[2] / "discovery" / "packs" / "sdk" / "examples"
)
AS_OF = datetime(2026, 6, 30, tzinfo=timezone.utc)


def days_before(count: int) -> str:
    return (AS_OF - timedelta(days=count)).isoformat()


def minutes_before(count: int) -> str:
    return (AS_OF - timedelta(minutes=count)).isoformat()


@pytest.fixture()
def example_manifest():
    return parse_manifest(json.loads((EXAMPLES / "example_partner_pack.json").read_text("utf-8")))


@pytest.fixture()
def example_signal():
    return signal_set_from_dicts(
        json.loads((EXAMPLES / "example_partner_signal.json").read_text("utf-8"))
    )


def contract_is_complete(finding) -> bool:
    return all(finding.contract.get(part) for part in FOUR_PART_CONTRACT_FIELDS)


# ── The library and its declaration cannot drift ──────────────────────────────


def test_every_declared_primitive_is_implemented():
    """One vocabulary, two halves — a declared primitive nobody implemented is a
    promise to an author that fails at their customer."""
    assert implemented_primitive_ids() == primitives.primitive_ids()


def test_no_primitive_is_implemented_without_a_declared_contract():
    for primitive_id in PRIMITIVE_IMPLEMENTATIONS:
        spec = primitives.get_primitive(primitive_id)
        assert spec is not None, f"{primitive_id} has no parameter contract"
        assert spec.parameters


def test_running_an_unknown_primitive_is_refused():
    with pytest.raises(PrimitiveExecutionError) as excinfo:
        run_primitive(
            "telepathy",
            detector_id="d",
            title="t",
            concepts=["incident_workflow"],
            parameters={},
            signals=signal_set([]),
        )
    assert "telepathy" in str(excinfo.value)


def test_running_a_primitive_outside_its_concept_arity_is_refused():
    with pytest.raises(PrimitiveExecutionError):
        run_primitive(
            "co_occurrence_window",
            detector_id="d",
            title="t",
            concepts=["incident_workflow"],  # needs exactly two
            parameters={"window_minutes": 60},
            signals=signal_set([]),
        )


def test_missing_required_parameter_is_refused_at_execution():
    """Defence in depth: manifest validation catches this, but a caller that
    bypassed it must not get a primitive running on invented defaults."""
    with pytest.raises(PrimitiveExecutionError):
        run_primitive(
            "recurrence",
            detector_id="d",
            title="t",
            concepts=["incident_workflow"],
            parameters={"window_days": 30},  # min_occurrences missing
            signals=signal_set([]),
        )


# ── Signal admission ──────────────────────────────────────────────────────────


def test_signal_refuses_an_unknown_concept():
    with pytest.raises(SignalError):
        concept_record(concept="telepathy", record_id="1", source_system="servicenow")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attributes": {"assignee": "someone"}},
        {"attributes": {"note": "raised by person@example.test"}},
        {"actor_group": "person@example.test"},
    ],
)
def test_signal_refuses_individual_references_at_admission(kwargs):
    """Checking only at the finding boundary would be too late: a pack could group
    BY an individual and emit a 'group' whose identity is a person."""
    with pytest.raises(SignalError):
        concept_record(
            concept="incident_workflow",
            record_id="INC-1",
            source_system="servicenow",
            **kwargs,
        )


def test_signal_ordering_is_deterministic_regardless_of_fixture_order():
    entries = [
        {"concept": "incident_workflow", "record_id": "b", "source_system": "servicenow", "observed_at": days_before(1)},
        {"concept": "incident_workflow", "record_id": "a", "source_system": "servicenow", "observed_at": days_before(5)},
    ]
    forward = signal_set(records_from_dicts(entries))
    backward = signal_set(records_from_dicts(list(reversed(entries))))
    assert [r.record_id for r in forward.records] == [r.record_id for r in backward.records]


def test_as_of_comes_from_the_data_not_the_clock(example_signal):
    assert example_signal.default_as_of() == AS_OF


# ── recurrence ────────────────────────────────────────────────────────────────


def recurrence_signal(count: int, *, system: str = "servicenow"):
    return signal_set(
        records_from_dicts(
            [
                {
                    "concept": "resolution_signature",
                    "record_id": f"INC-{index}",
                    "source_system": system,
                    "observed_at": days_before(index + 1),
                    "signature": "restart_worker",
                    "actor_group": "ops",
                    "entity_reference": "svc-a",
                }
                for index in range(count)
            ]
        )
    )


def test_recurrence_fires_at_its_threshold_and_not_below():
    params = {"min_occurrences": 4, "window_days": 30, "group_by": "signature"}
    fired = run_primitive(
        "recurrence",
        detector_id="rec",
        title="Recurring manual resolution",
        concepts=["resolution_signature"],
        parameters=params,
        signals=recurrence_signal(4),
        context=PrimitiveContext(as_of=AS_OF),
    )
    assert len(fired) == 1
    assert fired[0].metric_value == 4
    assert fired[0].evidence["occurrences"] == 4

    quiet = run_primitive(
        "recurrence",
        detector_id="rec",
        title="Recurring manual resolution",
        concepts=["resolution_signature"],
        parameters=params,
        signals=recurrence_signal(3),
        context=PrimitiveContext(as_of=AS_OF),
    )
    assert quiet == []


def test_recurrence_ignores_records_outside_its_window():
    signals = signal_set(
        records_from_dicts(
            [
                {"concept": "resolution_signature", "record_id": f"OLD-{i}", "source_system": "servicenow",
                 "observed_at": days_before(200 + i), "signature": "restart_worker"}
                for i in range(6)
            ]
        )
    )
    assert (
        run_primitive(
            "recurrence",
            detector_id="rec",
            title="t",
            concepts=["resolution_signature"],
            parameters={"min_occurrences": 4, "window_days": 30},
            signals=signals,
            context=PrimitiveContext(as_of=AS_OF),
        )
        == []
    )


def test_recurrence_is_reproducible():
    signals = recurrence_signal(6)
    kwargs = dict(
        detector_id="rec",
        title="t",
        concepts=["resolution_signature"],
        parameters={"min_occurrences": 4, "window_days": 30},
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF),
    )
    first = run_primitive("recurrence", **kwargs)
    second = run_primitive("recurrence", **kwargs)
    assert [f.to_dict() for f in first] == [f.to_dict() for f in second]


# ── threshold_vs_baseline ─────────────────────────────────────────────────────


def baseline_signal(current: float, baseline: float, runs: int = 5):
    return signal_set(
        records_from_dicts(
            [
                {
                    "concept": "incident_workflow",
                    "record_id": "Q-1",
                    "source_system": "servicenow",
                    "observed_at": days_before(1),
                    "artifact": "payments-queue",
                    "metrics": {
                        "backlog_depth": current,
                        "backlog_depth_baseline": baseline,
                        "baseline_runs": runs,
                    },
                }
            ]
        )
    )


def run_baseline(signals, **overrides):
    parameters = {"metric": "backlog_depth", "departure_pct": 0.25}
    parameters.update(overrides)
    return run_primitive(
        "threshold_vs_baseline",
        detector_id="baseline",
        title="Backlog above baseline",
        concepts=["incident_workflow"],
        parameters=parameters,
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF),
    )


def test_threshold_vs_baseline_judges_a_subject_against_its_own_baseline():
    findings = run_baseline(baseline_signal(150, 100))
    assert len(findings) == 1
    assert findings[0].evidence["departure_pct"] == 0.5
    assert findings[0].evidence["baseline_scope"] == "per_subject"


def test_threshold_vs_baseline_does_not_fire_within_normal_variation():
    assert run_baseline(baseline_signal(110, 100)) == []


def test_threshold_vs_baseline_requires_an_established_baseline():
    """Unbaselined is not the same as compliant — it must not fire either way."""
    assert run_baseline(baseline_signal(400, 100, runs=1)) == []


def test_threshold_vs_baseline_direction_is_respected():
    assert run_baseline(baseline_signal(40, 100)) == []
    below = run_baseline(baseline_signal(40, 100), direction="below")
    assert len(below) == 1
    assert below[0].evidence["departure_pct"] == -0.6


# ── ageing ────────────────────────────────────────────────────────────────────


def ageing_signal(count: int, age_days: int = 20, state: str = "open"):
    return signal_set(
        records_from_dicts(
            [
                {
                    "concept": "incident_workflow",
                    "record_id": f"AGE-{index}",
                    "source_system": "servicenow",
                    "observed_at": days_before(age_days),
                    "opened_at": days_before(age_days),
                    "entity_reference": "svc-a",
                    "state": state,
                }
                for index in range(count)
            ]
        )
    )


def run_ageing(signals, **overrides):
    parameters = {"min_age_days": 14, "min_items": 3}
    parameters.update(overrides)
    return run_primitive(
        "ageing",
        detector_id="ageing",
        title="Queue ageing",
        concepts=["incident_workflow"],
        parameters=parameters,
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF),
    )


def test_ageing_fires_over_its_floor():
    findings = run_ageing(ageing_signal(4))
    assert len(findings) == 1
    assert findings[0].evidence["aged_items"] == 4
    assert findings[0].evidence["oldest_age_days"] == pytest.approx(20.0)


def test_ageing_respects_its_aggregation_floor():
    """One aged item is a record, not a finding."""
    assert run_ageing(ageing_signal(2)) == []


def test_ageing_excludes_resolved_work_by_scope():
    assert run_ageing(ageing_signal(5, state="closed")) == []
    assert len(run_ageing(ageing_signal(5, state="closed"), state_scope="any")) == 1


def test_ageing_uses_the_injected_as_of():
    signals = ageing_signal(4, age_days=20)
    earlier = run_primitive(
        "ageing",
        detector_id="ageing",
        title="t",
        concepts=["incident_workflow"],
        parameters={"min_age_days": 14, "min_items": 3},
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF - timedelta(days=10)),
    )
    assert earlier == []


# ── oscillation ───────────────────────────────────────────────────────────────


def oscillation_signal(hops: int, participants=("group-a", "group-b")):
    return signal_set(
        records_from_dicts(
            [
                {
                    "concept": "incident_workflow",
                    "record_id": "OSC-1",
                    "source_system": "servicenow",
                    "observed_at": days_before(2),
                    "entity_reference": "svc-a",
                    "transitions": [
                        {
                            "kind": "assignment",
                            "at": days_before(3),
                            "participant": participants[index % len(participants)],
                        }
                        for index in range(hops)
                    ],
                }
            ]
        )
    )


def test_oscillation_fires_on_repeated_group_level_hops():
    findings = run_primitive(
        "oscillation",
        detector_id="osc",
        title="Reassignment ping-pong",
        concepts=["incident_workflow"],
        parameters={"min_hops": 3},
        signals=oscillation_signal(4),
        context=PrimitiveContext(as_of=AS_OF),
    )
    assert len(findings) == 1
    assert findings[0].evidence["max_hops"] == 4
    assert findings[0].evidence["participants"] == ["group-a", "group-b"]


def test_oscillation_below_the_hop_threshold_is_quiet():
    assert (
        run_primitive(
            "oscillation",
            detector_id="osc",
            title="t",
            concepts=["incident_workflow"],
            parameters={"min_hops": 3},
            signals=oscillation_signal(2),
            context=PrimitiveContext(as_of=AS_OF),
        )
        == []
    )


def test_oscillation_within_one_group_is_not_ping_pong():
    assert (
        run_primitive(
            "oscillation",
            detector_id="osc",
            title="t",
            concepts=["incident_workflow"],
            parameters={"min_hops": 3},
            signals=oscillation_signal(4, participants=("group-a",)),
            context=PrimitiveContext(as_of=AS_OF),
        )
        == []
    )


# ── concentration_traversal ───────────────────────────────────────────────────


def concentration_signal(dependents=("svc-a", "svc-b", "svc-c"), depth_chain=False):
    records = [
        {"concept": "incident_workflow", "record_id": "ANCH-1", "source_system": "servicenow",
         "observed_at": days_before(3), "entity_reference": "db-core"},
    ]
    for index, service in enumerate(dependents):
        records.append(
            {"concept": "incident_workflow", "record_id": f"DEP-{index}", "source_system": "servicenow",
             "observed_at": days_before(index + 1), "entity_reference": service}
        )
    edges = {service: ["db-core"] for service in dependents}
    if depth_chain:
        # svc-far depends on svc-a, which depends on db-core: two hops away.
        records.append(
            {"concept": "incident_workflow", "record_id": "DEP-FAR", "source_system": "servicenow",
             "observed_at": days_before(1), "entity_reference": "svc-far"}
        )
        edges["svc-far"] = [dependents[0]]
    return signal_set(records_from_dicts(records), dependency_edges=edges)


def run_concentration(signals, **overrides):
    parameters = {"max_depth": 2, "min_dependents": 3}
    parameters.update(overrides)
    return run_primitive(
        "concentration_traversal",
        detector_id="conc",
        title="Shared dependency concentration",
        concepts=["incident_workflow"],
        parameters=parameters,
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF),
    )


def test_concentration_fires_on_a_shared_dependency():
    findings = run_concentration(concentration_signal())
    assert len(findings) == 1
    assert findings[0].subject == "db-core"
    assert findings[0].evidence["dependents"] == ["svc-a", "svc-b", "svc-c"]


def test_concentration_wording_is_never_causal():
    """Causality is the causal engine's; a pack states concentration only."""
    finding = run_concentration(concentration_signal())[0]
    assert find_causal_language(finding.statement) == []
    assert "concentrates on" in finding.statement
    assert finding.evidence["relationship"] == "concentration"


def test_concentration_traversal_is_depth_bounded():
    """A dependent two hops out counts at depth 2 and is invisible at depth 1."""
    signals = concentration_signal(depth_chain=True)
    deep = run_concentration(signals, max_depth=2)[0]
    shallow = run_concentration(signals, max_depth=1)[0]
    assert "svc-far" in deep.evidence["dependents"]
    assert "svc-far" not in shallow.evidence["dependents"]


def test_concentration_below_the_dependent_floor_is_quiet():
    assert run_concentration(concentration_signal(dependents=("svc-a", "svc-b"))) == []


def test_concentration_can_require_corroboration():
    single_source = concentration_signal()
    assert run_concentration(single_source, require_corroboration=True) == []


# ── co_occurrence_window ──────────────────────────────────────────────────────


def co_occurrence_signal(gap_minutes: int, pairs: int = 3):
    records = []
    for index in range(pairs):
        anchor = 60 * 24 * (index + 1)
        records.append(
            {"concept": "operational_event", "record_id": f"ALM-{index}", "source_system": "aws",
             "observed_at": minutes_before(anchor), "entity_reference": "svc-a"}
        )
        records.append(
            {"concept": "incident_workflow", "record_id": f"INC-{index}", "source_system": "servicenow",
             "observed_at": minutes_before(anchor - gap_minutes), "entity_reference": "svc-a"}
        )
    return signal_set(records_from_dicts(records))


def run_co_occurrence(signals, **overrides):
    parameters = {"window_minutes": 120, "min_pairs": 3}
    parameters.update(overrides)
    return run_primitive(
        "co_occurrence_window",
        detector_id="cooc",
        title="Alert pairs with incident",
        concepts=["operational_event", "incident_workflow"],
        parameters=parameters,
        signals=signals,
        context=PrimitiveContext(as_of=AS_OF),
    )


def test_co_occurrence_fires_inside_the_window():
    findings = run_co_occurrence(co_occurrence_signal(30))
    assert len(findings) == 1
    assert findings[0].evidence["pair_count"] == 3
    assert findings[0].evidence["within_window"] is True


def test_a_join_outside_the_window_contributes_nothing():
    """Not a weaker signal — nothing. Coincidence must never inflate confidence."""
    assert run_co_occurrence(co_occurrence_signal(600)) == []


def test_co_occurrence_records_the_join_and_window_on_the_claim():
    finding = run_co_occurrence(co_occurrence_signal(30))[0]
    assert finding.contract["corroboration"]["window_gated"] is True
    assert finding.evidence["join_type"] == "co_occurrence"
    assert finding.evidence["window_minutes"] == 120


def test_co_occurrence_ordering_is_respected():
    """Reversed ordering: the incident precedes the alert, so an ordered join
    finds nothing while an unordered one still pairs them."""
    signals = co_occurrence_signal(-30)
    assert run_co_occurrence(signals, ordering="first_before_second") == []
    assert len(run_co_occurrence(signals, ordering="either")) == 1


# ── Inherited confidence and corroboration ────────────────────────────────────


def test_single_source_is_capped_and_labelled():
    derived = derive_confidence(["servicenow"])
    assert derived["confidence"]["level"] == CONFIDENCE_MEDIUM
    assert derived["confidence"]["capped"] is True
    assert derived["corroboration"]["status"] == STATUS_SINGLE_SOURCE


def test_two_independent_sources_reach_high():
    derived = derive_confidence(["servicenow", "aws"])
    assert derived["confidence"]["level"] == CONFIDENCE_HIGH
    assert derived["corroboration"]["status"] == STATUS_CORROBORATED


@pytest.mark.parametrize("systems", [["slack"], ["slack", "teams"]])
def test_conversation_sources_never_reach_high(systems):
    """The standing ceiling, inherited by every authored pack."""
    derived = derive_confidence(systems)
    assert derived["confidence"]["level"] == CONFIDENCE_MEDIUM
    assert derived["confidence"]["capped"] is True


def test_conversation_plus_a_record_source_is_corroborated_but_still_medium():
    derived = derive_confidence(["servicenow", "slack"])
    assert derived["corroboration"]["status"] == STATUS_CORROBORATED
    assert derived["confidence"]["level"] == CONFIDENCE_MEDIUM


def test_a_pack_cap_can_only_lower_the_derived_level():
    lowered = derive_confidence(
        ["servicenow", "aws"], caps={"corroboratedMax": CONFIDENCE_MEDIUM}
    )
    assert lowered["confidence"]["level"] == CONFIDENCE_MEDIUM
    single_low = derive_confidence(["servicenow"], caps={"singleSourceCap": CONFIDENCE_LOW})
    assert single_low["confidence"]["level"] == CONFIDENCE_LOW


def test_derived_confidence_reaches_findings(example_manifest, example_signal):
    result = run_manifest(example_manifest, example_signal)
    by_id = {f.detector_id: f for f in result.findings}
    assert by_id["service_desk_queue_ageing"].confidence_level == CONFIDENCE_MEDIUM
    assert by_id["alert_to_incident_pairing"].confidence_level == CONFIDENCE_HIGH


# ── The four-part contract is inherited, on every finding ─────────────────────


def test_the_worked_example_fires_every_detector(example_manifest, example_signal):
    result = run_manifest(example_manifest, example_signal)
    assert [outcome.detector_id for outcome in result.outcomes if outcome.fired] == [
        "repeated_manual_resolution",
        "service_desk_queue_ageing",
        "shared_dependency_concentration",
        "alert_to_incident_pairing",
    ]


def test_every_finding_carries_all_four_parts(example_manifest, example_signal):
    result = run_manifest(example_manifest, example_signal)
    assert result.findings
    for finding in result.findings:
        assert contract_is_complete(finding), finding.detector_id
        assert finding.contract["source_trace"]["systems"]
        assert finding.contract["source_trace"]["artifacts"]
        enforce_pack_contract(finding.contract, detector_id=finding.detector_id)


def test_source_trace_resolves_to_the_contributing_records(example_manifest, example_signal):
    result = run_manifest(example_manifest, example_signal)
    ageing = result.findings_for("service_desk_queue_ageing")[0]
    traced = {artifact["id"] for artifact in ageing.contract["source_trace"]["artifacts"]}
    assert {"INC-4001", "INC-4002", "INC-4003", "INC-4004", "INC-4005"} <= traced


def test_execution_is_reproducible(example_manifest, example_signal):
    first = run_manifest(example_manifest, example_signal).to_dict()
    second = run_manifest(example_manifest, example_signal).to_dict()
    assert first == second


def test_a_disabled_detector_is_reported_not_omitted(example_manifest, example_signal):
    """An author debugging a fixture must be able to tell 'did not fire' from
    'was not run'."""
    document = json.loads((EXAMPLES / "example_partner_pack.json").read_text("utf-8"))
    document["detectors"][0]["enabledByDefault"] = False
    manifest = parse_manifest(document)
    outcome = run_manifest(manifest, example_signal).outcomes[0]
    assert outcome.fired is False
    assert "enabledByDefault" in outcome.skipped_reason


def test_a_single_detector_can_be_run_on_its_own(example_manifest, example_signal):
    """The harness (2.0-C3 §3) asserts one detector at a time against seeded signal."""
    declaration = example_manifest.detector("service_desk_queue_ageing")
    outcome = run_detector(
        declaration, example_signal, context=PrimitiveContext(as_of=AS_OF)
    )
    assert outcome.fired is True
    assert outcome.primitive == "ageing"
    assert all(contract_is_complete(finding) for finding in outcome.findings)


def test_the_boundary_refuses_an_incomplete_contract():
    with pytest.raises(PackContractViolation):
        enforce_pack_contract({"evidence": {"count": 1}}, detector_id="broken")


def test_the_boundary_refuses_a_contract_naming_an_individual():
    with pytest.raises(PackContractViolation):
        enforce_pack_contract(
            {
                "evidence": {"count": 1, "assignee": "someone"},
                "confidence": {"level": CONFIDENCE_MEDIUM},
                "corroboration": {"status": STATUS_SINGLE_SOURCE},
                "source_trace": {"systems": ["servicenow"], "artifacts": [{"id": "1"}]},
            },
            detector_id="leaky",
        )


def test_no_finding_names_an_individual(example_manifest, example_signal):
    from discovery.packs.cloud_ops_finding import find_individual_references

    for finding in run_manifest(example_manifest, example_signal).findings:
        assert find_individual_references(finding.contract) == []


# ── Pipeline adapter ──────────────────────────────────────────────────────────


def test_findings_adapt_to_pipeline_detector_results(example_manifest, example_signal):
    result = run_manifest(example_manifest, example_signal)
    adapted = to_detector_results(result)
    assert len(adapted) == len(result.findings)
    first = adapted[0]
    assert first.provenance_type == "observed"
    assert first.raw_evidence["packId"] == example_manifest.pack_id
    assert first.raw_evidence["packVersion"] == example_manifest.pack_version
    assert first.raw_evidence["finding_contract"]["confidence"]["level"]


# ── Structural discipline ─────────────────────────────────────────────────────


def test_importing_the_sdk_does_not_import_app():
    """Authoring tooling runs offline; only the pipeline adapter may touch app."""
    import subprocess
    import sys

    probe = (
        "import sys; import discovery.packs.sdk as sdk; "
        "print(any(m == 'app' or m.startswith('app.') for m in sys.modules))"
    )
    output = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert output.stdout.strip() == "False", output.stderr


def test_the_primitive_library_never_reads_the_wall_clock():
    """A primitive reading now() would produce a different finding every day from
    the same fixture — the failure mode the harness cannot tolerate."""
    source = (
        Path(__file__).resolve().parents[2]
        / "discovery" / "packs" / "sdk" / "primitive_library.py"
    ).read_text("utf-8")
    assert "datetime.now" not in source
    assert "utcnow" not in source
