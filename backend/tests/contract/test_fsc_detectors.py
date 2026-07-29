"""Contract tests for 2.0-D1 T2 — FSC detector set + ingest surface.

Definition of done for this subtask: at least four detectors produce findings on a
seeded FSC estate, each carrying the four-part criterion (evidence, confidence,
corroboration status, source trace); and AC5 is an absolute — no detector output
names an individual, only groups, queues and processes, in narrative text as much
as in structured fields.

What each section proves:

  AC1  — ``TestAC1FourDetectorsFire`` / ``TestFourPartCriterion``
  AC3  — ``TestAC3TerminologyFromPackConfig`` (FSC wording is pack config, and the
         machine-readable glossary cannot drift from the user-visible label file)
  AC4  — ``TestAC4NoEngineChanges`` (composed from existing machinery: detectors
         emit the standard DetectorResult and import no scorer/template module)
  AC5  — ``TestAC5NoIndividuals``, including the strongest form available: every
         person literal PRESENT IN THE FIXTURE is asserted absent from every
         finding. The fixture deliberately carries owners, contacts, actor names
         and household names so that test cannot be vacuous.

Two things these tests deliberately do NOT claim:

  * that the CALIBRATION is right. The thresholds and the seed were authored
    together, so ``TestAC1FourDetectorsFire`` proves the wiring fires, not that the
    numbers are correct. ``TestProvisionalHonesty`` pins the config's own admission
    of that.
  * that the CONNECTOR READS CORRECTLY against a real org. The fixture is a
    scaffold. ``TestIngestFieldShapeTolerance`` covers the failure mode we CAN
    test for offline — the documented ServiceNow ``{value, display_value}`` trap and
    Salesforce's null-relationship equivalent — but only real records confirm the
    shapes.
"""
from __future__ import annotations

import importlib
import json
import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")


# ── module loaders (tolerate both import roots, as the sibling suites do) ────────

def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _fsc_ingest():
    return _mod("ingest.fsc")


def _fc():
    return _mod("packs.fsc_finding")


def _cfg():
    return _mod("packs.financial_services_cloud_config")


def _pack_config():
    return _mod("packs.pack_config")


PACK_ID = "financial_services_cloud"

DETECTOR_MODULES = (
    "detectors.fsc_servicing_request_recurrence",
    "detectors.fsc_referral_handoff_friction",
    "detectors.fsc_approval_review_cycle",
    "detectors.fsc_service_queue_ageing",
    "detectors.fsc_cross_object_rework",
)


@pytest.fixture(scope="module")
def block():
    """The normalised, detector-visible FSC block from the seeded estate."""
    return _fsc_ingest().ingest()


@pytest.fixture(scope="module")
def sf_data(block):
    return {"fsc": block}


@pytest.fixture(scope="module")
def findings(sf_data):
    """Every finding the five FSC detectors emit on the seeded estate."""
    out = []
    for name in DETECTOR_MODULES:
        out.extend(_mod(name).detect(sf_data, {}, {}))
    return out


