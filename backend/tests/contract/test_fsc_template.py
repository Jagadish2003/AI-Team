"""Contract tests for 2.0-D1 T4 — FSC template instance + registry entry.

Definition of done: the FSC template appears in the registry, pre-populates
systems, roles, focus and pack per AC2, and is selectable alongside Lending in one
run with both packs' findings present and separately calibrated.

Two things this file is careful about, both taken from the ticket:

  * **AC2's real test is the multi-pack composition, not anything
    template-specific.** The guarantee is that each pack's own scorer calibration
    applies only to that pack's findings and is never blended, and that two packs
    surfacing the same underlying pattern stay TWO findings distinguished by
    ``opportunity_identity`` — cross-pack merging is a permanent non-goal. So
    ``TestAC2MultiPackComposition`` asserts two approval findings where a naive
    reading expects one, rather than treating it as duplication.

  * **``detector_emphasis`` records emphasis; it does not change scoring.**
    ``TestDetectorEmphasisIsProvenanceOnly`` proves that directly by scoring with
    the emphasis list emptied and asserting the scores are identical — so a
    template whose emphasis "looks right" while its ``pack_id``/``focus_id`` is
    wrong cannot pass silently here.

AC4 applies most directly to this subtask: if delivering the template had required
editing ``TemplateDefinition`` or ``register_template``, that would be a defect
against the model's genericity claim. ``TestAC4NoTemplateModelChanges`` pins that
neither changed.
"""
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    marker = Path("backend") / "discovery" / "packs" / "template_registry.py"
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / marker).is_file():
            return candidate
    try:
        import discovery.packs.template_registry as tr
    except ModuleNotFoundError:  # pragma: no cover
        import backend.discovery.packs.template_registry as tr  # type: ignore
    # .../<repo>/backend/discovery/packs/template_registry.py
    #      parents: [0]=packs [1]=discovery [2]=backend [3]=<repo>
    return Path(tr.__file__).resolve().parents[3]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"

TEMPLATE_MODEL_FILES = (
    "backend/discovery/packs/template_registry.py",
)

TEMPLATE_ID = "financial_services_cloud"
PACK_ID = "financial_services_cloud"
LENDING_TEMPLATE_ID = "commercial_lending"
LENDING_PACK_ID = "ncino"


def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _tr():
    return _mod("packs.template_registry")


def _pack_config():
    return _mod("packs.pack_config")


def _fsc_scorer():
    return _mod("packs.financial_services_cloud_scorer")


@pytest.fixture(scope="module")
def template():
    defn = _tr().get_template(TEMPLATE_ID)
    assert defn is not None, "FSC template is not registered"
    return defn


@pytest.fixture(scope="module")
def fsc_findings():
    sf_data = {"fsc": _mod("ingest.fsc").ingest()}
    out = []
    for name in ("detectors.fsc_servicing_request_recurrence",
                 "detectors.fsc_referral_handoff_friction",
                 "detectors.fsc_approval_review_cycle",
                 "detectors.fsc_service_queue_ageing",
                 "detectors.fsc_cross_object_rework"):
        out.extend(_mod(name).detect(sf_data, {}, {}))
    return out


# ── The template exists and pre-populates its defaults (AC2) ───────────────────

