"""2.0-B4 T6 — acceptance validation across AC1–AC5, and the published vocabulary.

This is the story-level gate, not a re-run of each task's own suite. Every task (T1–T5)
proved its own claim from the inside; T6's job is to check the five ACs hold **together**
from the outside, on the assembled result, and to validate the artifact B4 hands to
2.0-C3.

The distinction matters in one specific way. T1–T5 each import the module they are
testing and assert against it. That catches a broken implementation, but it cannot catch
a claim made in one task that another task's data contradicts — which is exactly the
class of defect this ticket found (T4's docstring advertised four source families
including GitHub, whose `work_item` the registry declares UNSUPPORTED, and whose own
test correctly used three). So the tests here deliberately cross-check task against
task: the registry against the mappers, the fixtures against the registry, the ported
detectors against their originals, the published vocabulary against all of it.

Structure follows the ACs, then the vocabulary:

* AC1 — concept set + contracts documented and versioned; every connector declares.
* AC2 — two ported detectors, identical findings to their originals.
* AC3 — one concept-only detector across ≥3 source families, unmodified.
* AC4 — every connector has conformance fixtures, locked to the registry.
* AC5 — gaps recorded, visible, never approximated.
* T6 — the published vocabulary: complete, honest, pinnable, implementation-free.

AC4's own CI gate lives in ``tests/contract/`` and therefore cannot run without a test
database, though it needs none. The AC4 section below re-validates the same properties
DB-free, so the story's fixture discipline is verifiable in an environment with no
database — and so this suite can actually validate AC4 rather than assert it elsewhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import discovery.concepts.mappers  # noqa: F401 - registers every mapper
from discovery.concepts import conformance as conf
from discovery.concepts import gaps as G
from discovery.concepts import model as M
from discovery.concepts import sdk_vocabulary as V
from discovery.concepts.concept_detectors import (
    DETECTOR_ID as BACKLOG_DETECTOR_ID,
    detect_open_work_item_backlog,
)
from discovery.concepts.conformance_fixtures import (
    available_fixture_ids, fixture_path, load_all_fixtures, load_fixture,
)
from discovery.concepts.contracts import (
    BREAKING_CHANGE_RULES, CONCEPT_SET_VERSION, CONTRACTS, get_contract,
)
from discovery.concepts.mappers import MAPPERS, resolve_mapper

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs" / "normalised_concepts.md"
SAMPLES = (
    REPO_ROOT / "backend" / "discovery" / "tests" / "fixtures"
    / "concept_mapping_samples.json"
)
ORG = "org_t6"


@pytest.fixture(scope="module")
def samples():
    return json.loads(SAMPLES.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════════
# AC1 — "The concept set and its mapping contracts are documented and versioned;
#        each connector declares its conformance."
# ═══════════════════════════════════════════════════════════════════════════════

class TestAC1ConceptSetDocumentedAndVersioned:

    def test_the_set_is_exactly_the_seven_the_story_names(self):
        assert M.CONCEPT_SET == {
            "work_item", "actor_group", "artifact", "state_transition",
            "approval", "assignment", "entity_reference",
        }

    def test_every_concept_has_a_versioned_contract(self):
        assert set(CONTRACTS) == M.CONCEPT_SET
        for concept, contract in CONTRACTS.items():
            assert contract.version >= 1, concept
            assert contract.purpose.strip(), concept
            assert contract.fields, concept

    def test_two_version_levels_exist_and_are_independent(self):
        """A set version and per-contract versions answer different questions: adding a
        concept invalidates no declaration, adding a required field invalidates every
        declaration for that concept. One number could not express both."""
        assert CONCEPT_SET_VERSION >= 1
        assert set(CONTRACTS) == M.CONCEPT_SET
        assert BREAKING_CHANGE_RULES, "the bump rules must be stated, not remembered"

    def test_every_contract_field_exists_on_the_implementing_class(self):
        """The anti-drift property: a contract cannot describe a model that is not there."""
        for concept, contract in CONTRACTS.items():
            klass = M.CONCEPT_CLASSES[concept]
            attrs = set(getattr(klass, "__dataclass_fields__", {}))
            for field in contract.fields:
                assert field.name in attrs, f"{concept}.{field.name} has no model field"

    def test_every_declared_vocabulary_resolves_to_a_closed_set(self):
        for concept, contract in CONTRACTS.items():
            for field in contract.fields:
                if field.vocabulary:
                    value = getattr(M, field.vocabulary, None)
                    assert isinstance(value, frozenset), (
                        f"{concept}.{field.name} names vocabulary {field.vocabulary!r}, "
                        f"which is not a closed set on the model"
                    )

    def test_every_shipped_connector_declares_conformance(self):
        """Anchored on R191-R1's shipped set: a connector whose ingestion does not ship
        cannot declare conformance, and one that does ship cannot have no declaration."""
        from app.connector_roadmap import SHIPPED_CONNECTOR_IDS

        assert set(conf.CONFORMANCE) == set(SHIPPED_CONNECTOR_IDS)

    def test_every_declaration_covers_the_whole_set(self):
        """An omitted concept is silently unmapped — the ambiguity the registry exists
        to remove."""
        for connector_id, decl in conf.CONFORMANCE.items():
            assert {c.concept for c in decl.concepts} == M.CONCEPT_SET, connector_id

    def test_no_declaration_is_stale_against_the_current_set_version(self):
        assert conf.stale_declarations() == ()

    def test_the_set_and_contracts_are_documented(self):
        """AC1 says *documented*. A missing doc fails rather than skips — a skipped
        documentation test is how documentation stops existing."""
        assert DOCS.exists(), f"AC1 requires documentation at {DOCS}"
        text = DOCS.read_text(encoding="utf-8")
        for concept in sorted(M.CONCEPT_SET):
            assert concept in text, f"{concept} is undocumented"
        assert "CONCEPT_SET_VERSION" in text, "the versioning scheme is undocumented"


# ═══════════════════════════════════════════════════════════════════════════════
# AC2 — "Two existing detectors, ported to normalised concepts, produce identical
#        findings on golden fixtures."
# ═══════════════════════════════════════════════════════════════════════════════

class TestAC2PortedDetectorsAreBehaviourIdentical:
    """Validated by calling BOTH sides here, rather than trusting T3's own assertion.

    The concept stream is built with T3's fixture mapper. That is a deliberate seam and
    is recorded rather than hidden: the ORIGINAL detectors consume the aggregate
    ``approval_processes`` block (pre-computed ``avg_delay_days`` /
    ``bottleneck_score``), and no live connector record carries those aggregates, so
    the registered ``ProcessInstance`` mapper cannot produce the input AC2 must compare
    against. Byte-identical findings on the same input is what AC2 asks for; proving it
    through a record-level mapper would be a different (and weaker) claim, because the
    original could not be run on that input at all.
    """

    @pytest.fixture(scope="class")
    def raw(self):
        path = (
            REPO_ROOT / "backend" / "discovery" / "ingest" / "fixtures"
            / "salesforce_sample.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    @pytest.fixture(scope="class")
    def concept_stream(self, raw):
        from tests.unit.test_r2_0_b4_t3_detector_portability import (
            map_service_cloud_approvals,
        )

        return map_service_cloud_approvals(raw, org_id=ORG)

    def test_approval_bottleneck_port_matches_the_original(self, raw, concept_stream):
        from discovery.concepts.portable_detectors import detect_approval_bottleneck
        from discovery.detectors import approval_delay

        assert detect_approval_bottleneck(concept_stream) == approval_delay.detect(raw)

    def test_permission_bottleneck_port_matches_the_original(self, raw, concept_stream):
        from discovery.concepts.portable_detectors import detect_permission_bottleneck
        from discovery.detectors import permission_bottleneck

        assert (
            detect_permission_bottleneck(concept_stream)
            == permission_bottleneck.detect(raw)
        )

    def test_the_comparison_is_not_vacuous(self, raw):
        """Two empty lists are equal. The originals must actually fire on this fixture,
        or AC2 is proven by nothing happening on both sides."""
        from discovery.detectors import approval_delay, permission_bottleneck

        assert approval_delay.detect(raw), "the original approval detector fires nothing"
        assert permission_bottleneck.detect(raw), "the original permission detector fires nothing"

    def test_the_ports_share_their_originals_calibration(self):
        """'Same logic, only the input is normalised' is only true if the thresholds are
        the SAME objects — re-declared constants drift the moment one side is tuned."""
        from discovery.concepts import portable_detectors as port
        from discovery.detectors import approval_delay, permission_bottleneck

        assert port.DELAY_THRESHOLD is approval_delay.DELAY_THRESHOLD
        assert port.BOTTLENECK_THRESHOLD is approval_delay.BOTTLENECK_THRESHOLD
        assert port.SEVERE_DELAY is approval_delay.SEVERE_DELAY
        assert port.PERMISSION_THRESHOLD is permission_bottleneck.THRESHOLD

    def test_the_ports_name_no_connector_and_no_source_field_path(self):
        """A port still reaching into ``sf_data['approval_processes']`` would be a copy,
        not a port."""
        source = (
            Path(__file__).resolve().parents[2]
            / "discovery" / "concepts" / "portable_detectors.py"
        ).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        code = body.split('"""')[-1] if body.count('"""') >= 2 else body
        for forbidden in ("sf_data", "sn_data", "jira_data", "approval_processes"):
            assert forbidden not in code, f"the port still reads {forbidden!r}"

    def test_the_ports_reject_a_raw_connector_dict(self, raw):
        """Handed the shape the ORIGINAL eats, a concept-native detector must find
        nothing — that is what proves it reads concepts and not dicts."""
        from discovery.concepts.portable_detectors import (
            detect_approval_bottleneck, detect_permission_bottleneck,
        )

        assert detect_approval_bottleneck(raw["approval_processes"]) == []
        assert detect_permission_bottleneck(raw["approval_processes"]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# AC3 — "A detector written only against concepts runs across at least three
#        different source families without modification."
# ═══════════════════════════════════════════════════════════════════════════════

class TestAC3ConceptOnlyDetectorCrossesSourceFamilies:

    @pytest.fixture(scope="class")
    def mapped_by_family(self, samples):
        """Work items mapped through the REGISTERED mappers, one family at a time."""
        from discovery.concepts.mappers import jira, salesforce, servicenow

        s = samples
        return {
            "servicenow": [
                servicenow.map_incident_work_item(ORG, s["servicenow"]["incident_open"]),
                servicenow.map_incident_work_item(ORG, s["servicenow"]["incident_cancelled"]),
            ],
            "jira": [
                jira.map_issue_work_item(ORG, s["jira"]["issue_in_progress"]),
                jira.map_issue_work_item(ORG, s["jira"]["issue_done"]),
                jira.map_issue_work_item(ORG, s["jira"]["issue_wont_do"]),
            ],
            "salesforce": [
                salesforce.map_case_work_item(ORG, s["salesforce"]["case_queue_owned"]),
                salesforce.map_case_work_item(ORG, s["salesforce"]["case_person_owned"]),
                salesforce.map_case_work_item(ORG, s["salesforce"]["case_closed"]),
            ],
        }

    def test_at_least_three_distinct_source_families(self, mapped_by_family):
        families = {
            conf.get_conformance(cid).source_family for cid in mapped_by_family
        }
        assert len(families) >= 3, f"AC3 needs three families, got {families}"
        assert families == {"itsm", "engineering_tracker", "crm"}

    def test_the_same_function_fires_for_every_family_unmodified(self, mapped_by_family):
        """One call per family, no arguments varying, no per-family configuration."""
        for family, items in mapped_by_family.items():
            results = detect_open_work_item_backlog(items, min_open=1)
            assert results, f"{family} produced no finding"
            assert all(r.detector_id == BACKLOG_DETECTOR_ID for r in results)

    def test_a_mixed_stream_yields_one_finding_per_family(self, mapped_by_family):
        mixed = [item for items in mapped_by_family.values() for item in items]
        results = detect_open_work_item_backlog(mixed, min_open=1)
        assert sorted(r.signal_source for r in results) == sorted(mapped_by_family)

    def test_the_detector_never_branches_on_a_source_identity(self):
        """The property that makes it portable. `source_system` may be a grouping
        DIMENSION; it may not be a condition."""
        source = (
            Path(__file__).resolve().parents[2]
            / "discovery" / "concepts" / "concept_detectors.py"
        ).read_text(encoding="utf-8")
        code = source.split('"""', 2)[-1]
        for connector_id in conf.CONFORMANCE:
            assert f'"{connector_id}"' not in code, f"branches on {connector_id}"
            assert f"'{connector_id}'" not in code
        assert not re.search(r"source_system\s*==", code)

    def test_the_detector_reads_only_concepts(self, samples):
        """Handed raw connector dicts it must find nothing."""
        assert detect_open_work_item_backlog(
            [samples["servicenow"]["incident_open"]], min_open=1
        ) == []

    def test_every_family_used_here_actually_declares_support(self, mapped_by_family):
        """The cross-check that caught T4's overstated docstring: a family may only be
        counted toward AC3 if the registry says its work_item is SUPPORTED."""
        for connector_id in mapped_by_family:
            position = conf.get_conformance(connector_id).position(M.CONCEPT_WORK_ITEM)
            assert position.conforms, (
                f"{connector_id} is counted as an AC3 family but its work_item is "
                f"{position.status!r} — a family with no mapper cannot be evidence"
            )

    def test_github_is_not_claimed_as_a_work_item_family_anywhere(self):
        """GitHub carries PRs but its `work_item` is `declared`, not `supported`. The
        regression this pins: a docstring or doc advertising it as a fourth family."""
        assert not conf.get_conformance("github").position(M.CONCEPT_WORK_ITEM).conforms
        module = (
            Path(__file__).resolve().parents[2]
            / "discovery" / "concepts" / "concept_detectors.py"
        ).read_text(encoding="utf-8")
        assert "four source families" not in module


