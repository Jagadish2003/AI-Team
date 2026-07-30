"""Contract tests for 2.0-D2 T4 — Honest system anchoring using shipped connectors only.

T1 shipped the Insurance TEMPLATE (systems, roles, focus, terminology) and proved
its suggested systems anchor on shipped connectors. T4 adds the associated
Insurance INDUSTRY registry entry and folds both — template AND industry — into
the R191-R1 anchor-on-shipped cross-check, with a negative control proving the
gate actively rejects an unimplemented insurance platform.

What this file proves, mapped to the task:

  Objective            ``TestObjectiveShippedOnly`` — every system the Insurance
                       template and industry expose resolves to a shipped ingestor
                       or an approved implementation alias.
  Suggested systems    ``TestSuggestedSystems`` — the exact role shape the task
                       names (Service Cloud SoR; ServiceNow/Jira workflow;
                       Slack/Teams operational signal; Confluence/SharePoint docs),
                       every suggested system carrying a valid role.
  Industry config      ``TestIndustryConfiguration`` — the insurance industry entry
                       uses service_cloud as the primary pack hint and the SAME
                       canonical ids and roles as the template; the two never
                       disagree about the primary pack or the workflow shape.
  Unsupported          ``TestUnsupportedPlatforms`` — no Guidewire / Duck Creek /
                       SAP / Dynamics 365 / Zendesk is preselected or advertised as
                       connectable; where such a platform is visible at all it is a
                       non-connectable roadmap label.
  Registry enforcement ``TestRegistryEnforcement`` — the extended R191-R1
                       cross-check passes and the negative control proves the gate
                       rejects an unimplemented insurance platform.
  Definition of done   ``TestDefinitionOfDone`` — every exposed system has a shipped
                       ingestion path / valid alias / non-connectable roadmap, and
                       no new connector was built (out of scope).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app import connector_roadmap
from discovery.packs.industry_registry import (
    INDUSTRY_REGISTRY,
    get_industry,
    get_pack_hints,
    get_recommended_systems,
    get_roadmap_systems,
    get_system_defaults,
)
from discovery.packs.template_registry import get_template

INSURANCE_ID = "insurance"
PRIMARY_PACK = "service_cloud"

# The valid SystemRole literals (frontend/src/types/stack_builder.ts), mirrored
# by test_stack_builder_api.py::test_all_role_values_valid.
VALID_ROLES = {
    "system_of_record",
    "workflow_system",
    "operational_signal_source",
    "documentation_system",
    "engineering_change_system",
}

# The systems the task names as the suitable Insurance configuration, with the
# role each must carry.
EXPECTED_ROLE_SHAPE = {
    "salesforce_sc": "system_of_record",
    "servicenow": "workflow_system",
    "jira": "workflow_system",
    "teams": "operational_signal_source",
    "slack": "operational_signal_source",
    "confluence": "documentation_system",
    "sharepoint": "documentation_system",
}

# Insurance-related platforms the task forbids advertising unless their ingestor
# ships. None ships today.
FORBIDDEN_INSURANCE_PLATFORMS = (
    "guidewire",
    "duck_creek",
    "duckcreek",
    "sap",
    "dynamics365",
    "zendesk",
)


def _backend_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "discovery" / "packs" / "template_registry.py").is_file():
            return candidate
    raise RuntimeError("could not locate the backend root")


BACKEND_ROOT = _backend_root()


def _load_r191_guard():
    """Load the R191-R1 cross-check module by path and reuse its real shipped-
    ingestor discovery, so this file asserts against the SAME implementation set
    the CI gate uses rather than a parallel copy."""
    path = (
        BACKEND_ROOT / "tests" / "contract"
        / "test_r191_r1_ingestor_registry_enforcement.py"
    )
    spec = importlib.util.spec_from_file_location("_r191_guard_d2_t4", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load_r191_guard()


@pytest.fixture(scope="module")
def implemented(guard):
    return guard._implemented_connector_ids()


@pytest.fixture(scope="module")
def template():
    defn = get_template(INSURANCE_ID)
    assert defn is not None, "the Insurance template is not registered"
    return defn


@pytest.fixture(scope="module")
def industry():
    config = get_industry(INSURANCE_ID)
    assert config is not None, "the Insurance industry is not registered"
    return config


# ── Objective — shipped ingestors only ─────────────────────────────────────────

class TestObjectiveShippedOnly:

    def test_template_systems_all_resolve_to_a_shipped_ingestor(
        self, guard, implemented, template
    ):
        missing = [
            s for s in template.suggested_systems
            if guard._missing_implementation(s, implemented)
        ]
        assert missing == [], missing

    def test_industry_defaults_all_resolve_to_a_shipped_ingestor(
        self, guard, implemented, industry
    ):
        missing = [
            s for s in industry.system_defaults
            if guard._missing_implementation(s, implemented)
        ]
        assert missing == [], missing

    def test_industry_recommendations_all_resolve_to_a_shipped_ingestor(
        self, guard, implemented, industry
    ):
        missing = [
            s for s in industry.recommended_systems
            if guard._missing_implementation(s, implemented)
        ]
        assert missing == [], missing

    def test_salesforce_sc_is_an_approved_implementation_alias(self, guard, implemented):
        """salesforce_sc is not itself an ingestor module — it is an approved
        alias for the shipped base Salesforce ingestor."""
        assert "salesforce_sc" in guard.IMPLEMENTATION_ALIASES
        assert guard.IMPLEMENTATION_ALIASES["salesforce_sc"] == {"salesforce"}
        assert not guard._missing_implementation("salesforce_sc", implemented)


# ── Suggested systems — role shape the task names ──────────────────────────────

class TestSuggestedSystems:

    def test_the_template_uses_the_suggested_configuration(self, template):
        for system_id, role in EXPECTED_ROLE_SHAPE.items():
            assert system_id in template.suggested_systems, system_id
            assert template.suggested_roles[system_id] == role, system_id

    def test_service_cloud_is_the_system_of_record(self, template):
        sor = [
            s for s, r in template.suggested_roles.items()
            if r == "system_of_record"
        ]
        assert sor == ["salesforce_sc"], sor

    def test_servicenow_and_jira_are_the_workflow_systems(self, template):
        assert template.suggested_roles["servicenow"] == "workflow_system"
        assert template.suggested_roles["jira"] == "workflow_system"

    def test_slack_or_teams_are_operational_signal_sources(self, template):
        assert template.suggested_roles["teams"] == "operational_signal_source"
        assert template.suggested_roles["slack"] == "operational_signal_source"

    def test_confluence_or_sharepoint_are_documentation_systems(self, template):
        assert template.suggested_roles["confluence"] == "documentation_system"
        assert template.suggested_roles["sharepoint"] == "documentation_system"

    def test_every_suggested_system_has_a_valid_role(self, template):
        for system_id in template.suggested_systems:
            role = template.suggested_roles.get(system_id)
            assert role in VALID_ROLES, (system_id, role)


# ── Industry configuration ─────────────────────────────────────────────────────

class TestIndustryConfiguration:

    def test_insurance_industry_is_registered_and_listed(self):
        assert INSURANCE_ID in INDUSTRY_REGISTRY
        assert get_industry(INSURANCE_ID).label == "Insurance"

    def test_primary_pack_hint_is_service_cloud(self, industry):
        assert industry.pack_hints[0] == PRIMARY_PACK
        assert get_pack_hints(INSURANCE_ID)[0] == PRIMARY_PACK

    def test_pack_hints_name_no_unshipped_or_domain_insurance_pack(self, industry):
        from discovery.packs.pack_config import PACK_REGISTRY
        for hint in industry.pack_hints:
            assert hint in PACK_REGISTRY, f"pack_hint '{hint}' is not registered"
        assert not any("insurance" in h for h in industry.pack_hints), (
            "no insurance-specific pack ships — the industry must not hint one"
        )

    def test_industry_and_template_use_the_same_canonical_system_ids(
        self, industry, template
    ):
        assert set(industry.system_defaults) == set(template.suggested_systems)

    def test_industry_roles_match_the_template_roles(self, industry, template):
        for system_id, defaults in industry.system_defaults.items():
            assert defaults.role == template.suggested_roles[system_id], system_id

    def test_industry_and_template_agree_on_the_primary_pack(self, industry, template):
        assert industry.pack_hints[0] == template.pack_id == PRIMARY_PACK

    def test_recommended_systems_are_a_subset_of_the_canonical_ids(
        self, industry, template
    ):
        assert set(industry.recommended_systems) <= set(template.suggested_systems)

    def test_every_default_role_priority_and_focus_are_valid(self, industry):
        valid_priorities = {"primary", "secondary", "optional"}
        valid_tags = {
            "intake_requests", "service_casework", "approvals",
            "backlog_work_queues", "compliance_risk", "documents_knowledge",
            "handoffs_routing", "communications", "change_release", "data_analytics",
        }
        for system_id, d in industry.system_defaults.items():
            assert d.role in VALID_ROLES, (system_id, d.role)
            assert d.priority in valid_priorities, (system_id, d.priority)
            assert 0 < len(d.workflow_focus) <= 3, system_id
            assert set(d.workflow_focus) <= valid_tags, (system_id, d.workflow_focus)

    def test_industry_has_a_primary_system_of_record(self, industry):
        primaries = [
            s for s, d in industry.system_defaults.items()
            if d.role == "system_of_record" and d.priority == "primary"
        ]
        assert primaries == ["salesforce_sc"], primaries

    def test_llm_context_names_insurance_and_bars_automated_decisions(self, industry):
        suffix = industry.llm_context_suffix.lower()
        assert "insurance" in suffix
        assert "claims" in suffix and "underwriting" in suffix
        assert "never suggest automated" in suffix

    def test_industry_endpoint_accessors_reflect_the_entry(self):
        defaults = get_system_defaults(INSURANCE_ID, "salesforce_sc")
        assert defaults is not None and defaults.role == "system_of_record"
        recs = get_recommended_systems(INSURANCE_ID, [])
        assert recs and len(recs) <= 3


# ── Unsupported platforms ───────────────────────────────────────────────────────

class TestUnsupportedPlatforms:

    def test_no_forbidden_platform_is_a_template_default(self, template):
        anchored = set(template.suggested_systems) | set(template.suggested_roles)
        offenders = anchored & set(FORBIDDEN_INSURANCE_PLATFORMS)
        assert not offenders, offenders

    def test_no_forbidden_platform_is_an_industry_default_or_recommendation(
        self, industry
    ):
        anchored = set(industry.system_defaults) | set(industry.recommended_systems)
        offenders = anchored & set(FORBIDDEN_INSURANCE_PLATFORMS)
        assert not offenders, offenders

    def test_insurance_declares_no_roadmap_systems(self, industry):
        """The story commits no insurance-platform connector to a release, so a
        fabricated roadmap target would be dishonest — the industry declares none."""
        assert industry.roadmap_systems == []
        assert get_roadmap_systems(INSURANCE_ID) == []

    def test_no_forbidden_platform_resolves_to_a_shipped_ingestor(
        self, guard, implemented
    ):
        for platform in FORBIDDEN_INSURANCE_PLATFORMS:
            assert guard._missing_implementation(platform, implemented), platform

    def test_roadmap_labelled_platforms_are_non_connectable(self):
        """SAP / Dynamics 365 / Zendesk are catalog roadmap tiles: the honest
        roadmap mechanism marks them non-connectable, never shipped."""
        for platform in ("sap", "dynamics365", "zendesk"):
            assert connector_roadmap.is_roadmap(platform), platform
            assert not connector_roadmap.is_shipped(platform), platform
            assert connector_roadmap.roadmap_block_message(platform).strip()


# ── Registry enforcement (the R191-R1 cross-check + negative control) ──────────

class TestRegistryEnforcement:

    def test_the_extended_cross_check_passes(self, guard):
        guard.test_registry_connectable_entries_have_shipped_ingestors()
        guard.test_connectable_catalog_tiles_have_shipped_ingestors()
        guard.test_unimplemented_catalog_tiles_stay_roadmap_not_connectable()
        guard.test_insurance_template_suggested_systems_have_shipped_ingestors()
        guard.test_insurance_industry_defaults_and_recommendations_have_shipped_ingestors()
        guard.test_insurance_template_and_industry_agree_on_pack_and_workflow_shape()

    def test_the_gate_actively_rejects_an_unimplemented_platform(self, guard):
        """Delegates to the mutation-based negative control living in the guard
        module (probe industry → real cross-check goes red → registry restored)."""
        guard.test_gate_rejects_an_unimplemented_insurance_platform()
        guard.test_removing_the_probe_left_the_real_registry_green()

    def test_pure_helper_negative_control_over_the_insurance_config(
        self, guard, implemented, industry
    ):
        """Had the Insurance industry anchored on Guidewire, the gate would have
        flagged exactly Guidewire — proven without mutating the real registry."""
        hypothetical = list(industry.system_defaults) + ["guidewire"]
        flagged = [
            s for s in hypothetical
            if guard._missing_implementation(s, implemented)
        ]
        assert flagged == ["guidewire"], flagged


# ── Definition of done ──────────────────────────────────────────────────────────

class TestDefinitionOfDone:

    def test_every_exposed_system_is_shipped_or_a_valid_alias(
        self, guard, implemented, template, industry
    ):
        exposed = (
            set(template.suggested_systems)
            | set(industry.system_defaults)
            | set(industry.recommended_systems)
        )
        unbacked = sorted(
            s for s in exposed if guard._missing_implementation(s, implemented)
        )
        assert unbacked == [], unbacked

    def test_no_insurance_platform_connector_was_built(self):
        """Building a connector is out of scope — no insurance-platform ingestor
        module may have been added under discovery/ingest or connectors/db."""
        ingest_dir = BACKEND_ROOT / "discovery" / "ingest"
        db_dir = BACKEND_ROOT / "connectors" / "db"
        offenders = [
            p.name
            for root in (ingest_dir, db_dir)
            for p in root.rglob("*.py")
            if any(
                token in p.name.lower()
                for token in ("guidewire", "duck_creek", "duckcreek", "insurance")
            )
        ]
        assert offenders == [], offenders