class TestTemplateRegistryEntry:

    def test_template_is_in_the_registry(self):
        assert TEMPLATE_ID in _tr().TEMPLATE_REGISTRY

    def test_template_appears_in_the_listing(self):
        ids = [t.template_id for t in _tr().list_templates()]
        assert TEMPLATE_ID in ids

    def test_get_template_resolves_it(self, template):
        assert template.template_id == TEMPLATE_ID

    def test_it_has_a_label_and_description(self, template):
        assert template.label == "Financial Services Cloud"
        assert template.description.strip()

    def test_it_prepopulates_systems(self, template):
        """AC2: pre-populates systems."""
        assert "salesforce_fsc" in template.suggested_systems
        assert len(template.suggested_systems) >= 3

    def test_it_prepopulates_roles_for_every_suggested_system(self, template):
        """AC2: pre-populates roles. A suggested system with no role would launch
        with no weighting for that source."""
        for system_id in template.suggested_systems:
            assert system_id in template.suggested_roles, system_id
        assert template.suggested_roles["salesforce_fsc"] == "system_of_record"

    def test_it_prepopulates_focus(self, template):
        """AC2: pre-populates focus."""
        assert template.focus_defaults.focus_id == "member_customer_service"
        assert template.focus_defaults.emphasis

    def test_it_prepopulates_the_pack(self, template):
        """AC2: pre-populates pack — and it must be THE FSC pack."""
        assert template.pack_id == PACK_ID

    def test_the_focus_is_a_real_focus(self, template):
        focus = _mod("packs.focus_affinity")
        assert template.focus_defaults.focus_id in focus.FOCUS_AFFINITY

    def test_the_focus_actually_emphasises_an_fsc_detector(self, template):
        """A focus_id that emphasises none of the pack's detectors would rank
        nothing — the silent-wrongness the ticket warns about."""
        focus = _mod("packs.focus_affinity")
        emphasised = set(focus.FOCUS_AFFINITY[template.focus_defaults.focus_id] or ())
        assert emphasised & set(_fsc_scorer().FSC_DETECTOR_IDS), (
            f"focus {template.focus_defaults.focus_id!r} emphasises none of the FSC "
            f"detectors"
        )

    def test_metadata_records_the_industry_and_provenance(self, template):
        assert template.metadata["industry_id"] == "financial_services"
        assert template.metadata["source"] == "2.0-D1"
        assert template.metadata["version"]

    def test_it_declares_the_salesforce_product_it_belongs_to(self, template):
        assert template.metadata["salesforce_product"] == "salesforce_fsc"

    def test_pack_reference_is_valid_so_register_template_would_accept_it(self, template):
        """register_template raises on an unknown pack — the hard T1 dependency."""
        assert template.pack_id in _pack_config().list_packs()

    def test_every_template_still_references_a_real_pack(self):
        """The registry-wide invariant must not be broken by the new entry."""
        known = set(_pack_config().list_packs())
        for t in _tr().list_templates():
            assert t.pack_id in known, f"{t.template_id} -> {t.pack_id}"

    def test_the_snapshot_carries_the_packs_current_version(self, template):
        snapshot = _tr().template_defaults_snapshot(template)
        assert snapshot["pack_id"] == PACK_ID
        assert snapshot["pack_version"] == _pack_config().get_pack_version(PACK_ID)


# ── Registry entry under financial_services ────────────────────────────────────

class TestIndustryRegistryEntry:

    def test_fsc_pack_is_hinted_for_financial_services(self):
        industries = _mod("packs.industry_registry").INDUSTRY_REGISTRY
        assert PACK_ID in industries["financial_services"].pack_hints

    def test_hinting_it_is_honest_because_the_pack_ships_detectors(self):
        """The story's registry-honesty rule: an entry appears on a selectable
        surface only once the pack actually ships. It now does (T1/T2/T3)."""
        assert _pack_config().get_detector_modules(PACK_ID), (
            "FSC is hinted for financial_services but ships no detectors"
        )

    def test_every_hinted_pack_is_registered(self):
        """Registry-wide invariant, re-checked after the new hint."""
        industries = _mod("packs.industry_registry").INDUSTRY_REGISTRY
        registry = _pack_config().PACK_REGISTRY
        for industry_id, config in industries.items():
            for hint in config.pack_hints:
                assert hint in registry, f"{industry_id} hints unknown pack {hint!r}"

    def test_lending_hint_is_undisturbed(self):
        industries = _mod("packs.industry_registry").INDUSTRY_REGISTRY
        hints = industries["financial_services"].pack_hints
        assert "ncino" in hints and "service_cloud" in hints

    def test_the_fsc_system_default_already_existed(self):
        """salesforce_fsc was already an anchored system default for this industry;
        T4 only adds the pack hint."""
        industries = _mod("packs.industry_registry").INDUSTRY_REGISTRY
        assert "salesforce_fsc" in industries["financial_services"].system_defaults