# ═══════════════════════════════════════════════════════════════════════════════
# AC4 — "Every connector has conformance fixtures; CI fails if a connector lacks them."
# ═══════════════════════════════════════════════════════════════════════════════

class TestAC4ConformanceFixtures:
    """Re-validated DB-free. The T5 CI gate lives in ``tests/contract/``, whose conftest
    requires a test database at session start even though this gate needs none — so
    without this section AC4 could not be validated in a DB-less environment at all."""

    def test_every_declared_connector_has_a_fixture(self):
        missing = sorted(set(conf.CONFORMANCE) - available_fixture_ids())
        assert missing == [], f"connectors with no conformance fixture: {missing}"

    def test_there_are_no_orphan_fixtures(self):
        """A fixture for a connector nobody declares is a stale golden that will be
        trusted by the next reader."""
        orphans = sorted(available_fixture_ids() - set(conf.CONFORMANCE))
        assert orphans == []

    def test_every_fixture_is_locked_to_the_registry(self):
        """Status, reason, mapper and field-gap count, per concept. A conformance change
        cannot ship without updating its golden."""
        for connector_id, decl in sorted(conf.CONFORMANCE.items()):
            fixture = load_fixture(connector_id)["concepts"]
            assert set(fixture) == M.CONCEPT_SET, connector_id
            for position in decl.concepts:
                locked = fixture[position.concept]
                assert locked["status"] == position.status, f"{connector_id}/{position.concept}"
                assert locked["reason"] == position.reason
                assert locked["mapper"] == position.mapper
                assert len(locked.get("field_gaps") or []) == len(position.field_gaps)

    def test_every_supported_claim_in_every_fixture_resolves_to_a_mapper(self):
        for connector_id, fixture in sorted(load_all_fixtures().items()):
            for concept, locked in fixture["concepts"].items():
                if locked["status"] == conf.STATUS_SUPPORTED:
                    mapper = resolve_mapper(locked["mapper"])
                    assert (mapper.connector_id, mapper.concept) == (connector_id, concept)

    def test_the_fixture_suite_is_not_vacuous(self):
        supported = sum(
            1 for fixture in load_all_fixtures().values()
            for locked in fixture["concepts"].values()
            if locked["status"] == conf.STATUS_SUPPORTED
        )
        assert supported >= 20, f"only {supported} supported claims across all fixtures"

    def test_the_gate_would_reject_a_newly_shipped_connector_with_no_fixture(self):
        """The negative control for AC4's "CI fails if a connector lacks them". Proven by
        construction rather than asserted: a connector id that ships with no fixture is
        detected by the same set difference the gate uses."""
        pretend_shipped = set(conf.CONFORMANCE) | {"brand_new_connector"}
        missing = sorted(pretend_shipped - available_fixture_ids())
        assert missing == ["brand_new_connector"]
        assert not fixture_path("brand_new_connector").exists()

    def test_the_gate_would_reject_a_registry_change_without_a_golden_update(self):
        """The other half of the lock: a status changed in the registry and not in the
        fixture must be caught. Simulated on a copy, so nothing on disk is touched."""
        connector_id = "servicenow"
        fixture = json.loads(json.dumps(load_fixture(connector_id)))
        fixture["concepts"][M.CONCEPT_WORK_ITEM]["status"] = conf.STATUS_DECLARED
        registry_status = (
            conf.get_conformance(connector_id).position(M.CONCEPT_WORK_ITEM).status
        )
        assert fixture["concepts"][M.CONCEPT_WORK_ITEM]["status"] != registry_status


