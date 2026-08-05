"""2.0-B4 T5 (AT-814) — conformance fixture suite + CI gate (AC4).

AC4: *Every connector has conformance fixtures; CI fails if a connector lacks them.*

This file IS that CI gate. It lives under ``tests/contract`` deliberately: CI runs
``pytest tests/contract/`` (see ``.github/workflows/contract-tests.yml``), so a shipped
connector that arrives with no conformance fixture fails the build here. It uses no
database — it is a pure gate that happens to live where CI will actually run it.

What it enforces:
  * **presence** — every shipped connector (``conformance.CONFORMANCE``) has a fixture;
    a missing one fails (the AC4 gate), and an orphan fixture fails too;
  * **the fixture is a true lock on the registry** — its per-concept status/reason/mapper
    match the declaration, so a registry change without a fixture change fails;
  * **gaps and not-applicables carry reasons** (AC5's honesty, re-checked at the fixture);
  * **mapping cases prove the mapper** — where a fixture carries a raw→concept case, the
    named mapper is run and its output must equal the golden, so a fixture "proves the
    mapping is correct" rather than merely asserting a status; and
  * **a `supported` claim cannot exist without a proving case** (forward guard for T2).

A negative control proves the presence gate actually rejects a missing fixture, so the
gate is known to be a gate.
"""
from __future__ import annotations

import json

import pytest

from discovery.concepts.conformance import CONFORMANCE, STATUSES
from discovery.concepts.conformance_fixtures import (
    available_fixture_ids,
    fixture_path,
    load_fixture,
    run_mapping_case,
)
from discovery.concepts.model import CONCEPT_SET

CONNECTORS = sorted(CONFORMANCE)
REASON_REQUIRED = {"gap", "not_applicable"}

# (connector, case_index) pairs for every mapping case across all fixtures — computed
# at collection time so each mapping-case proof is its own parametrised test.
_MAPPING_CASES = [
    (cid, idx)
    for cid in CONNECTORS
    for idx in range(len(load_fixture(cid).get("mapping_cases", [])))
]


# ── AC4 — the presence gate ─────────────────────────────────────────────────────

def test_every_shipped_connector_has_a_conformance_fixture():
    """THE AC4 GATE. A shipped connector with no conformance fixture fails CI."""
    missing = sorted(set(CONFORMANCE) - available_fixture_ids())
    assert missing == [], (
        "shipped connectors are missing a conformance fixture — a connector ships "
        f"with its conformance fixtures or does not ship (2.0-B4 AC4): {missing}"
    )


def test_no_orphan_conformance_fixtures():
    """A fixture with no matching shipped-connector declaration is dead or misnamed."""
    orphans = sorted(available_fixture_ids() - set(CONFORMANCE))
    assert orphans == [], (
        f"conformance fixtures exist for connectors not in the registry: {orphans}"
    )


# ── The fixture is a true lock on the registry ──────────────────────────────────

@pytest.mark.parametrize("cid", CONNECTORS)
def test_fixture_is_wellformed(cid):
    fx = load_fixture(cid)
    assert fx["connector_id"] == cid
    assert fx["source_family"] == CONFORMANCE[cid].source_family
    assert fx["concept_set_version"] == CONFORMANCE[cid].concept_set_version
    assert isinstance(fx.get("mapping_cases"), list)


@pytest.mark.parametrize("cid", CONNECTORS)
def test_fixture_covers_every_concept(cid):
    """A partial declaration is a silent unmapped concept — the exact ambiguity the
    registry (and its fixture) exists to remove."""
    fx = load_fixture(cid)
    assert set(fx["concepts"]) == set(CONCEPT_SET), (
        f"{cid}: fixture concepts != the concept set; "
        f"missing={sorted(set(CONCEPT_SET) - set(fx['concepts']))}, "
        f"extra={sorted(set(fx['concepts']) - set(CONCEPT_SET))}"
    )


@pytest.mark.parametrize("cid", CONNECTORS)
def test_fixture_matches_the_registry_declaration(cid):
    """The fixture pins the registry: status, reason and mapper for every concept must
    match, so a conformance change cannot ship without updating the golden fixture."""
    fx = load_fixture(cid)
    decl = CONFORMANCE[cid]
    for pos in decl.concepts:
        entry = fx["concepts"][pos.concept]
        assert entry["status"] == pos.status, (cid, pos.concept, "status drift")
        assert entry.get("reason", "") == pos.reason, (cid, pos.concept, "reason drift")
        assert entry.get("mapper") == pos.mapper, (cid, pos.concept, "mapper drift")
        assert entry["status"] in STATUSES