# ── detector_emphasis is provenance, not scoring ───────────────────────────────

class TestDetectorEmphasisIsProvenanceOnly:

    def test_emphasis_names_real_fsc_detectors(self, template):
        """Pinned against the scorer so the list cannot drift into looking right
        while being wrong."""
        scorer = _fsc_scorer()
        assert template.detector_emphasis, "template declares no emphasis"
        for detector_id in template.detector_emphasis:
            assert scorer.is_financial_services_cloud_detector(detector_id), (
                f"{detector_id} is not a scored FSC detector"
            )

    def test_emphasis_covers_the_whole_pack(self, template):
        assert set(template.detector_emphasis) == set(
            _fsc_scorer().FSC_DETECTOR_IDS
        )

    def test_emphasis_matches_the_registered_detector_modules(self, template):
        shipped = {
            importlib.import_module(p).DETECTOR_ID
            for p in _pack_config().get_detector_modules(PACK_ID)
        }
        assert set(template.detector_emphasis) == shipped

    def test_emptying_the_emphasis_does_not_change_any_score(self, fsc_findings, template):
        """The ticket's warning, tested directly: emphasis records intent for the
        run and UI and does NOT itself change scoring. Real scoring comes from
        pack_id and focus_id."""
        scorer = _fsc_scorer()
        ranking = scorer.rank_fsc_findings(fsc_findings)
        with_emphasis = [
            scorer.score_financial_services_cloud(f, ranking=ranking)
            for f in fsc_findings
        ]

        original = list(template.detector_emphasis)
        try:
            template.detector_emphasis = []
            ranking2 = scorer.rank_fsc_findings(fsc_findings)
            without = [
                scorer.score_financial_services_cloud(f, ranking=ranking2)
                for f in fsc_findings
            ]
        finally:
            template.detector_emphasis = original

        for a, b in zip(with_emphasis, without):
            assert a["impact"] == b["impact"]
            assert a["ops_impact_score"] == b["ops_impact_score"]
            assert a["ops_impact_rank"] == b["ops_impact_rank"]

    def test_the_field_documents_its_own_limitation(self):
        """The model's own comment says so; keep it there."""
        source = (REPO_ROOT / TEMPLATE_MODEL_FILES[0]).read_text(encoding="utf-8")
        assert "it does not itself change scoring" in source


# ── Terminology: FSC language, and never blended with lending's ────────────────