# ═══════════════════════════════════════════════════════════════════════════════
# AC5 — "Unmappable connector concepts are recorded as declared gaps, visible to pack
#        authors — never silently approximated."
# ═══════════════════════════════════════════════════════════════════════════════

class TestAC5GapsRecordedVisibleAndNeverApproximated:

    def test_every_non_supported_position_carries_a_reason(self):
        """Recorded means explained. A bare status is indistinguishable from an oversight."""
        for connector_id, decl in conf.CONFORMANCE.items():
            for position in decl.concepts:
                if not position.conforms:
                    assert position.reason.strip(), (
                        f"{connector_id}/{position.concept} is {position.status!r} with "
                        f"no reason"
                    )

    def test_gaps_are_distinguished_from_deliberate_non_applicability(self):
        """A cloud stream having no approvals is not a shortcoming; an ITSM tool whose
        approvals we cannot read is. Reporting both as gaps produces a backlog nobody
        acts on."""
        report = conf.declared_gaps()
        for connector_id, positions in report.items():
            for position in positions:
                assert position.status == conf.STATUS_GAP
        aws = G.connector_gap_report("aws_events")
        assert aws["gaps"] == []
        assert aws["not_applicable"], "cloud events must record deliberate boundaries"

    def test_field_level_gaps_exist_and_name_real_contract_fields(self):
        found = 0
        for connector_id, decl in conf.CONFORMANCE.items():
            for position in decl.concepts:
                known = {f.name for f in get_contract(position.concept).fields}
                for gap in position.field_gaps:
                    found += 1
                    assert gap.field in known
                    assert gap.reason.strip()
                    assert gap.kind in conf.FIELD_GAP_KINDS
        assert found >= 5, "field-level gaps are the level AC5 actually bites at"

    def test_no_mapper_populates_a_field_declared_absent(self, samples):
        """The runtime teeth, applied across every registered mapper's output."""
        from tests.unit.test_r2_0_b4_t2_connector_mapping import _all_mapped_outputs

        for connector_id, concept, produced in _all_mapped_outputs(samples):
            G.assert_no_approximation(connector_id, concept, produced)

    def test_the_approximation_guard_is_proven_to_fail(self, samples):
        """A guard never observed failing is not known to be a guard."""
        from discovery.concepts.mappers import servicenow

        transition = servicenow.map_state_transition(
            ORG, samples["servicenow"]["audit_state_change"]
        )
        transition.actor_group = M.EntityReference(
            entity_type="team", source_system="servicenow", source_record_id="g1",
        )
        with pytest.raises(G.ApproximationError):
            G.assert_no_approximation("servicenow", M.CONCEPT_STATE_TRANSITION, transition)

    def test_a_gap_is_visible_in_detector_output_not_papered_over(self, samples):
        """End to end: Jira declares an `actor_group` gap, so its work items carry no
        group, and the detector reports them as ungrouped rather than inventing one from
        the assignee's name."""
        from discovery.concepts.mappers import jira

        items = [jira.map_issue_work_item(ORG, samples["jira"]["issue_in_progress"])]
        assert items[0].assigned_group is None
        results = detect_open_work_item_backlog(items, min_open=1)
        assert results
        evidence = results[0].raw_evidence
        assert evidence.get("ungrouped_count", 0) >= 1, (
            "the gap must be countable in the output, not silently absent"
        )
        assert "Sam Rivera" not in json.dumps(evidence)

    def test_gaps_are_reachable_without_reading_the_registry_by_hand(self):
        """'Visible to pack authors' — the inverted, concept-first read."""
        report = G.concept_gap_report()
        assert set(report) == M.CONCEPT_SET
        approval = report[M.CONCEPT_APPROVAL]
        assert approval["usable_connector_ids"] == ["salesforce"]
        for entry in approval["unavailable"]:
            assert entry["reason"].strip()

    def test_every_registered_mapper_is_declared_and_every_claim_has_a_mapper(self):
        """The two-way lock between T2's code and T1's registry."""
        for (connector_id, concept), mapper in MAPPERS.items():
            assert conf.get_conformance(connector_id).position(concept).conforms
        for connector_id, decl in conf.CONFORMANCE.items():
            for position in decl.concepts:
                if position.conforms:
                    resolve_mapper(position.mapper)