@pytest.fixture(scope="module")
def raw_fixture():
    ingest = _fsc_ingest()
    with open(ingest.FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── AC1 — at least four detectors fire on the seeded estate ─────────────────────

class TestAC1FourDetectorsFire:

    def test_at_least_four_detectors_produce_findings(self, findings):
        fired = {f.detector_id for f in findings}
        assert len(fired) >= 4, f"only {sorted(fired)} fired; AC1 needs >= 4"

    def test_all_five_detectors_produce_findings(self, findings):
        """Stronger than AC1 requires, and the honest state of the seed."""
        fired = {f.detector_id for f in findings}
        assert len(fired) == 5, sorted(fired)

    def test_every_detector_module_reports_fired_from_evaluate(self, sf_data):
        """``evaluate()`` (the non-firing-capture path) must agree with ``detect()``."""
        for name in DETECTOR_MODULES:
            module = _mod(name)
            evaluation = module.evaluate(sf_data, {}, {})
            fired = (
                evaluation.get("fired")
                if isinstance(evaluation, dict)
                else getattr(evaluation, "fired", None)
            )
            assert fired is True, f"{module.DETECTOR_ID} evaluate() did not fire"

    def test_metric_value_crosses_its_own_threshold(self, findings):
        for f in findings:
            assert f.metric_value >= f.threshold, (
                f"{f.detector_id} emitted a finding below its own threshold "
                f"({f.metric_value} < {f.threshold})"
            )

    def test_findings_carry_the_pack_signal_source(self, findings):
        for f in findings:
            assert f.signal_source == "salesforce"


# ── The four-part criterion, on every finding ──────────────────────────────────

class TestFourPartCriterion:

    def test_four_part_fields_match_the_platform_definition(self):
        """One definition platform-wide (Release 2.0 DoD #2), not three dialects."""
        fsc = _fc().FOUR_PART_CONTRACT_FIELDS
        cloud = _mod("packs.cloud_ops_finding").FOUR_PART_CONTRACT_FIELDS
        assert fsc == cloud == ("evidence", "confidence", "corroboration", "source_trace")

    def test_every_finding_carries_a_contract(self, findings):
        for f in findings:
            assert f.raw_evidence.get("finding_contract"), f.detector_id

    def test_every_contract_is_complete(self, findings):
        fc = _fc()
        for f in findings:
            contract = f.raw_evidence["finding_contract"]
            assert fc.missing_contract_parts(contract) == [], f.detector_id

    def test_evidence_carries_a_number(self, findings):
        for f in findings:
            evidence = f.raw_evidence["finding_contract"]["evidence"]
            assert any(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in evidence.values()
            ), f.detector_id

    def test_confidence_is_honest_about_being_capped(self, findings):
        """FSC reads one system, so every finding must say so rather than imply
        corroborated confidence."""
        for f in findings:
            confidence = f.raw_evidence["finding_contract"]["confidence"]
            assert confidence["level"] == "MEDIUM"
            assert confidence["capped"] is True
            assert confidence["eligible_for_high"] is False
            assert confidence["cap_reason"].strip()

    def test_corroboration_status_is_explicit_single_source(self, findings):
        fc = _fc()
        for f in findings:
            corroboration = f.raw_evidence["finding_contract"]["corroboration"]
            assert corroboration["status"] == fc.STATUS_SINGLE_SOURCE
            assert corroboration["label"] == fc.SINGLE_SOURCE_LABEL
            assert corroboration["sources"] == ["salesforce"]

    def test_source_trace_walks_back_to_records(self, findings):
        for f in findings:
            trace = f.raw_evidence["finding_contract"]["source_trace"]
            assert trace["systems"] == ["salesforce"]
            assert trace["artifacts"], f.detector_id
            for artifact in trace["artifacts"]:
                assert artifact.get("type") and artifact.get("id")

    def test_pack_boundary_enforcement_accepts_every_finding(self, findings):
        fc = _fc()
        assert fc.enforce_pack_findings(findings) == len(findings)

    def test_pack_boundary_enforcement_fails_an_incomplete_finding(self, findings):
        fc = _fc()
        broken = dict(findings[0].raw_evidence["finding_contract"])
        del broken["corroboration"]
        with pytest.raises(fc.FscContractViolation):
            fc.enforce_finding_contract(broken, detector_id="d")

    def test_pack_boundary_enforcement_fails_a_missing_contract(self):
        fc = _fc()
        with pytest.raises(fc.FscContractViolation):
            fc.enforce_finding_contract(None, detector_id="d")

    def test_source_trace_rejects_an_artifact_without_a_pointer(self):
        fc = _fc()
        with pytest.raises(ValueError):
            fc.build_source_trace(systems=["salesforce"], artifacts=[{"type": "queue"}])


# ── AC5 — no detector output names an individual (absolute) ─────────────────────

def _person_literals(raw_fixture) -> set:
    """Collect every person-identifying literal the FIXTURE contains.

    Derived from the fixture rather than hardcoded, so a person field added to the
    fixture later is covered without editing this test. Covers: values of
    person-shaped keys, ``Owner``/``OriginalActor`` names where Type == 'User'
    (a queue name there is permitted, a user's name is not), and household
    ``Account.Name`` values (a household name identifies a family).
    """
    fc = _fc()
    literals = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, dict) and "Type" in value and "Name" in value:
                    if str(value.get("Type", "")).lower() == "user" and value.get("Name"):
                        literals.add(str(value["Name"]))
                # Strings only, and long enough to be a real identifier: a bare
                # "1" would match any serialised payload and prove nothing.
                if isinstance(value, str) and len(value) >= 4 and fc.is_person_field(key):
                    literals.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw_fixture)
    for record in raw_fixture.get("Account", {}).get("records", []):
        if record.get("Name"):
            literals.add(str(record["Name"]))
    return {x for x in literals if x and x != "None"}