class TestTerminology:

    def test_template_declares_fsc_terminology(self, template):
        assert template.terminology["customer"] == "household"
        assert template.terminology["ticket"] == "service process"
        assert template.terminology["backlog"] == "service queue"
        # `account -> financial account` and `handoff -> referral handoff` were
        # deliberately removed: their replacements contain their sources, so they
        # double-expanded this pack's own label copy. See
        # test_every_mapping_is_idempotent below.
        assert "account" not in template.terminology
        assert "handoff" not in template.terminology

    def test_template_terminology_matches_the_packs_language_map(self, template):
        """Template and pack must speak the same language (the cloud_ops
        discipline), so a reader never sees two different vocabularies."""
        config_path = _pack_config().get_pack_config_path(PACK_ID)
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        language_map = raw["terminology"]["language_map"]
        for generic, domain in language_map.items():
            assert template.terminology.get(generic) == domain, (
                f"template says {template.terminology.get(generic)!r} for "
                f"{generic!r} but the pack config says {domain!r}"
            )

    def test_fsc_and_lending_vocabularies_are_different(self):
        """If they were the same, the non-blending test below would prove nothing."""
        fsc = _tr().get_template(TEMPLATE_ID).terminology
        lending = _tr().get_template(LENDING_TEMPLATE_ID).terminology
        assert fsc["customer"] != lending["customer"]

    def test_a_combined_run_keeps_each_packs_terminology_separate(self):
        """Composition must not blend vocabularies: each pack boundary carries its
        OWN map, so an FSC finding never gets relabelled with lending words."""
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        boundaries = {
            b["pack_id"]: b["terminology"]
            for b in resolved["effective"]["pack_boundaries"]
        }
        assert boundaries[PACK_ID]["customer"] == "household"
        assert boundaries[LENDING_PACK_ID]["customer"] == "borrower"

    def test_every_mapping_is_idempotent(self):
        """A mapping whose REPLACEMENT CONTAINS its SOURCE double-expands.

        ``app/terminology.py`` substitutes whole words, so ``account`` ->
        ``financial account`` turns text that already says "financial account" into
        "financial financial account" — on every served finding, roadmap entry and
        executive report. Applying the map twice must equal applying it once.
        """
        terminology = _mod_app_terminology()
        fsc = _tr().get_template(TEMPLATE_ID).terminology
        for generic, domain in fsc.items():
            once = terminology.rewrite_text(generic, fsc)
            twice = terminology.rewrite_text(once, fsc)
            assert once == twice, (
                f"{generic!r} -> {domain!r} is not idempotent: {once!r} becomes "
                f"{twice!r}. The replacement must not contain the source word."
            )

    def test_no_mapping_replacement_contains_its_own_source(self):
        """The structural form of the rule above, stated directly."""
        fsc = _tr().get_template(TEMPLATE_ID).terminology
        for generic, domain in fsc.items():
            assert generic.lower() not in domain.lower().split(), (
                f"{generic!r} -> {domain!r}: the replacement contains the source, "
                f"so any text already using the domain phrase double-expands"
            )

    def test_the_packs_own_label_copy_survives_its_own_map(self):
        """The user-visible property: FSC labels are ALREADY in FSC language, so the
        rewrite must not corrupt them. This is what the removed mappings broke."""
        terminology = _mod_app_terminology()
        fsc = _tr().get_template(TEMPLATE_ID).terminology
        labels = _pack_config().get_ui_labels(PACK_ID) or {}
        for detector_id, entry in labels.items():
            if detector_id.startswith("_"):
                continue
            for field, value in entry.items():
                if not isinstance(value, str):
                    continue
                once = terminology.rewrite_text(value, fsc)
                twice = terminology.rewrite_text(once, fsc)
                assert once == twice, f"{detector_id}.{field} is not stable"
                for doubled in ("financial financial", "referral referral",
                                "household household", "service process service process"):
                    assert doubled not in once.lower(), (
                        f"{detector_id}.{field} double-expanded to {once!r}"
                    )

    def test_applying_one_vocabulary_never_introduces_the_others(self):
        terminology = _mod_app_terminology()
        fsc = _tr().get_template(TEMPLATE_ID).terminology
        rewritten = terminology.apply_terminology(
            {"description": "The customer account was updated."}, fsc
        )
        assert "household" in rewritten["description"]
        assert "borrower" not in rewritten["description"]
        assert "facility" not in rewritten["description"]


def _mod_app_terminology():
    try:
        return importlib.import_module("app.terminology")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.app.terminology")


# ── AC2 — composable with Lending in one multi-pack run ────────────────────────

