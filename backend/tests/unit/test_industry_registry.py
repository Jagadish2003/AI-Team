"""R1.9.1-R1 T1 — industry_registry.py "anchor-on-shipped" re-anchor tests.

Covers the manufacturing / logistics_supply_chain re-anchor: SAP and
Dynamics 365 no longer appear as connectable defaults (they have no shipped
ingestor), shipped sources that genuinely fit (databases, documents, Teams)
take their place, and SAP/Dynamics 365 are still represented — as an explicit,
non-connectable roadmap entry (target 2.0.1) via the new roadmap_systems
field.

Pure-config module (no DB, no app import) — runs standalone, matching
CLAUDE.md's tests/unit/ purpose ("unit tests for individual backend modules").

Acceptance Criteria covered (R1.9.1-R1)
----------------------------------------
AC1 (partial — the two industries in this task's scope): no system_default or
    recommended_system for manufacturing/logistics_supply_chain references a
    connector without a shipped ingestor. The full AC1 guarantee (every
    industry, enforced by a dynamically-discovered CI cross-check against
    backend/discovery/ingest/) is a separate, later task.
AC2: SAP/Dynamics 365 are represented as roadmap (target 2.0.1) for these two
     industries, never as a connectable default — so no run can select them.
"""
from __future__ import annotations

from discovery.packs.industry_registry import (
    INDUSTRY_REGISTRY,
    IndustryConfig,
    RoadmapSystemConfig,
    get_industry,
    get_pack_hints,
    get_recommended_systems,
    get_roadmap_systems,
    get_system_defaults,
    list_industries,
)

# The two SAP/D365 alternates the story's re-anchoring removes from these
# industries' connectable defaults.
_ABSENT_CONNECTORS = frozenset({"sap", "dynamics365"})

# Shipped sources the story specifies as the replacement anchor.
_REANCHOR_TARGETS = ("sqlserver", "documents", "teams")

_REANCHORED_INDUSTRIES = ("manufacturing", "logistics_supply_chain")

# The valid enums from frontend/src/types/stack_builder.ts — kept here as a
# plain literal set (not imported; this module has no TS/JSON bridge) so a
# newly invented, invalid tag/role/priority is caught rather than silently
# shipped.
_VALID_ROLES = frozenset(
    {
        "system_of_record",
        "workflow_system",
        "operational_signal_source",
        "documentation_system",
        "engineering_change_system",
    }
)
_VALID_PRIORITIES = frozenset({"primary", "secondary", "optional"})
_VALID_WORKFLOW_TAGS = frozenset(
    {
        "intake_requests",
        "service_casework",
        "approvals",
        "backlog_work_queues",
        "compliance_risk",
        "documents_knowledge",
        "handoffs_routing",
        "communications",
        "change_release",
        "data_analytics",
    }
)


# ---------------------------------------------------------------------------
# AC1 / AC2 — the two re-anchored industries
# ---------------------------------------------------------------------------


def test_manufacturing_and_logistics_no_longer_anchor_on_sap_or_d365():
    """SAP/Dynamics 365 are gone from the connectable surface for both
    industries: not a system_default key, not a recommended_system."""
    for industry_id in _REANCHORED_INDUSTRIES:
        config = get_industry(industry_id)
        assert config is not None, industry_id

        absent_in_defaults = _ABSENT_CONNECTORS & set(config.system_defaults)
        assert not absent_in_defaults, (
            f"{industry_id}: {absent_in_defaults} still anchored as a "
            "connectable system_default"
        )

        absent_in_recs = _ABSENT_CONNECTORS & set(config.recommended_systems)
        assert not absent_in_recs, (
            f"{industry_id}: {absent_in_recs} still listed in recommended_systems"
        )


def test_manufacturing_and_logistics_re_anchor_on_shipped_sources():
    """Databases (sqlserver), documents, and Teams — the shipped sources the
    story specifies — are now genuine connectable defaults for both industries."""
    for industry_id in _REANCHORED_INDUSTRIES:
        config = get_industry(industry_id)
        assert config is not None, industry_id
        missing = [s for s in _REANCHOR_TARGETS if s not in config.system_defaults]
        assert not missing, f"{industry_id}: missing re-anchor targets {missing}"


def test_manufacturing_and_logistics_still_retain_a_primary_system_of_record():
    """Removing SAP/D365 must not leave the industry with no primary anchor —
    the Salesforce variants already covered this role and are untouched."""
    for industry_id in _REANCHORED_INDUSTRIES:
        config = get_industry(industry_id)
        primaries = [
            sid
            for sid, d in config.system_defaults.items()
            if d.role == "system_of_record" and d.priority == "primary"
        ]
        assert primaries, f"{industry_id}: no primary system_of_record remains"