class TestAC5NoIndividuals:

    def test_the_fixture_actually_contains_person_data(self, raw_fixture):
        """Guards against a vacuous AC5 test.

        If the fixture were scrubbed of owners, contacts and household names, the
        leak test below would pass trivially and prove nothing.
        """
        literals = _person_literals(raw_fixture)
        assert len(literals) >= 10, literals
        assert "Priya Raman" in literals, "user-owner name missing from fixture"
        assert "Sharma Household" in literals, "household name missing from fixture"

    def test_no_person_literal_reaches_any_finding(self, findings, raw_fixture):
        """THE AC5 test. No person literal from the source data appears anywhere in
        any finding — structured field, narrative string, or artifact pointer."""
        literals = _person_literals(raw_fixture)
        serialised = json.dumps([f.raw_evidence for f in findings], default=str)
        leaked = sorted(x for x in literals if x in serialised)
        assert leaked == [], f"person literals leaked into findings: {leaked}"

    def test_no_person_literal_reaches_the_normalised_block(self, block, raw_fixture):
        """The floor is applied at the INGEST boundary, not only at emit time."""
        literals = _person_literals(raw_fixture)
        serialised = json.dumps(block, default=str)
        leaked = sorted(x for x in literals if x in serialised)
        assert leaked == [], f"person literals leaked into the FSC block: {leaked}"

    def test_contract_sweep_finds_nothing_in_any_finding(self, findings):
        fc = _fc()
        for f in findings:
            hits = fc.find_individual_references(f.raw_evidence["finding_contract"])
            assert hits == [], f"{f.detector_id}: {hits}"

    def test_household_names_are_never_emitted_only_record_ids(self, block, raw_fixture):
        """A household NAME identifies a family (and a one-member household, a
        person). Only opaque record ids may be emitted."""
        names = {
            r["Name"] for r in raw_fixture.get("Account", {}).get("records", [])
            if r.get("Name")
        }
        serialised = json.dumps(block, default=str)
        for name in names:
            assert name not in serialised

        # ...and the ids ARE present, so the finding is still traceable.
        assert "001FSCHH000000001" in serialised

    def test_config_declares_household_names_are_not_emitted(self):
        aggregation = _cfg().get_aggregation()
        assert aggregation.emit_household_names is False
        assert aggregation.household_reference_form == "record_id_only"

    def test_permitted_units_are_config_declared(self):
        aggregation = _cfg().get_aggregation()
        for unit in ("service_process_type", "review_type", "queue", "team", "object_pair"):
            assert unit in aggregation.permitted_units
        for forbidden in ("individual", "relationship_manager", "advisor"):
            assert forbidden in aggregation.forbidden_units

    def test_every_finding_declares_a_permitted_aggregation_unit(self, findings):
        permitted = set(_cfg().get_aggregation().permitted_units)
        for f in findings:
            unit = f.raw_evidence["finding_contract"]["evidence"].get("aggregation_unit")
            assert unit in permitted, f"{f.detector_id} aggregates by {unit!r}"

    def test_a_user_owned_record_contributes_no_owner(self, block):
        """Owner.Name is a QUEUE name when Type=='Queue' and a PERSON'S name when
        it is 'User'. Only the former may ever be read."""
        queues = {row["queue"] for row in block["service_queues"]}
        assert "Priya Raman" not in queues
        assert "Client Servicing Tier 1" in queues

    def test_building_a_finding_that_names_a_person_is_rejected(self):
        fc = _fc()
        with pytest.raises(ValueError):
            fc.build_finding_contract(
                evidence={"count": 3, "relationship_manager": "Priya Raman"},
                confidence=fc.build_confidence(
                    "LOW", capped=True, eligible_for_high=False),
                corroboration=fc.build_corroboration(
                    fc.STATUS_SINGLE_SOURCE, sources=["salesforce"], label="x"),
                source_trace=fc.build_source_trace(
                    systems=["salesforce"], artifacts=[{"type": "queue", "id": "q"}]),
            )

    def test_enforcement_rejects_a_finding_that_names_a_person(self, findings):
        fc = _fc()
        contract = json.loads(json.dumps(findings[0].raw_evidence["finding_contract"]))
        contract["evidence"]["case_owner"] = "Priya Raman"
        with pytest.raises(fc.FscContractViolation):
            fc.enforce_finding_contract(contract, detector_id="d")

    def test_narrative_person_name_is_caught(self, findings):
        """AC5 applies to narrative text as much as to structured fields."""
        fc = _fc()
        contract = json.loads(json.dumps(findings[0].raw_evidence["finding_contract"]))
        contract["evidence"]["statement"] = "Priya Raman is reassigning these."
        with pytest.raises(fc.FscContractViolation):
            fc.enforce_finding_contract(contract, detector_id="d")

    @pytest.mark.parametrize(
        "field",
        ["OwnerId", "ContactId", "FinServ__PrimaryOwner__c", "CreatedById",
         "relationship_manager", "advisor", "household_name", "case_owner",
         "LastModifiedById", "email", "phone"],
    )
    def test_person_field_names_are_recognised(self, field):
        assert _fc().is_person_field(field) is True

    @pytest.mark.parametrize(
        "field",
        ["queue", "team", "service_process_type", "review_type", "referral_type",
         "object_pair", "recurrence_count", "household_ids", "case_ids",
         "open_count", "departure_pct"],
    )
    def test_permitted_field_names_are_not_flagged(self, field):
        assert _fc().is_person_field(field) is False

    def test_email_and_phone_are_caught_anywhere(self):
        fc = _fc()
        assert fc.find_contact_details("reach me at a.b@example.com")
        assert fc.find_contact_details("call +44 7700 900123")
        assert fc.find_contact_details("Client Servicing Tier 1") == []

    def test_a_queue_name_is_not_mistaken_for_a_person(self):
        """The permitted-unit allowance: queues are Title-Case too."""
        fc = _fc()
        assert fc.find_person_text(
            "Overdue work concentrates on Client Servicing Tier 1.",
            allow=["Client Servicing Tier 1"],
        ) == []

    def test_scrub_person_fields_drops_them(self):
        fc = _fc()
        scrubbed = fc.scrub_person_fields(
            {"queue": "Card Services", "OwnerId": "005x", "ContactId": "003x"}
        )
        assert scrubbed == {"queue": "Card Services"}