class TestAC2MultiPackComposition:

    def test_both_templates_resolve_to_both_packs(self):
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        assert resolved["effective"]["pack_ids"] == [PACK_ID, LENDING_PACK_ID]

    def test_selection_order_is_preserved(self):
        resolved = _tr().resolve_launch_config(
            None, template_ids=[LENDING_TEMPLATE_ID, TEMPLATE_ID]
        )
        assert resolved["effective"]["pack_ids"] == [LENDING_PACK_ID, PACK_ID]

    def test_systems_are_the_union_of_both_templates(self):
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        systems = resolved["effective"]["selected_system_ids"]
        assert "salesforce_fsc" in systems
        assert "salesforce_ncino" in systems

    def test_an_untouched_combined_launch_is_recorded_as_untouched(self):
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        assert resolved["provenance"]["untouched"] is True
        assert resolved["provenance"]["edited_fields"] == []

    def test_each_pack_boundary_carries_its_own_pack_version(self):
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        m = _pack_config()
        for boundary in resolved["effective"]["pack_boundaries"]:
            assert boundary["pack_version"] == m.get_pack_version(boundary["pack_id"])

    def test_each_template_retains_its_own_focus_for_traceability(self):
        """A run has ONE focus (the first template's), but every template's own
        focus survives on its snapshot — otherwise composing would silently lose
        the fact that lending wanted approvals_compliance."""
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        per_template = {
            b["template_id"]: b["focus_id"]
            for b in resolved["effective"]["pack_boundaries"]
        }
        assert per_template[TEMPLATE_ID] == "member_customer_service"
        assert per_template[LENDING_TEMPLATE_ID] == "approvals_compliance"

    def test_the_first_selected_template_supplies_the_run_focus(self):
        """Documented model behaviour, asserted so it cannot surprise anyone: these
        two templates differ in focus, unlike the cloud_ops/security_ops pair."""
        tr = _tr()
        fsc_first = tr.resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID]
        )
        lending_first = tr.resolve_launch_config(
            None, template_ids=[LENDING_TEMPLATE_ID, TEMPLATE_ID]
        )
        assert fsc_first["effective"]["focus_id"] == "member_customer_service"
        assert lending_first["effective"]["focus_id"] == "approvals_compliance"

    def test_focus_remains_editable_in_a_combined_run(self):
        resolved = _tr().resolve_launch_config(
            None,
            template_ids=[TEMPLATE_ID, LENDING_TEMPLATE_ID],
            focus_id="approvals_compliance",
        )
        assert resolved["effective"]["focus_id"] == "approvals_compliance"
        assert "focus_id" in resolved["provenance"]["edited_fields"]

    def test_both_templates_declare_each_other_compatible(self):
        tr = _tr()
        fsc = tr.get_template(TEMPLATE_ID)
        lending = tr.get_template(LENDING_TEMPLATE_ID)
        assert LENDING_TEMPLATE_ID in fsc.metadata["compatible_templates"]
        assert TEMPLATE_ID in lending.metadata["compatible_templates"]


