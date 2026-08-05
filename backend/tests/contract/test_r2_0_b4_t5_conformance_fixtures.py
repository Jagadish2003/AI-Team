"""2.0-B4 T5 (AT-814) — conformance fixture suite + CI gate (AC4).

AC4: *Every connector has conformance fixtures; CI fails if a connector lacks them.*

This file IS that CI gate. It lives under ``tests/contract`` deliberately: CI runs
``pytest tests/contract/`` (see ``.github/workflows/contract-tests.yml``), so a shipped
connector that arrives with no conformance fixture fails the build here. It uses no
database — a pure gate that happens to live where CI will actually run it.

What it enforces:
  * **presence** — every shipped connector (``conformance.CONFORMANCE``) has a fixture;
    a missing one fails (the AC4 gate), and an orphan fixture fails too;
  * **completeness** — a fixture covers all seven concepts (a partial declaration is a
    silently unmapped concept);
  * **the fixture is a true lock on the registry** — its per-concept status/reason/mapper/
    field_gaps match the declaration, so a conformance change cannot ship without updating
    its golden fixture;
  * **gaps and not-applicables carry reasons** (AC5's honesty, re-checked at the fixture);
  * **every `supported` claim resolves to a real mapper** in T2's registry — so the
    strongest claim cannot be made by editing a status. (T2's connector-mapping suite
    proves each mapper's raw→concept output over golden samples; this gate does not
    re-prove it, it pins the per-connector fixture discipline.)

A negative control proves the presence gate actually rejects a missing fixture.
"""
from __future__ import annotations

import json

import pytest

# Importing the mappers package registers every @maps mapper for resolve_mapper.
import discovery.concepts.mappers  # noqa: F401
from discovery.concepts.conformance import CONFORMANCE, STATUS_SUPPORTED, STATUSES
from discovery.concepts.conformance_fixtures import (
    available_fixture_ids,
    fixture_path,
    load_fixture,
)
from discovery.concepts.mappers import resolve_mapper
from discovery.concepts.model import CONCEPT_SET

CONNECTORS = sorted(CONFORMANCE)
REASON_REQUIRED = {"gap", "not_applicable"}


def _registry_field_gaps(pos) -> list:
    return [
        {"field": g.field, "kind": g.kind, "reason": g.reason}
        for g in getattr(pos, "field_gaps", ()) or ()
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


@pytest.mark.parametrize("cid", CONNECTORS)
def test_fixture_covers_every_concept(cid):
    fx = load_fixture(cid)
    assert set(fx["concepts"]) == set(CONCEPT_SET), (
        f"{cid}: fixture concepts != the concept set; "
        f"missing={sorted(set(CONCEPT_SET) - set(fx['concepts']))}, "
        f"extra={sorted(set(fx['concepts']) - set(CONCEPT_SET))}"
    )


@pytest.mark.parametrize("cid", CONNECTORS)
def test_fixture_matches_the_registry_declaration(cid):
    """The fixture pins the registry: status, reason, mapper and field_gaps for every
    concept must match, so a conformance change cannot ship without updating the golden."""
    fx = load_fixture(cid)
    decl = CONFORMANCE[cid]
    for pos in decl.concepts:
        entry = fx["concepts"][pos.concept]
        assert entry["status"] == pos.status, (cid, pos.concept, "status drift")
        assert entry.get("reason", "") == pos.reason, (cid, pos.concept, "reason drift")
        assert entry.get("mapper") == pos.mapper, (cid, pos.concept, "mapper drift")
        assert entry.get("field_gaps", []) == _registry_field_gaps(pos), (
            cid, pos.concept, "field_gaps drift",
        )
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
def test_supported_concepts_name_a_mapper_that_resolves(cid):
    """A `supported` claim (the only status that asserts conformance) must name a mapper
    that resolves to a registered callable in T2's registry — the strongest claim cannot
    be made by editing a status. (T2's connector-mapping suite proves the mapper's
    raw→concept correctness; here we pin that the fixture's claim is backed by real code.)"""
    fx = load_fixture(cid)
    for concept, entry in fx["concepts"].items():
        if entry["status"] == STATUS_SUPPORTED:
            name = entry.get("mapper")
            assert name, f"{cid}.{concept}: 'supported' with no mapper named"
            mapper = resolve_mapper(name)  # raises if it does not resolve
            assert callable(mapper.fn if hasattr(mapper, "fn") else mapper)


def test_the_suite_is_not_vacuous_some_concept_is_supported():
    """If nothing were supported the resolve check would be vacuous — pin that the
    fixtures actually capture supported mappings (T2 flipped many concepts to supported)."""
    supported = [
        (cid, concept)
        for cid in CONNECTORS
        for concept, entry in load_fixture(cid)["concepts"].items()
        if entry["status"] == STATUS_SUPPORTED
    ]
    assert len(supported) >= 10, f"expected many supported concepts, got {len(supported)}"


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