# ═══════════════════════════════════════════════════════════════════════════════
# T6 — the published vocabulary 2.0-C3 builds against
# ═══════════════════════════════════════════════════════════════════════════════

class TestPublishedVocabulary:

    @pytest.fixture(scope="class")
    def published(self):
        return V.publish_vocabulary()

    def test_it_publishes_every_concept_with_its_contract(self, published):
        assert set(published["concepts"]) == M.CONCEPT_SET
        for concept, entry in published["concepts"].items():
            contract = get_contract(concept)
            assert entry["contract_version"] == contract.version
            assert set(entry["required_fields"]) == set(contract.required_fields)
            assert len(entry["fields"]) == len(contract.fields)
            assert entry["purpose"].strip()

    def test_it_publishes_the_closed_vocabulary_values(self, published):
        """A partner authoring a declarative manifest must know the allowed tokens; a
        closed set they cannot read is a set they cannot honour."""
        for name, values in published["vocabularies"].items():
            assert values == sorted(getattr(M, name))
        assert published["vocabularies"]["STATUS_CATEGORIES"], "must not be empty"

    def test_it_carries_no_implementation_path(self, published):
        """2.0-C3's governing constraint is that partner packs are declarative
        configuration, not code. Publishing a module path invites the import that
        constraint forbids."""
        blob = json.dumps(published)
        assert "discovery.concepts.mappers" not in blob
        assert "map_incident_work_item" not in blob
        for entry in published["concepts"].values():
            for source in entry["available_from"]:
                assert "mapper" not in source

    def test_availability_is_supported_only(self, published):
        """A vocabulary advertising `declared` concepts sends a partner to write a
        detector that runs, finds nothing, and reports the emptiness as an answer."""
        for concept, ids in published["availability"].items():
            assert list(ids) == list(conf.connectors_supporting(concept))
            for connector_id in ids:
                assert conf.get_conformance(connector_id).position(concept).conforms

    def test_unavailable_sources_are_published_with_their_reason(self, published):
        """Honesty cuts both ways: a partner needs to know a source cannot supply a
        concept, and why, before designing around it."""
        for concept, entry in published["concepts"].items():
            for source in entry["unavailable_from"]:
                assert source["status"] != conf.STATUS_SUPPORTED
                assert source["reason"].strip(), f"{concept}/{source['connector_id']}"

    def test_field_gaps_travel_with_availability(self, published):
        state = published["concepts"][M.CONCEPT_STATE_TRANSITION]
        servicenow = next(
            s for s in state["available_from"] if s["connector_id"] == "servicenow"
        )
        assert "actor_group" in servicenow["fields_never_populated"]
        assignment = published["concepts"][M.CONCEPT_ASSIGNMENT]
        salesforce = next(
            s for s in assignment["available_from"] if s["connector_id"] == "salesforce"
        )
        assert "assigned_to" in salesforce["fields_conditionally_populated"]

    def test_the_digest_is_deterministic(self):
        """Same content, same digest — across calls and across processes, since it is a
        hash of canonical JSON with no clock and no environment in it."""
        assert V.vocabulary_digest() == V.vocabulary_digest()
        assert V.vocabulary_digest(V.publish_vocabulary()) == V.vocabulary_digest()
        assert V.vocabulary_digest().startswith("sha256:")

    def test_the_digest_moves_when_a_partner_visible_thing_changes(self, published):
        """What makes pinning meaningful. A withdrawn availability must be observable."""
        baseline = V.vocabulary_digest(published)
        narrowed = json.loads(json.dumps(published))
        narrowed["availability"][M.CONCEPT_APPROVAL] = []
        assert V.vocabulary_digest(narrowed) != baseline

    def test_it_states_its_stability_promise_and_the_bump_rules(self, published):
        assert published["stability_contract"], "a promise only in a design doc is not a promise"
        assert published["breaking_change_rules"] == list(BREAKING_CHANGE_RULES)
        assert published["vocabulary_version"] == V.VOCABULARY_VERSION
        assert published["concept_set_version"] == CONCEPT_SET_VERSION

    def test_it_records_what_c3_inherits_and_what_it_does_not(self, published):
        """The boundary is stated so the SDK story inherits it rather than assuming it."""
        handoff = published["sdk_handoff"]
        assert handoff["provided_by_b4"]
        assert handoff["not_provided_by_b4"]
        joined = " ".join(handoff["not_provided_by_b4"]).lower()
        assert "primitive" in joined, "the primitive library is C3's, and must say so"

    def test_a_pack_can_ask_which_sources_satisfy_its_requirements(self):
        """The read C1's compatibility check makes on a manifest's behalf."""
        assert V.sources_for_required_concepts(
            M.CONCEPT_WORK_ITEM, M.CONCEPT_ACTOR_GROUP
        ) == ("salesforce", "servicenow")
        assert V.sources_for_required_concepts(M.CONCEPT_APPROVAL) == ("salesforce",)
        with pytest.raises(KeyError, match="not a published concept"):
            V.sources_for_required_concepts("sandwich")

    def test_a_refusal_can_name_the_unmet_requirement(self):
        """C1 must name what is missing; 'incompatible' alone is not actionable."""
        assert V.unsupported_requirements(
            "jira", M.CONCEPT_APPROVAL, M.CONCEPT_ACTOR_GROUP, M.CONCEPT_WORK_ITEM
        ) == (M.CONCEPT_ACTOR_GROUP, M.CONCEPT_APPROVAL)
        assert V.unsupported_requirements("servicenow", M.CONCEPT_WORK_ITEM) == ()

    def test_the_published_families_match_the_registry(self, published):
        assert published["source_families"] == sorted(
            {d.source_family for d in conf.CONFORMANCE.values()}
        )

    def test_the_vocabulary_survives_json(self, published):
        """It is served over HTTP and written into partner tooling."""
        assert json.loads(json.dumps(published)) == published