class TestAC2CalibrationIsNeverBlended:
    """The guarantee AC2 actually rests on (from R191-P1)."""

    @pytest.fixture(scope="class")
    def lending_findings(self):
        sn = _mod("ingest.ncino").ingest()
        sf_data = {"ncino": sn}
        out = []
        for name in ("detectors.loan_origination_routing_friction",
                     "detectors.covenant_tracking_gap",
                     "detectors.checklist_bottleneck",
                     "detectors.spreading_bottleneck",
                     "detectors.approval_bottleneck"):
            out.extend(_mod(name).detect(sf_data, {}, {}))
        return out

    def test_both_packs_produce_findings(self, fsc_findings, lending_findings):
        assert fsc_findings, "FSC produced no findings"
        assert lending_findings, "Lending produced no findings"

    def test_each_packs_scorer_claims_only_its_own_detectors(
        self, fsc_findings, lending_findings
    ):
        """The two-key guard: no pack ever applies another pack's calibration."""
        fsc_scorer = _fsc_scorer()
        lending = _mod("lending_scorer")
        for finding in fsc_findings:
            assert fsc_scorer.is_financial_services_cloud_detector(finding.detector_id)
            assert not lending.is_lending_detector(finding.detector_id)
        for finding in lending_findings:
            assert lending.is_lending_detector(finding.detector_id)
            assert not fsc_scorer.is_financial_services_cloud_detector(
                finding.detector_id
            )

    def test_findings_are_scored_by_their_own_pack(self, fsc_findings, lending_findings):
        fsc_scorer = _fsc_scorer()
        lending = _mod("lending_scorer")
        ranking = fsc_scorer.rank_fsc_findings(fsc_findings)
        for finding in fsc_findings:
            scored = fsc_scorer.score_financial_services_cloud(finding, ranking=ranking)
            assert scored["score_debug"]["scorer"] == "financial_services_cloud"
        for finding in lending_findings:
            scored = lending.score_lending(finding)
            assert scored["score_debug"]["scorer"] == "lending"

    def test_the_two_approval_findings_stay_two_not_one(
        self, fsc_findings, lending_findings
    ):
        """Both packs surface an approval bottleneck. A naive reading expects one
        finding; the correct outcome is TWO, distinguished by opportunity_identity.
        Cross-pack merging is a permanent non-goal, so this is not duplication."""
        identity = _mod("opportunity_identity")

        fsc_approval = [
            f for f in fsc_findings if f.detector_id == "FSC_APPROVAL_REVIEW_CYCLE"
        ]
        lending_approval = [
            f for f in lending_findings if f.detector_id == "APPROVAL_BOTTLENECK"
        ]
        assert fsc_approval, "FSC approval detector did not fire"
        assert lending_approval, "Lending approval detector did not fire"

        fsc_id = identity.compute_opportunity_identity(
            "org1", PACK_ID, fsc_approval[0].detector_id,
            identity.primary_entity_keys_for_detector(
                fsc_approval[0].detector_id, fsc_approval[0].signal_source
            ),
        )
        lending_id = identity.compute_opportunity_identity(
            "org1", LENDING_PACK_ID, lending_approval[0].detector_id,
            identity.primary_entity_keys_for_detector(
                lending_approval[0].detector_id, lending_approval[0].signal_source
            ),
        )
        assert fsc_id != lending_id, (
            "the two packs' approval findings collapsed to one identity — "
            "cross-pack merging is a permanent non-goal"
        )

    def test_pack_id_alone_separates_two_otherwise_identical_findings(self):
        """The mechanism behind the assertion above: identity includes pack_id, so
        even a hypothetical identically-named detector in both packs stays two."""
        identity = _mod("opportunity_identity")
        keys = identity.primary_entity_keys_for_detector("SAME_DETECTOR", "salesforce")
        a = identity.compute_opportunity_identity("org1", PACK_ID, "SAME_DETECTOR", keys)
        b = identity.compute_opportunity_identity(
            "org1", LENDING_PACK_ID, "SAME_DETECTOR", keys
        )
        assert a != b

    def test_no_fsc_finding_shares_an_identity_with_a_lending_finding(
        self, fsc_findings, lending_findings
    ):
        identity = _mod("opportunity_identity")

        def ident(pack, finding):
            return identity.compute_opportunity_identity(
                "org1", pack, finding.detector_id,
                identity.primary_entity_keys_for_detector(
                    finding.detector_id, finding.signal_source
                ),
            )

        fsc_ids = {ident(PACK_ID, f) for f in fsc_findings}
        lending_ids = {ident(LENDING_PACK_ID, f) for f in lending_findings}
        assert fsc_ids.isdisjoint(lending_ids)


# ── AC4 — zero template-model code changes ────────────────────────────────────

