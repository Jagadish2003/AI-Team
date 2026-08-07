"""R1.9.1-R1 T1/T2/T3/T4 — industry_registry.py "anchor-on-shipped" re-anchor tests.

T1 covers the manufacturing / logistics_supply_chain re-anchor: SAP and
Dynamics 365 no longer appear as connectable defaults (they have no shipped
ingestor), shipped sources that genuinely fit (databases, documents, Teams)
take their place, and SAP/Dynamics 365 are still represented — as an explicit,
non-connectable roadmap entry (target 2.0.1) via the roadmap_systems field.

T2 covers the technology re-anchor: GitLab (no shipped ingestor) moves to
roadmap_systems (target "unscheduled" — the story never commits GitLab to
2.0.1 the way it does SAP/D365/Kafka/Windows Event Log/Health Cloud), GitHub
(shipped) stays a connectable "optional" default unchanged, and databases
(sqlserver) are added as a genuinely-fitting shipped source.

T3 covers the remaining five industries (financial_services, public_sector,
retail_commerce, healthcare, energy_utilities): databases (sqlserver) added
as a connectable default, completing "databases in every industry profile
where they are realistically core" across all eight industries. T3 is
scoped to databases ONLY; T6 completes AC1 by moving energy_utilities' remaining
pre-existing `sap` default to roadmap because SAP still has no shipped ingestor.

T4 covers three things: (1) Teams added wherever Slack is offered — the five
industries that had slack but not teams (financial_services, retail_commerce,
healthcare, energy_utilities, technology) now have both, mirroring slack's
priority/workflow_focus exactly, matching the pattern T1 already established
for manufacturing/logistics_supply_chain and public_sector's pre-existing
pairing; (2) reducing Salesforce-centric anchoring for technology — the story
names no specific industry/system (it references an external "C0-A §4"
findings document not available to this task), so per explicit direction this
elevates Jira from operational_signal_source/secondary to a co-primary
workflow_system/primary, reflecting technology's own llm_context_suffix
naming engineering-delivery friction as the primary signal category —
additive (Salesforce variants stay primary too), not a Salesforce removal;
(3) technology's pack_hints gain github_engineering (the pack that scores the
already-connectable github default — the honest-pack-list gap the story
names as its own example). The 1.9 cloud-ops/security-ops packs the story also
names for this sub-goal now have registered pack_ids, so technology includes
them and the tests assert every hint remains registered.

Pure-config module (no DB, no app import) — runs standalone, matching
the repo's tests/unit/ purpose ("unit tests for individual backend modules").

Acceptance Criteria covered (R1.9.1-R1)
----------------------------------------
AC1: no system_default or recommended_system references a connector without a
    shipped ingestor. The full registry/catalog guarantee is enforced by the
    dynamically-discovered R191-R1 T6 CI cross-check.
AC2: SAP/Dynamics 365 are represented as roadmap (target 2.0.1) for
     manufacturing/logistics_supply_chain, never as a connectable default —
     so no run can select them (T1).
AC3 (partial): the eight industries remain present and each has at least one
     honest primary system after T4's Jira elevation for technology (a
     full per-industry Stack Builder walkthrough test is a separate concern).
AC4: databases appear in the agreed industry profiles — manufacturing,
     logistics_supply_chain, technology (T1/T2), and now financial_services,
     public_sector, retail_commerce, healthcare, energy_utilities (T3), i.e.
     all eight industries; technology carries GitHub-optional and
     GitLab-as-roadmap (T2); Teams parity with Slack holds registry-wide and
     technology's pack list is honest about what it actually anchors (T4).
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

# The two SAP/D365 alternates the story's re-anchoring removes from
# manufacturing/logistics_supply_chain's connectable defaults (T1).
_ABSENT_CONNECTORS = frozenset({"sap", "dynamics365"})

# Every industry touched by a re-anchor task so far (T1 + T2) — used by the
# cross-industry regression guard below to know which industries are allowed
# to declare roadmap_systems / have changed defaults.
_TOUCHED_INDUSTRIES = frozenset(
    {"manufacturing", "logistics_supply_chain", "technology", "energy_utilities"}
)

# T3 — the five industries that gained a database anchor in this task, and
# every industry that now has one (T1/T2's three + T3's five = all eight).
_DATABASE_ONLY_INDUSTRIES = (
    "financial_services",
    "public_sector",
    "retail_commerce",
    "healthcare",
    "energy_utilities",
)
_ALL_INDUSTRIES_WITH_DATABASE = _DATABASE_ONLY_INDUSTRIES + (
    "manufacturing", "logistics_supply_chain", "technology",
)

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
# T2 — technology: GitLab -> roadmap, GitHub stays optional, databases added.
# ---------------------------------------------------------------------------


def test_technology_no_longer_anchors_on_gitlab():
    """GitLab is gone from the connectable surface: not a system_default key,
    not a recommended_system."""
    config = get_industry("technology")
    assert "gitlab" not in config.system_defaults
    assert "gitlab" not in config.recommended_systems


def test_technology_declares_gitlab_as_roadmap_unscheduled():
    """GitLab is still represented — as an explicit, non-connectable roadmap
    entry. Unlike SAP/D365 (T1, committed to 2.0.1), the story never commits
    GitLab to a release, so its target must NOT be fabricated as "2.0.1"."""
    roadmap = get_roadmap_systems("technology")
    roadmap_ids = {r.system_id for r in roadmap}
    assert roadmap_ids == {"gitlab"}
    entry = roadmap[0]
    assert entry.target_release != "2.0.1", (
        "GitLab has no committed release target in the story — must not "
        "borrow SAP/D365's 2.0.1 commitment"
    )
    assert entry.label
    assert entry.reason


def test_technology_github_stays_connectable_and_optional_unchanged():
    """GitHub has a shipped connector/pack and must remain a connectable
    default at 'optional' priority — the re-anchor must not touch it."""
    defaults = get_system_defaults("technology", "github")
    assert defaults is not None
    assert defaults.role == "engineering_change_system"
    assert defaults.priority == "optional"


def test_technology_re_anchors_on_a_shipped_database():
    """sqlserver — the shipped native-DB / sqlserver_opsignal source — is now
    a genuine connectable default for technology."""
    defaults = get_system_defaults("technology", "sqlserver")
    assert defaults is not None
    assert defaults.role in _VALID_ROLES
    assert defaults.priority in _VALID_PRIORITIES
    assert set(defaults.workflow_focus) <= _VALID_WORKFLOW_TAGS


def test_technology_pack_hints_include_sqlserver_opsignal():
    assert "sqlserver_opsignal" in get_pack_hints("technology")


def test_technology_recommended_systems_never_surface_gitlab():
    recs = get_recommended_systems("technology", selected_ids=[])
    assert "gitlab" not in recs


def test_get_system_defaults_returns_none_for_gitlab_in_technology():
    """The accessor used by the Stack Builder API also reflects the removal —
    not just direct dict access."""
    assert get_system_defaults("technology", "gitlab") is None


# ---------------------------------------------------------------------------
# T3 — databases added to every remaining industry profile where they are
# realistically core: financial_services, public_sector, retail_commerce,
# healthcare, energy_utilities.
# ---------------------------------------------------------------------------


def test_all_five_remaining_industries_gain_a_database_default():
    for industry_id in _DATABASE_ONLY_INDUSTRIES:
        defaults = get_system_defaults(industry_id, "sqlserver")
        assert defaults is not None, f"{industry_id}: missing sqlserver default"
        assert defaults.role in _VALID_ROLES, (industry_id, defaults.role)
        assert defaults.priority in _VALID_PRIORITIES, (industry_id, defaults.priority)
        assert defaults.workflow_focus, industry_id
        assert len(defaults.workflow_focus) <= 3, industry_id
        invalid_tags = set(defaults.workflow_focus) - _VALID_WORKFLOW_TAGS
        assert not invalid_tags, (industry_id, invalid_tags)


def test_all_five_remaining_industries_pack_hints_include_sqlserver_opsignal():
    for industry_id in _DATABASE_ONLY_INDUSTRIES:
        assert "sqlserver_opsignal" in get_pack_hints(industry_id), industry_id


def test_all_five_remaining_industries_recommend_the_new_database():
    for industry_id in _DATABASE_ONLY_INDUSTRIES:
        config = get_industry(industry_id)
        assert "sqlserver" in config.recommended_systems, industry_id


def test_all_eight_original_industries_carry_a_database_default():
    """T1/T2's three industries plus T3's five equal the eight industries that
    existed when AC4 ("databases in every industry profile where they are
    realistically core") was stated — each still carries a database default.

    2.0-D2 T4 later added a ninth industry, "insurance", which deliberately
    carries NO database default: it exists to mirror the Insurance TEMPLATE's
    shape exactly, and that template names no database source. So the AC4
    invariant is scoped to the eight it was written for, and the ninth is
    asserted to be present-but-DB-free rather than silently widening the rule.
    """
    assert set(_ALL_INDUSTRIES_WITH_DATABASE) <= set(INDUSTRY_REGISTRY)
    for industry_id in _ALL_INDUSTRIES_WITH_DATABASE:
        assert get_system_defaults(industry_id, "sqlserver") is not None, industry_id
    # The one industry (insurance) deliberately outside the database-anchor rule.
    assert set(INDUSTRY_REGISTRY) - set(_ALL_INDUSTRIES_WITH_DATABASE) == {"insurance"}
    assert get_system_defaults("insurance", "sqlserver") is None


def test_energy_utilities_moves_sap_to_roadmap_for_full_ac1():
    """T6 removes the last unshipped registry default: SAP is still represented
    for energy/utilities, but only as a non-connectable roadmap entry."""
    assert get_system_defaults("energy_utilities", "sap") is None
    assert "sap" not in get_industry("energy_utilities").recommended_systems

    roadmap = get_roadmap_systems("energy_utilities")
    assert {r.system_id for r in roadmap} == {"sap"}
    entry = roadmap[0]
    assert entry.label == "SAP"
    assert entry.target_release == "2.0.1"
    assert entry.reason


def test_database_only_industries_still_retain_existing_primary_anchors():
    """Adding a database default must not disturb each industry's existing
    primary system_of_record anchor(s)."""
    for industry_id in _DATABASE_ONLY_INDUSTRIES:
        config = get_industry(industry_id)
        primaries = [
            sid
            for sid, d in config.system_defaults.items()
            if d.role == "system_of_record" and d.priority == "primary"
        ]
        assert primaries, f"{industry_id}: no primary system_of_record remains"


# ---------------------------------------------------------------------------
# T4 — Teams available wherever Slack is offered; reduce Salesforce-centric
# anchoring for technology (Jira elevated to a co-primary workflow_system);
# technology's pack_hints gain github_engineering (the honest-pack-list gap
# the story names as its own example).
# ---------------------------------------------------------------------------

# Every industry that had `slack` but not `teams` before T4.
_TEAMS_ADDED_INDUSTRIES = (
    "financial_services",
    "retail_commerce",
    "healthcare",
    "energy_utilities",
    "technology",
)

# Industries that already had both slack and teams before T4 (T1's re-anchor
# gave manufacturing/logistics_supply_chain both; public_sector always had
# both) — Teams parity must hold for the whole registry, not just the five
# T4 touches directly.
_ALREADY_HAD_TEAMS = ("public_sector", "manufacturing", "logistics_supply_chain")


def test_teams_added_wherever_slack_is_offered():
    """Every industry with a slack default now has a teams default too,
    mirroring slack's exact priority and workflow_focus."""
    for industry_id in _TEAMS_ADDED_INDUSTRIES:
        config = get_industry(industry_id)
        slack = config.system_defaults.get("slack")
        teams = config.system_defaults.get("teams")
        assert slack is not None, f"{industry_id}: expected a slack default"
        assert teams is not None, f"{industry_id}: teams was not added"
        assert teams.role == slack.role
        assert teams.priority == slack.priority
        assert teams.workflow_focus == slack.workflow_focus


def test_teams_parity_holds_registry_wide():
    """No industry has slack without teams, anywhere in the registry —
    T1-T4 combined must leave the whole registry consistent, not just the
    industries each task directly touched."""
    for industry_id, config in INDUSTRY_REGISTRY.items():
        if "slack" in config.system_defaults:
            assert "teams" in config.system_defaults, (
                f"{industry_id}: has slack but not teams"
            )


def test_previously_teams_equipped_industries_unaffected_by_t4():
    """Industries that already had both slack and teams (T1's three, plus
    public_sector) are untouched by T4's own edits."""
    for industry_id in _ALREADY_HAD_TEAMS:
        config = get_industry(industry_id)
        slack = config.system_defaults["slack"]
        teams = config.system_defaults["teams"]
        assert teams.role == slack.role
        assert teams.priority == slack.priority
        assert teams.workflow_focus == slack.workflow_focus


def test_technology_jira_elevated_to_co_primary_workflow_system():
    """Reduce Salesforce-centric anchoring (T4): Jira is now an honest
    co-primary for technology, not a secondary signal source — Salesforce
    variants remain primary too (additive, not a removal)."""
    jira = get_system_defaults("technology", "jira")
    assert jira is not None
    assert jira.role == "workflow_system"
    assert jira.priority == "primary"


def test_technology_salesforce_variants_remain_primary_after_jira_elevation():
    """Elevating Jira must not demote the existing Salesforce anchors — this
    is diversification, not a Salesforce removal."""
    for system_id in ("salesforce", "salesforce_sc", "salesforce_rc"):
        defaults = get_system_defaults("technology", system_id)
        assert defaults is not None, system_id
        assert defaults.role == "system_of_record"
        assert defaults.priority == "primary"


def test_technology_pack_hints_include_github_engineering():
    """The honest-pack-list gap the story names as its own example: technology
    already anchors github as a connectable default, but the pack that scores
    its signal was missing from the hint list."""
    hints = get_pack_hints("technology")
    assert "github_engineering" in hints


def test_technology_pack_hints_include_registered_1_9_ops_packs():
    """Technology carries the 1.9 ops packs and every hint is registered."""
    from discovery.packs.pack_config import PACK_REGISTRY

    hints = get_pack_hints("technology")
    assert "cloud_ops" in hints
    assert "security_ops" in hints
    for hint in hints:
        assert hint in PACK_REGISTRY, f"pack_hint '{hint}' has no registered pack"


# ---------------------------------------------------------------------------
# Structural invariant — a system is either shipped-and-connectable or
# roadmap-and-not, never both, for EVERY industry (not just the ones in
# scope so far). Documented in the module docstring; this test enforces it
# going forward.
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
    """Only industries touched by a re-anchor task (T1: manufacturing/
    logistics_supply_chain; T2: technology; T6: energy_utilities) declare
    roadmap_systems; every other industry's connectable surface is unaffected."""
    untouched = set(INDUSTRY_REGISTRY) - _TOUCHED_INDUSTRIES
    assert untouched, "sanity: expected other industries to exist"
    for industry_id in untouched:
        config = get_industry(industry_id)
        assert config.roadmap_systems == [], (
            f"{industry_id}: unexpectedly declares roadmap_systems"
        )


def test_all_nine_industries_still_present():
    # Eight original industries + "insurance" (2.0-D2 T4).
    assert len(INDUSTRY_REGISTRY) == 9, sorted(INDUSTRY_REGISTRY)
    assert "insurance" in INDUSTRY_REGISTRY


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