def test_manufacturing_and_logistics_declare_sap_and_d365_as_roadmap():
    """SAP and Dynamics 365 are still represented — as an explicit,
    non-connectable roadmap entry targeting 2.0.1 (AC2)."""
    for industry_id in _REANCHORED_INDUSTRIES:
        roadmap = get_roadmap_systems(industry_id)
        roadmap_ids = {r.system_id for r in roadmap}
        assert roadmap_ids == _ABSENT_CONNECTORS, (
            f"{industry_id}: expected roadmap systems {_ABSENT_CONNECTORS}, "
            f"got {roadmap_ids}"
        )
        for entry in roadmap:
            assert entry.target_release == "2.0.1", entry
            assert entry.label, "roadmap entry must carry a display label"
            assert entry.reason, "roadmap entry must carry a user-facing reason"


def test_manufacturing_and_logistics_pack_hints_include_sqlserver_opsignal():
    """The pack that actually serves the new database anchor is hinted."""
    for industry_id in _REANCHORED_INDUSTRIES:
        assert "sqlserver_opsignal" in get_pack_hints(industry_id), industry_id


def test_get_system_defaults_returns_none_for_removed_connectors():
    """The accessor used by the Stack Builder API also reflects the removal —
    not just direct dict access."""
    for industry_id in _REANCHORED_INDUSTRIES:
        assert get_system_defaults(industry_id, "sap") is None
        assert get_system_defaults(industry_id, "dynamics365") is None


def test_recommended_systems_for_reanchored_industries_are_shipped():
    """get_recommended_systems() never surfaces a roadmap system as a
    suggested addition."""
    for industry_id in _REANCHORED_INDUSTRIES:
        recs = get_recommended_systems(industry_id, selected_ids=[])
        assert not (_ABSENT_CONNECTORS & set(recs)), (
            f"{industry_id}: recommended_systems surfaced a roadmap system"
        )


# ---------------------------------------------------------------------------
# Structural invariant — a system is either shipped-and-connectable or
# roadmap-and-not, never both, for EVERY industry (not just the two in scope).
# Documented in the module docstring; this test enforces it going forward.
# ---------------------------------------------------------------------------


def test_no_industry_double_lists_a_system_as_both_connectable_and_roadmap():
    for config in list_industries():
        roadmap_ids = {r.system_id for r in config.roadmap_systems}
        connectable_ids = set(config.system_defaults) | set(config.recommended_systems)
        overlap = roadmap_ids & connectable_ids
        assert not overlap, (
            f"{config.industry_id}: {overlap} listed as both connectable and roadmap"
        )


# ---------------------------------------------------------------------------
# Regression guard — industries NOT in this task's scope are untouched.
# ---------------------------------------------------------------------------


def test_untouched_industries_have_no_roadmap_systems_and_are_unaffected():
    """Only manufacturing/logistics_supply_chain gain roadmap_systems in this
    change; every other industry's connectable surface is unaffected."""
    untouched = set(INDUSTRY_REGISTRY) - set(_REANCHORED_INDUSTRIES)
    assert untouched, "sanity: expected other industries to exist"
    for industry_id in untouched:
        config = get_industry(industry_id)
        assert config.roadmap_systems == [], (
            f"{industry_id}: unexpectedly declares roadmap_systems"
        )


def test_all_eight_industries_still_present():
    assert len(INDUSTRY_REGISTRY) == 8, sorted(INDUSTRY_REGISTRY)


# ---------------------------------------------------------------------------
# Enum sanity — role/priority/workflow_focus on the new entries use only
# values recognised by frontend/src/types/stack_builder.ts.
# ---------------------------------------------------------------------------


def test_new_system_default_entries_use_valid_enum_values():
    for industry_id in _REANCHORED_INDUSTRIES:
        config = get_industry(industry_id)
        for system_id in _REANCHOR_TARGETS:
            entry = config.system_defaults[system_id]
            assert entry.role in _VALID_ROLES, (industry_id, system_id, entry.role)
            assert entry.priority in _VALID_PRIORITIES, (
                industry_id, system_id, entry.priority,
            )
            assert entry.workflow_focus, (industry_id, system_id)
            assert len(entry.workflow_focus) <= 3, (industry_id, system_id)
            invalid_tags = set(entry.workflow_focus) - _VALID_WORKFLOW_TAGS
            assert not invalid_tags, (industry_id, system_id, invalid_tags)


# ---------------------------------------------------------------------------
# get_roadmap_systems() accessor — mirrors get_pack_hints()/
# get_recommended_systems()'s not-found/empty contract.
# ---------------------------------------------------------------------------


def test_get_roadmap_systems_returns_empty_list_for_unknown_industry():
    assert get_roadmap_systems("not-a-real-industry") == []


def test_get_roadmap_systems_returns_empty_list_when_none_declared():
    assert get_roadmap_systems("financial_services") == []


def test_roadmap_system_config_is_the_declared_dataclass_shape():
    roadmap = get_roadmap_systems("manufacturing")
    assert all(isinstance(r, RoadmapSystemConfig) for r in roadmap)


def test_industry_config_default_roadmap_systems_is_empty_list_not_shared():
    """default_factory=list must not share one mutable list across instances."""
    a = IndustryConfig("a", "A", [], {}, [], "")
    b = IndustryConfig("b", "B", [], {}, [], "")
    a.roadmap_systems.append(
        RoadmapSystemConfig("x", "X", "2.0.1", "test")
    )
    assert b.roadmap_systems == []