@pytest.mark.parametrize("cid", CONNECTORS)
def test_gaps_and_not_applicables_carry_reasons(cid):
    fx = load_fixture(cid)
    for concept, entry in fx["concepts"].items():
        if entry["status"] in REASON_REQUIRED:
            assert entry.get("reason", "").strip(), (
                f"{cid}.{concept}: status {entry['status']!r} needs a reason"
            )


@pytest.mark.parametrize("cid", CONNECTORS)
def test_supported_concepts_are_backed_by_a_mapping_case(cid):
    """A `supported` claim (the only status that asserts conformance) must be proven by
    a mapping case in the same fixture — the strongest claim cannot be made by editing a
    status. (Vacuous today: nothing is `supported` yet; enforced so T2 cannot skip it.)"""
    fx = load_fixture(cid)
    proven = set()
    for case in fx.get("mapping_cases", []):
        proven |= set(case.get("produces", []))
    for concept, entry in fx["concepts"].items():
        if entry["status"] == "supported":
            assert concept in proven, (
                f"{cid}.{concept} is 'supported' but no mapping case proves it"
            )


# ── Mapping cases prove the mapper is correct ───────────────────────────────────

@pytest.mark.parametrize("cid,idx", _MAPPING_CASES)
def test_mapping_case_output_equals_the_golden(cid, idx):
    """Run the named mapper on the case's raw input; its output must equal the golden
    exactly. This is where a fixture PROVES the mapping is correct."""
    case = load_fixture(cid)["mapping_cases"][idx]
    produced = run_mapping_case(case)
    assert produced == case["expected"], (
        f"{cid} mapping case {idx} ({case.get('name')!r}): mapper output != golden "
        f"expected — regenerate the fixture if the mapper changed on purpose"
    )


@pytest.mark.parametrize("cid,idx", _MAPPING_CASES)
def test_mapping_case_produces_valid_concepts_and_names_its_produces(cid, idx):
    case = load_fixture(cid)["mapping_cases"][idx]
    produced = run_mapping_case(case)
    produced_concepts = {d["concept"] for d in produced}
    # Every produced concept is a real concept token...
    assert produced_concepts <= set(CONCEPT_SET)
    # ...and matches what the case says it produces (no undeclared surprise concept).
    assert produced_concepts == set(case.get("produces", [])), (
        f"{cid} case {idx}: produces={case.get('produces')} but mapper emitted "
        f"{sorted(produced_concepts)}"
    )
    # Groups never individuals: an actor_group is an aggregate with no roster.
    for d in produced:
        if d["concept"] == "actor_group":
            assert "member_count" in d
            assert "member_list" not in d and "members" not in d


def test_the_suite_is_not_vacuous_at_least_one_real_mapping_is_proven():
    """If no fixture proved a real raw→concept mapping, the suite would only be
    checking statuses — pin that at least one mapping is actually exercised."""
    assert _MAPPING_CASES, "no mapping cases exist — the fixture suite proves no mapping"


# ── Negative controls — the gate is known to be a gate ──────────────────────────

def test_gate_rejects_a_missing_fixture():
    """Prove the AC4 presence gate fails when a shipped connector lacks a fixture, by
    running its exact logic against a fixture set with one connector removed."""
    victim = CONNECTORS[0]
    simulated_present = available_fixture_ids() - {victim}
    missing = sorted(set(CONFORMANCE) - simulated_present)
    assert victim in missing, "removing a fixture did not trip the presence gate"


def test_gate_rejects_an_orphan_fixture():
    simulated_present = available_fixture_ids() | {"not_a_real_connector"}
    orphans = sorted(simulated_present - set(CONFORMANCE))
    assert orphans == ["not_a_real_connector"]


def test_every_fixture_is_valid_json_on_disk():
    for cid in CONNECTORS:
        path = fixture_path(cid)
        assert path.is_file(), f"{cid}: fixture file missing at {path}"
        json.loads(path.read_text(encoding="utf-8"))