# ── AC3 — FSC terminology is driven by pack config ─────────────────────────────

class TestAC3TerminologyFromPackConfig:

    def test_config_declares_the_required_fsc_terms(self):
        cfg = _cfg()
        glossary = cfg.get_terminology().glossary
        for term in cfg.REQUIRED_FSC_TERMS:
            assert term in glossary and glossary[term].strip()

    def test_glossary_cannot_drift_from_the_user_visible_label_file(self):
        """The machine-readable glossary (T2 config) and the user-visible
        _terminology block (T1 label file) must stay one vocabulary."""
        cfg_keys = set(_cfg().get_terminology().glossary)
        labels = _pack_config().get_ui_labels(PACK_ID) or {}
        label_keys = set(labels.get("_terminology", {}))
        assert cfg_keys == label_keys, (
            f"config-only: {cfg_keys - label_keys}, label-only: {label_keys - cfg_keys}"
        )

    def test_language_map_is_config_not_code(self):
        language_map = _cfg().get_terminology().language_map
        assert language_map.get("customer") == "household"
        assert language_map.get("account") == "financial account"

    def test_missing_term_is_rejected_loudly(self, tmp_path):
        cfg = _cfg()
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps({"terminology": {"glossary": {"household": "x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(cfg.FscConfigError):
            cfg.load_fsc_config(str(bad))

    def test_findings_speak_fsc_units_not_generic_ones(self, findings):
        """Each finding's evidence names an FSC concept, not a generic ticket."""
        fsc_units = {
            "service_process_type", "referral_type", "review_type", "queue",
            "object_pair",
        }
        for f in findings:
            evidence = f.raw_evidence["finding_contract"]["evidence"]
            assert fsc_units & set(evidence), f.detector_id


# ── AC4 — composed from existing machinery, no engine changes ───────────────────

class TestAC4NoEngineChanges:

    def test_detectors_emit_the_standard_detector_result(self, findings):
        models = _mod("models")
        for f in findings:
            assert isinstance(f, models.DetectorResult)

    def test_detectors_import_no_scorer_or_template_module(self):
        """A detector reaching into the scorer or the template model would be the
        engine change AC4 forbids."""
        import inspect
        forbidden = ("scorer", "template_registry", "materialize", "roadmap_engine")
        for name in DETECTOR_MODULES:
            source = inspect.getsource(_mod(name))
            for token in forbidden:
                assert token not in source, f"{name} references {token!r}"

    def test_no_detector_hardcodes_its_firing_threshold_as_the_only_source(self):
        """Module defaults may exist as a degrade path, but the config must win."""
        for name in DETECTOR_MODULES:
            module = _mod(name)
            assert hasattr(module, "_thresholds")
            assert module._THRESHOLD_SECTION in _cfg().get_thresholds()

    def test_every_detector_declares_signal_metrics(self):
        for name in DETECTOR_MODULES:
            metrics = _mod(name).SIGNAL_METRICS
            assert isinstance(metrics, list) and metrics

    def test_pack_dispatch_needs_no_new_mechanism(self):
        """The pack routes through the same registry predicate every pack uses."""
        m = _pack_config()
        assert m.is_financial_services_cloud_pack(PACK_ID) is True
        assert len(m.get_detector_modules(PACK_ID)) == 5


# ── Composition: what was reused, and what deliberately was not ────────────────

class TestComposedFromExistingDetectors:

    def test_referral_friction_carries_handoff_frictions_average_forward(self):
        """handoff_friction.py's THRESHOLD = 1.5 is reused unchanged."""
        source = _mod("detectors.handoff_friction")
        thresholds = _cfg().get_detector_thresholds("referral_handoff_friction", {})
        assert float(thresholds["min_avg_hops"]) == float(source.THRESHOLD) == 1.5

    def test_cross_object_rework_carries_cross_system_echos_rate_forward(self):
        """cross_system_echo.py's THRESHOLD = 0.15 is reused unchanged."""
        source = _mod("detectors.cross_system_echo")
        thresholds = _cfg().get_detector_thresholds("cross_object_rework", {})
        assert float(thresholds["duplicate_rate_threshold"]) == float(source.THRESHOLD)

    def test_queue_ageing_reuses_the_per_queue_baseline_rule(self):
        """The one new detector still borrows cloud_ops' per-queue-own-baseline
        shape and its departure fraction."""
        source = _mod("detectors.cloud_ops_queue_ageing")
        thresholds = _cfg().get_detector_thresholds("service_queue_ageing", {})
        assert float(thresholds["baseline_departure_pct"]) == float(
            source.DEFAULT_BASELINE_DEPARTURE_PCT
        )
        assert int(thresholds["min_baseline_runs"]) == int(
            source.DEFAULT_MIN_BASELINE_RUNS
        )

    def test_queue_ageing_never_uses_a_global_baseline(self, findings):
        for f in findings:
            if f.detector_id == "FSC_SERVICE_QUEUE_AGEING":
                assert f.raw_evidence["baseline_scope"] == "per_queue"

    def test_unbaselined_queue_does_not_fire(self):
        """No global fallback: a queue with no baseline has nothing to be elevated
        against, so it must not fire."""
        module = _mod("detectors.fsc_service_queue_ageing")
        rows = [{
            "queue": "Brand New Queue", "current_avg_age_days": 400.0,
            "baseline_avg_age_days": 0.0, "baseline_runs": 0, "open_count": 99,
        }]
        assert module.detect({"fsc": {"service_queues": rows}}, {}, {}) == []

    def test_approval_detector_covers_both_source_detectors_ideas(self):
        """approval_delay (dwell) and approval_bottleneck (pending depth) collide on
        DETECTOR_ID upstream, so both ideas ride one FSC detector as two legs."""
        delay = _mod("detectors.approval_delay")
        bottleneck = _mod("detectors.approval_bottleneck")
        assert delay.DETECTOR_ID == bottleneck.DETECTOR_ID == "APPROVAL_BOTTLENECK"
        thresholds = _cfg().get_detector_thresholds("approval_review_cycle", {})
        assert "dwell_days_threshold" in thresholds   # from approval_delay
        assert "min_pending" in thresholds            # from approval_bottleneck


# ── Config-driven: a threshold edit changes behaviour with no code change ───────

class TestThresholdsAreConfigDriven:

    def test_all_five_sections_are_declared(self):
        cfg = _cfg()
        thresholds = cfg.get_thresholds()
        for section in cfg.REQUIRED_THRESHOLD_SECTIONS:
            assert section in thresholds

    def test_missing_section_is_rejected_loudly(self, tmp_path):
        """A config that silently dropped a section would leave that detector on
        hardcoded defaults — the exact thing externalising them prevents."""
        cfg = _cfg()
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({
            "terminology": {"glossary": {t: "x" for t in cfg.REQUIRED_FSC_TERMS}},
            "thresholds": {"servicing_request_recurrence": {}},
        }), encoding="utf-8")
        with pytest.raises(cfg.FscConfigError):
            cfg.load_fsc_config(str(bad))

    def test_documentation_keys_are_never_returned_as_thresholds(self):
        thresholds = _cfg().get_detector_thresholds("servicing_request_recurrence", {})
        assert not any(str(k).startswith("_") for k in thresholds)

    def test_raising_a_threshold_stops_a_detector_firing(self, sf_data, tmp_path):
        """The behavioural proof: same data, edited config, different outcome — with
        no code change."""
        cfg = _cfg()
        module = _mod("detectors.fsc_servicing_request_recurrence")

        assert module.detect(sf_data, {}, {}), "precondition: fires as shipped"

        raised = tmp_path / "raised.json"
        raised.write_text(json.dumps({
            "packVersion": "9.9.9",
            "terminology": {"glossary": {t: "x" for t in cfg.REQUIRED_FSC_TERMS}},
            "thresholds": {
                "servicing_request_recurrence": {"min_recurrence_count": 999,
                                                 "min_distinct_financial_accounts": 1},
                "referral_handoff_friction": {},
                "approval_review_cycle": {},
                "service_queue_ageing": {},
                "cross_object_rework": {},
            },
        }), encoding="utf-8")

        thresholds = cfg.get_detector_thresholds(
            "servicing_request_recurrence", {}, path=str(raised)
        )
        rows = (sf_data["fsc"].get("servicing_requests") or [])
        assert module._qualifying({"servicing_requests": rows}, thresholds) == []

    def test_config_edit_is_picked_up_without_restart(self, tmp_path):
        """Cache is keyed by (path, mtime), so an edit applies immediately."""
        cfg = _cfg()
        path = tmp_path / "cfg.json"
        payload = {
            "packVersion": "1.0.0",
            "terminology": {"glossary": {t: "x" for t in cfg.REQUIRED_FSC_TERMS}},
            "thresholds": {s: {"min_pending": 1} for s in cfg.REQUIRED_THRESHOLD_SECTIONS},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert cfg.load_fsc_config(str(path)).thresholds[
            "approval_review_cycle"]["min_pending"] == 1

        payload["thresholds"]["approval_review_cycle"]["min_pending"] = 42
        path.write_text(json.dumps(payload), encoding="utf-8")
        stamp = os.path.getmtime(path) + 10
        os.utime(path, (stamp, stamp))
        assert cfg.load_fsc_config(str(path)).thresholds[
            "approval_review_cycle"]["min_pending"] == 42

    def test_missing_config_degrades_to_module_defaults(self):
        """A detector degrades to its documented defaults rather than failing a run."""
        cfg = _cfg()
        result = cfg.get_detector_thresholds(
            "service_queue_ageing", {"baseline_departure_pct": 0.25},
            path="/no/such/fsc_config.json",
        )
        assert result == {"baseline_departure_pct": 0.25}


class TestProvisionalHonesty:
    """The calibration is not measured, and nothing may imply otherwise."""

    def test_config_declares_itself_provisional(self):
        assert _cfg().is_provisional() is True
        assert "PROVISIONAL" in _cfg().calibration_status().upper()

    def test_provisional_status_is_readable_on_the_normalised_block(self, block):
        assert "PROVISIONAL" in block["_meta"]["calibration_status"].upper()

    def test_every_threshold_section_records_its_basis(self):
        """Each number says where it came from, so a reader is not left guessing
        whether it was measured."""
        ingest = _fsc_ingest()
        path = _pack_config().get_pack_config_path(PACK_ID)
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for section, body in raw["thresholds"].items():
            if section.startswith("_"):
                continue
            assert body.get("_basis", "").strip(), f"{section} has no _basis"
            assert "PROVISIONAL" in body["_basis"].upper()
        assert ingest.FIXTURE_PATH.exists()

    def test_fixture_denies_being_evidence_of_correctness(self, raw_fixture):
        """The fixture is a scaffold, and says so."""
        meta = raw_fixture["_meta"]
        assert meta.get("not_evidence_of_correctness", "").strip()
        assert meta.get("fidelity_notes")


# ── Ingest: field-shape tolerance (the ServiceNow lesson) ───────────────────────

class TestIngestFieldShapeTolerance:

    def test_nested_relationship_traversal(self):
        ingest = _fsc_ingest()
        record = {"RecordType": {"DeveloperName": "Service_Process"}}
        assert ingest._field(record, "RecordType.DeveloperName") == "Service_Process"

    def test_null_relationship_does_not_raise(self):
        """``RecordType`` is null whenever the record has none — a real org has
        these, and traversal must not assume it resolves."""
        ingest = _fsc_ingest()
        assert ingest._field({"RecordType": None}, "RecordType.DeveloperName") is None
        assert ingest._field({}, "Owner.Type", "fallback") == "fallback"

    def test_value_display_value_envelope_is_unwrapped(self):
        """The documented ServiceNow failure mode, defended against here rather
        than discovered in production."""
        ingest = _fsc_ingest()
        record = {"Status": {"value": "Working", "display_value": "In Progress"}}
        assert ingest._field(record, "Status") == "Working"

    def test_unresolvable_record_type_is_counted_not_assumed_in_scope(self, block):
        """A record whose type will not resolve is a reported data gap, not a
        silent inclusion or exclusion."""
        assert block["_meta"]["record_types_unresolved"] >= 1

    def test_unresolvable_review_type_is_counted(self, block):
        assert block["_meta"]["review_types_unresolved"] >= 1

    def test_out_of_scope_record_types_are_excluded(self, block):
        """The Marketing_Enquiry cases must not appear as service processes."""
        types = {r["service_process_type"] for r in block["servicing_requests"]}
        assert "Marketing_Query" not in types
        assert "Address_Change" in types


class TestScopeIsOrgConfigurable:
    """Record types and picklist values are org-defined, so they are config.

    The task requires confirming that "a Case in an FSC org carries the record types
    and picklist values the detectors branch on". That cannot be confirmed without a
    real org — so instead of hardcoding a guess, the values are declared in the pack
    config and a differing org becomes a config edit rather than a code change.
    """

    def test_scope_loads_from_config(self):
        scope = _cfg().get_scope()
        assert "Service_Process" in scope.service_process_record_types
        assert "IndustriesHousehold" in scope.household_record_types
        assert "closed" in scope.closed_statuses

    def test_ingest_reports_the_configured_scope(self, block):
        scope = _cfg().get_scope()
        assert block["_meta"]["in_scope_record_types"] == list(
            scope.service_process_record_types
        )

    def test_a_different_org_record_type_is_a_config_edit(self, raw_fixture):
        """Rename the in-scope record type and the previously out-of-scope cases
        become in scope — with no code change."""
        ingest = _fsc_ingest()
        assert ingest._scope_service_process_types()  # loads

        # Marketing_Enquiry is out of scope as shipped...
        block = ingest.ingest(raw_fixture)
        assert "Marketing_Query" not in {
            r["service_process_type"] for r in block["servicing_requests"]
        }

        # ...and in scope for an org that names its service process that way.
        original = ingest.SERVICE_PROCESS_RECORD_TYPES
        try:
            ingest.SERVICE_PROCESS_RECORD_TYPES = ("Marketing_Enquiry",)
            ingest.get_scope = lambda *a, **k: _cfg().FscScope(
                service_process_record_types=["Marketing_Enquiry"],
                household_record_types=["IndustriesHousehold"],
                closed_statuses=["closed"],
            )
            block = ingest.ingest(raw_fixture)
            assert "Marketing_Query" in {
                r["service_process_type"] for r in block["servicing_requests"]
            }
        finally:
            ingest.SERVICE_PROCESS_RECORD_TYPES = original
            importlib.reload(ingest)

    def test_unknown_status_reads_as_open(self):
        """The CLOSED set is matched, so an unrecognised org status reads as open —
        the conservative direction for an ageing detector."""
        ingest = _fsc_ingest()
        assert ingest._is_open({"Status": "Awaiting_Client_Signature"}) is True
        assert ingest._is_open({"Status": "Closed"}) is False
        assert ingest._is_open({"ClosedDate": "2026-01-01T00:00:00.000+0000"}) is False

    def test_scope_degrades_to_module_defaults_when_config_unreadable(self):
        ingest = _fsc_ingest()
        original = ingest.get_scope
        try:
            def _boom(*args, **kwargs):
                raise RuntimeError("config unreadable")
            ingest.get_scope = _boom
            assert ingest._scope_service_process_types() == (
                ingest.SERVICE_PROCESS_RECORD_TYPES
            )
            assert ingest._scope_closed_statuses() == ingest.CLOSED_STATUSES
        finally:
            ingest.get_scope = original

    def test_query_envelope_and_bare_list_both_read(self):
        ingest = _fsc_ingest()
        assert len(ingest._records({"Case": {"records": [{"Id": "1"}]}}, "Case")) == 1
        assert len(ingest._records({"Case": [{"Id": "1"}]}, "Case")) == 1
        assert ingest._records({}, "Case") == []

    def test_salesforce_datetime_formats_parse(self):
        ingest = _fsc_ingest()
        for value in ("2026-06-28T09:14:00.000+0000",
                      "2026-06-28T09:14:00.000Z",
                      "2026-06-28T09:14:00+00:00",
                      "2026-06-28"):
            assert ingest._parse_dt(value) is not None
        assert ingest._parse_dt(None) is None
        assert ingest._parse_dt("not a date") is None

    def test_offline_run_is_deterministic(self):
        """Ages are measured from the fixture's pinned reference date, so repeated
        offline runs are identical and threshold tests are not time-dependent."""
        ingest = _fsc_ingest()
        first = ingest.ingest()
        second = ingest.ingest()
        assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
            second, sort_keys=True, default=str
        )

    def test_fixture_uses_real_fsc_api_names(self, raw_fixture):
        """Structurally faithful, not convenient: the managed-package namespace and
        the real query envelope."""
        for sobject in ("FinServ__Referral__c", "FinServ__FinancialAccount__c",
                        "FinServ__Referral__History"):
            assert sobject in raw_fixture
        case = raw_fixture["Case"]
        assert {"totalSize", "done", "records"} <= set(case)
        assert "attributes" in case["records"][0]

    def test_live_soql_targets_the_fsc_objects(self):
        """The ingest surface the story says was missing: FSC managed-package SOQL
        alongside the standard-object reads."""
        ingest = _fsc_ingest()
        queried = {sobject for sobject, _ in ingest._LIVE_QUERIES}
        assert "FinServ__Referral__c" in queried
        assert "FinServ__FinancialAccount__c" in queried
        assert "Case" in queried and "ProcessInstance" in queried
        assert "FinServ__ReferralType__c" in ingest.SOQL_REFERRAL

    def test_live_soql_selects_no_person_columns(self):
        """Not selecting a person column means it cannot leak even if a future
        normaliser forgets to scrub."""
        fc = _fc()
        ingest = _fsc_ingest()
        for sobject, soql in ingest._LIVE_QUERIES:
            for column in ("OwnerId", "ContactId", "CreatedById", "ActorId",
                           "FinServ__PrimaryOwner__c", "Owner.Name,"):
                assert column not in soql.replace("Owner.Name\n", ""), (
                    f"{sobject} SOQL selects person column {column!r}"
                )
        assert fc.is_person_field("OwnerId")

    def test_ingest_accepts_an_injected_payload(self):
        """Tests and callers can supply raw records without a client."""
        ingest = _fsc_ingest()
        block = ingest.ingest({"_meta": {"reference_date": "2026-07-01T00:00:00.000+0000"}})
        assert block["servicing_requests"] == []
        assert block["_meta"]["reference_date"].startswith("2026-07-01")


# ── Negative controls: below-threshold signal must not fire ─────────────────────

class TestNegativeControls:

    def test_below_threshold_recurrence_does_not_fire(self, block):
        """Beneficiary_Update has 2 occurrences against a floor of 4."""
        module = _mod("detectors.fsc_servicing_request_recurrence")
        fired = {
            f.raw_evidence["service_process_type"]
            for f in module.detect({"fsc": block}, {}, {})
        }
        assert "Beneficiary_Update" not in fired
        assert "Address_Change" in fired

    def test_shallow_queue_does_not_fire_on_ageing(self, block):
        """Compliance Review is elevated against its baseline but holds fewer items
        than min_open_count, so it must not fire."""
        module = _mod("detectors.fsc_service_queue_ageing")
        fired = {f.raw_evidence["queue"] for f in module.detect({"fsc": block}, {}, {})}
        assert "Wealth Operations" not in fired
        assert "Client Servicing Tier 1" in fired

    def test_low_pending_review_does_not_fire(self, block):
        module = _mod("detectors.fsc_approval_review_cycle")
        fired = {f.raw_evidence["review_type"] for f in module.detect({"fsc": block}, {}, {})}
        assert "Beneficiary_Change_Approval" not in fired
        assert "Suitability_Review" in fired

    def test_empty_signal_fires_nothing(self):
        for name in DETECTOR_MODULES:
            module = _mod(name)
            assert module.detect({}, {}, {}) == []
            assert module.detect({"fsc": {}}, {}, {}) == []

    def test_malformed_rows_are_ignored_not_fatal(self):
        for name in DETECTOR_MODULES:
            module = _mod(name)
            block = {
                "servicing_requests": [None, "nope", {}],
                "referral_handoffs": [None, 42],
                "approval_reviews": [None],
                "service_queues": ["x"],
                "cross_object_rework": [None],
            }
            assert module.detect({"fsc": block}, {}, {}) == []


# ── Causal gate: concentration-shaped wording only ─────────────────────────────

class TestWordingIsNotCausal:

    def test_no_finding_asserts_causation(self, findings):
        fc = _fc()
        for f in findings:
            statement = f.raw_evidence["finding_contract"]["evidence"].get("statement", "")
            assert fc.find_causal_language(statement) == [], f.detector_id

    def test_every_finding_carries_a_concentration_statement(self, findings):
        for f in findings:
            statement = f.raw_evidence["finding_contract"]["evidence"].get("statement", "")
            assert "concentrat" in statement.lower(), f.detector_id

    def test_causal_wording_is_rejected(self):
        fc = _fc()
        with pytest.raises(ValueError):
            fc.assert_not_causal("Queue ageing is caused by the referral backlog")