class TestPartnerDocumentationCannotRot:
    """The partner reference carries an availability table, which is a snapshot and can
    therefore drift. Partner-facing documentation that has drifted from reality is worse
    than none — the same reasoning as 2.0-C3's AC6 (its worked example must build in CI).
    So the table is pinned to the live registry here.
    """

    PARTNER_DOC = REPO_ROOT / "docs" / "skills_sdk_vocabulary.md"

    @pytest.fixture(scope="class")
    def doc(self):
        assert self.PARTNER_DOC.exists(), f"partner reference missing at {self.PARTNER_DOC}"
        return self.PARTNER_DOC.read_text(encoding="utf-8")

    def test_it_documents_every_concept(self, doc):
        for concept in sorted(M.CONCEPT_SET):
            assert f"`{concept}`" in doc, f"{concept} is undocumented for partners"

    def test_the_availability_table_matches_the_live_registry(self, doc):
        """Parses the doc's own table and compares it, concept by concept, to what the
        registry actually supports."""
        rows = re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*([a-z_,\s]+?)\s*\|$", doc, re.M)
        documented = {
            concept: {c.strip() for c in sources.split(",") if c.strip()}
            for concept, sources in rows
            if concept in M.CONCEPT_SET
        }
        assert len(documented) == len(M.CONCEPT_SET), (
            f"the availability table covers {sorted(documented)}, not the whole set"
        )
        for concept, listed in documented.items():
            assert listed == set(conf.connectors_supporting(concept)), (
                f"the partner doc's availability for {concept} has drifted from the "
                f"registry: doc={sorted(listed)} "
                f"registry={list(conf.connectors_supporting(concept))}"
            )

    def test_it_states_the_stability_promise_and_the_digest(self, doc):
        assert "digest" in doc
        assert "stability promise" in doc.lower()
        for version_field in ("vocabulary_version", "concept_set_version", "contract_versions"):
            assert version_field in doc

    def test_it_hands_no_module_path_to_a_partner(self, doc):
        """The partner-facing page must not name an internal mapper either — the same
        boundary the published artifact holds. The internal reference footer names the
        module a CloudFulcrum reader needs, which is why only mapper paths are barred."""
        assert "discovery.concepts.mappers" not in doc
        for (_cid, _concept), mapper in MAPPERS.items():
            assert mapper.name not in doc

    def test_it_tells_a_partner_the_gap_rule(self, doc):
        """The single most load-bearing sentence for a pack author: an empty field on a
        declared gap is deliberate, not broken ingestion."""
        assert "absent" in doc and "partial" in doc
        assert "not broken ingestion" in doc