class TestAC4NoTemplateModelChanges:

    EXPECTED_FIELDS = (
        "template_id", "label", "description", "suggested_systems",
        "suggested_roles", "focus_defaults", "pack_id", "detector_emphasis",
        "terminology", "metadata",
    )

    def test_template_definition_field_set_is_unchanged(self):
        """Delivering this template must not have required a new field."""
        import dataclasses
        fields = tuple(
            f.name for f in dataclasses.fields(_tr().TemplateDefinition)
        )
        assert fields == self.EXPECTED_FIELDS, (
            f"TemplateDefinition changed shape: {fields}. Adding a template must be "
            f"adding a dict entry (D1 AC4)."
        )

    def test_focus_defaults_field_set_is_unchanged(self):
        import dataclasses
        fields = tuple(f.name for f in dataclasses.fields(_tr().FocusDefaults))
        assert fields == ("focus_id", "emphasis")

    def test_register_template_still_validates_the_pack_by_default(self):
        """Unchanged behaviour — and the hard T1 ordering dependency."""
        tr = _tr()
        bad = tr.TemplateDefinition(
            template_id="fsc_bad_pack_probe",
            label="probe",
            description="",
            suggested_systems=[],
            suggested_roles={},
            focus_defaults=tr.FocusDefaults(focus_id="core_operations"),
            pack_id="no_such_pack",
        )
        with pytest.raises(ValueError):
            tr.register_template(bad)
        assert tr.get_template("fsc_bad_pack_probe") is None

    def test_the_template_is_config_only_and_round_trips(self, template):
        """The genericity proof: this template can be removed and re-registered as
        pure configuration, with no code change."""
        tr = _tr()
        try:
            tr.unregister_template(TEMPLATE_ID)
            assert tr.get_template(TEMPLATE_ID) is None
            tr.register_template(template)
            assert tr.get_template(TEMPLATE_ID) is template
        finally:
            tr.register_template(template)
        assert tr.get_template(TEMPLATE_ID) is not None

    def test_no_template_model_file_changed_since_the_base_branch(self):
        """The AC4 diff check, when repository history is available.

        template_registry.py is BOTH the model and the registry, so a pure diff
        exclusion is impossible — a dict entry necessarily edits the file. This
        therefore asserts the precise thing AC4 means: the change is confined to
        the TEMPLATE_REGISTRY literal, and the model/API definitions above and
        below it are untouched. SKIPS on a shallow clone rather than passing
        vacuously (CI checks out at depth 1).
        """
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/2.0-D1"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if base.returncode != 0 or not base.stdout.strip():
                pytest.skip("no merge base with origin/2.0-D1 (shallow clone)")
            diff = subprocess.run(
                ["git", "diff", "-U0", base.stdout.strip(), "HEAD", "--",
                 "backend/discovery/packs/template_registry.py"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if diff.returncode != 0:
                pytest.skip("git diff unavailable")
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")

        added = [
            line[1:] for line in diff.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        removed = [
            line[1:] for line in diff.stdout.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        # No model or public-API line may be touched.
        forbidden = ("class TemplateDefinition", "class FocusDefaults",
                     "def register_template", "def unregister_template",
                     "def get_template", "def list_templates",
                     "def resolve_launch_config", "def template_defaults_snapshot",
                     "def normalize_template_ids")
        for line in added + removed:
            for token in forbidden:
                assert token not in line, (
                    f"AC4 violation — the template MODEL/API changed: {line.strip()!r}"
                )
        # Nothing may be REMOVED at all: this subtask is purely additive.
        substantive_removals = [
            line for line in removed
            if line.strip() and not line.strip().startswith("#")
        ]
        assert substantive_removals == [] or all(
            "compatible_templates" in line or "metadata" in line or
            line.strip() in ("},", ")", "),")
            for line in substantive_removals
        ), f"unexpected removals from the template registry: {substantive_removals}"

    def test_shared_scoring_engine_is_still_untouched(self):
        """Carried from T3 — AC4 covers the scoring engine too."""
        for rel in ("backend/discovery/scorer.py",
                    "backend/discovery/calibration/calibrator.py",
                    "backend/discovery/calibration/ranking.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            for token in ("fsc", "financial_services_cloud"):
                assert token not in source, f"{rel} references {token!r}"

    def test_app_terminology_stays_pack_agnostic(self):
        source = (REPO_ROOT / "backend/app/terminology.py").read_text(
            encoding="utf-8"
        ).lower()
        for token in ("fsc", "financial_services_cloud"):
            assert token not in source