# ═══════════════════════════════════════════════════════════════════════════════
# Story-level invariants that no single task owns
# ═══════════════════════════════════════════════════════════════════════════════

class TestStoryLevelInvariants:

    def test_the_package_front_door_exposes_every_b4_surface(self):
        """`discovery.concepts` is what a reader (and 2.0-C3) opens first. A surface
        reachable only by a submodule path is a surface nobody finds."""
        import discovery.concepts as package

        for name in (
            "CONCEPT_SET", "CONTRACTS", "CONFORMANCE",          # T1
            "MAPPERS", "assert_no_approximation",                # T2
            "detect_approval_bottleneck",                        # T3
            "detect_open_work_item_backlog",                     # T4
            "load_fixture", "available_fixture_ids",             # T5
            "publish_vocabulary", "vocabulary_digest",           # T6
        ):
            assert hasattr(package, name), f"{name} is not on the package front door"
            assert name in package.__all__, f"{name} is not exported"

    def test_no_concept_output_can_name_an_individual(self, samples):
        """The platform's standing rule, checked over every mapper's output at once.
        The fixtures deliberately contain people, so a pass here is meaningful."""
        from tests.unit.test_r2_0_b4_t2_connector_mapping import _all_mapped_outputs

        blob = json.dumps([
            produced.to_dict() for _cid, _c, produced in _all_mapped_outputs(samples)
        ])
        for person in ("Sam Rivera", "Priya Nadar", "Alex Chen"):
            assert person not in blob

    def test_the_concept_set_carries_no_field_that_denotes_a_person(self):
        """Structural: ActorGroup is the only actor concept, and no concept offers a
        field a mapper could put a name in."""
        for concept, klass in M.CONCEPT_CLASSES.items():
            for field in getattr(klass, "__dataclass_fields__", {}):
                assert field not in ("assignee", "owner", "user", "person", "approver"), (
                    f"{concept}.{field} would give an individual somewhere to live"
                )

    def test_the_five_acs_are_all_represented_in_this_suite(self):
        """A validation ticket that quietly dropped an AC would look like a pass."""
        source = Path(__file__).read_text(encoding="utf-8")
        for ac in ("AC1", "AC2", "AC3", "AC4", "AC5"):
            assert f"# {ac} " in source or f"class Test{ac}" in source, ac
