"""
industry_registry.py — SB-12 Task 12 Sprint 7
ENG-EA — Industry Registry for Stack Builder

Provides industry-aware configuration for the Guided Discovery Stack Builder.
Used by:
  - useSetupState (frontend) via /api/stack-builder/industries endpoint
  - SYSTEM_DEFAULT_ASSUMPTIONS (Sprint 8) — upgrade from generic defaults
    to industry-calibrated role and workflow focus suggestions
  - Recommended additions logic on Screen 4 (currently in DiscoveryPlanScreen)
  - LLM enrichment prompts — industry context string injected alongside pack context

Architecture note (Sprint 7 — confirmed May 2026):
  The frontend currently uses hardcoded INDUSTRIES and TEMPLATES arrays in
  DiscoveryFocusScreen. Sprint 8 will replace those with data fetched from
  /api/stack-builder/industries and /api/stack-builder/templates, making
  the frontend data-driven from this registry.

  The SYSTEM_DEFAULT_ASSUMPTIONS map in useSetupState references this module
  as the Sprint 8 source of truth for industry-aware role defaults. The
  "Sprint 8 story: pull from SB-3 IndustryConfig" note in useSetupState
  refers specifically to IndustryConfig.system_defaults defined here.

IndustryConfig fields:
  industry_id         — matches IndustryId from stack_builder.ts
  label               — display label (matches INDUSTRIES array in frontend)
  pack_hints          — list of pack IDs most relevant for this industry
                        used by pack selector (ENG-PACK-SELECTOR Sprint 5.1)
  system_defaults     — dict[system_id -> SystemDefaultConfig]
                        industry-calibrated role + workflow focus defaults
                        for each system. Replaces generic SYSTEM_DEFAULT_ASSUMPTIONS.
                        R191-R1 "anchor-on-shipped" rule: every key here MUST have
                        a shipped ingestor/connector — never a roadmap system. See
                        roadmap_systems below for the non-connectable counterpart.
  recommended_systems — system IDs to suggest as additions on Screen 4
                        when not already selected. Ordered by priority. Same
                        anchor-on-shipped rule as system_defaults.
  roadmap_systems     — (R191-R1) list[RoadmapSystemConfig] for systems this
                        industry genuinely wants but that have no shipped
                        ingestor yet (e.g. SAP/D365, target 2.0.1). Rendered as
                        an explicit roadmap label in Stack Builder / the
                        Integration Hub — never a connectable default, never
                        selectable by a run. Defaults to empty.
  llm_context_suffix  — appended to pack llm_context for industry specificity

SystemDefaultConfig fields:
  role           — SystemRole default for this system in this industry
  priority       — SystemPriority default
  workflow_focus — list of WorkflowFocusTag defaults (max 3)

RoadmapSystemConfig fields (R191-R1 — anchor-on-shipped):
  system_id      — same id space as SystemDefaultConfig keys; MUST NOT also
                   appear in system_defaults or recommended_systems for the
                   same industry (a system is either shipped-and-connectable
                   or roadmap-and-not, never both).
  label          — display label for the roadmap tile/badge.
  target_release — the release expected to ship the ingestor (e.g. "2.0.1").
  reason         — short, honest, user-facing note on why it isn't connectable yet.

Public API:
  get_industry(industry_id) -> Optional[IndustryConfig]
  list_industries() -> list[IndustryConfig]
  get_system_defaults(industry_id, system_id) -> Optional[SystemDefaultConfig]
  get_recommended_systems(industry_id, selected_ids) -> list[str]
  get_pack_hints(industry_id) -> list[str]
  get_roadmap_systems(industry_id) -> list[RoadmapSystemConfig]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# R191-R1: the SAP/D365 demand-gated release target is owned by
# app.connector_roadmap (the catalog's roadmap source of truth). Import it here so
# the Stack Builder registry and the Integration Hub catalog never drift on the
# target string — one edit there moves both surfaces. (app.connector_roadmap is a
# standalone, dependency-light module, so this import introduces no cycle.)
try:
    from app.connector_roadmap import TARGET_2_0_1
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.connector_roadmap import TARGET_2_0_1


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class SystemDefaultConfig:
    """
    Industry-calibrated defaults for a specific system.
    Replaces the generic SYSTEM_DEFAULT_ASSUMPTIONS in useSetupState (Sprint 8).
    """
    role: str            # SystemRole literal
    priority: str        # SystemPriority literal
    workflow_focus: List[str]  # WorkflowFocusTag literals, max 3


@dataclass
class RoadmapSystemConfig:
    """
    A system this industry genuinely wants but that has no shipped ingestor
    yet (R191-R1 "anchor-on-shipped" rule). Rendered as an explicit roadmap
    label in Stack Builder / the Integration Hub catalog — never a connectable
    default, never selectable by a run.
    """
    system_id: str        # same id space as SystemDefaultConfig keys
    label: str             # display label for the roadmap tile/badge
    target_release: str    # e.g. "2.0.1"
    reason: str             # short, honest, user-facing note


@dataclass
class IndustryConfig:
    """
    Full configuration for one industry in the Stack Builder registry.
    """
    industry_id: str
    label: str
    pack_hints: List[str]
    system_defaults: Dict[str, SystemDefaultConfig]   # system_id -> defaults
    recommended_systems: List[str]                     # ordered by priority
    llm_context_suffix: str
    roadmap_systems: List[RoadmapSystemConfig] = field(default_factory=list)


# ── Registry ──────────────────────────────────────────────────────────────────

INDUSTRY_REGISTRY: Dict[str, IndustryConfig] = {

    "financial_services": IndustryConfig(
        industry_id="financial_services",
        label="Financial services",
        # R191-R1 T3: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below.
        pack_hints=["ncino", "service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests", "compliance_risk"]),
            "salesforce_ncino":SystemDefaultConfig("system_of_record",         "primary",   ["approvals", "compliance_risk", "handoffs_routing"]),
            "salesforce_fsc": SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T4: Teams available wherever Slack is offered — mirrors
            # slack's priority/workflow_focus exactly (the established pattern
            # from public_sector/manufacturing/logistics_supply_chain).
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T3: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits core banking / loan
            # servicing / risk data stores underneath the CRM layer.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        },
        recommended_systems=["jira", "servicenow", "confluence", "sqlserver"],
        llm_context_suffix=(
            "Financial services context. Regulatory compliance (FCA, SEC, OCC) "
            "is the highest-weight signal category. Covenant tracking, approval "
            "audit trails, and credit decision documentation are priority friction "
            "patterns. Never suggest automated credit or compliance decisions."
        ),
    ),

    "public_sector": IndustryConfig(
        industry_id="public_sector",
        label="Public sector",
        # R191-R1 T3: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below.
        pack_hints=["service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "salesforce_pss": SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T3: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits the legacy
            # case-management / records databases underneath many agencies'
            # public-facing systems.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        },
        recommended_systems=["slack", "sharepoint", "servicenow", "sqlserver"],
        llm_context_suffix=(
            "Public sector context. Regulatory deadline compliance, fiduciary "
            "obligations, and audit-trail completeness are highest-weight signals. "
            "Member/beneficiary outcomes are the primary measure of friction cost. "
            "Never suggest automated benefit decisions."
        ),
    ),

   "logistics_supply_chain": IndustryConfig(
    industry_id="logistics_supply_chain",
    label="Logistics & supply chain",
    # R191-R1: sqlserver_opsignal added — the shipped native-DB pack fits the
    # operational-signal role sap/dynamics365 used to (incorrectly) anchor.
    pack_hints=["service_cloud", "sqlserver_opsignal"],
    system_defaults={
        "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing", "approvals"]),
        "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing"]),
        "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "handoffs_routing"]),
        "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
        "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
        # R191-R1 re-anchor: databases, documents, and Teams are shipped sources
        # that genuinely fit logistics/supply-chain operations (dispatch/procurement
        # records, carrier and customs paperwork, cross-team handoff chat) — replacing
        # the removed SAP/D365 anchoring below.
        "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        "documents":      SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "handoffs_routing"]),
        "teams":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "handoffs_routing"]),
        "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "handoffs_routing"]),
    },
    recommended_systems=["jira", "sqlserver", "documents"],
    llm_context_suffix=(
        "Logistics and supply chain context. Cross-system handoffs, "
        "throughput bottlenecks, and approval delays in procurement and "
        "dispatch workflows are the primary friction categories."
    ),
    # R191-R1 "anchor-on-shipped": SAP and Dynamics 365 are genuinely relevant
    # to this industry but have no shipped ingestor/pack today. They render as
    # roadmap (target 2.0.1) rather than a connectable default — never
    # selectable by a run — until a real connector ships (CEO decision:
    # SAP/D365 connectors and packs defer to 2.0.1, demand-gated).
    roadmap_systems=[
        RoadmapSystemConfig(
            "sap", "SAP", TARGET_2_0_1,
            "SAP connector and pack are demand-gated for 2.0.1 — not yet connectable.",
        ),
        RoadmapSystemConfig(
            "dynamics365", "Dynamics 365", TARGET_2_0_1,
            "Dynamics 365 connector and pack are demand-gated for 2.0.1 — not yet connectable.",
        ),
    ],
),

    "retail_commerce": IndustryConfig(
        industry_id="retail_commerce",
        label="Retail & commerce",
        # R191-R1 T3: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below.
        pack_hints=["service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_rc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["service_casework", "backlog_work_queues"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications"]),
            # R191-R1 T4: Teams available wherever Slack is offered — mirrors
            # slack's priority/workflow_focus exactly.
            "teams":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "optional",  ["documents_knowledge"]),
            # R191-R1 T3: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits POS / inventory /
            # order-management databases underneath the storefront layer.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        },
        recommended_systems=["jira", "slack", "confluence", "sqlserver"],
        llm_context_suffix=(
            "Retail and commerce context. Customer service case routing, "
            "returns processing, and order-to-fulfilment handoffs are "
            "primary friction patterns."
        ),
    ),

    "healthcare": IndustryConfig(
        industry_id="healthcare",
        label="Healthcare",
        # R191-R1 T3: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below.
        pack_hints=["service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_hc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "compliance_risk"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "optional",  ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T4: Teams available wherever Slack is offered — mirrors
            # slack's priority/workflow_focus exactly.
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T3: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits clinical/ancillary
            # data stores underneath the EHR layer (lab, scheduling, billing).
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "compliance_risk"]),
        },
        recommended_systems=["servicenow", "sharepoint", "slack", "sqlserver"],
        llm_context_suffix=(
            "Healthcare context. HIPAA compliance, clinical workflow approvals, "
            "and patient-facing service SLAs are highest-weight signal categories. "
            "Never suggest automated clinical or treatment decisions."
        ),
    ),

    "energy_utilities": IndustryConfig(
        industry_id="energy_utilities",
        label="Energy & utilities",
        # R191-R1 T3: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below. R191-R1 T6 now enforces the
        # full anchor-on-shipped rule, so SAP moves to roadmap until its real
        # ingestor ships.
        pack_hints=["service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "compliance_risk"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T4: Teams available wherever Slack is offered — mirrors
            # slack's priority/workflow_focus exactly.
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            # R191-R1 T3: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits asset-management /
            # billing / SCADA-adjacent data stores underneath the ERP layer.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        },
        recommended_systems=["servicenow", "sharepoint", "jira", "sqlserver"],
        llm_context_suffix=(
            "Energy and utilities context. Regulatory compliance, asset "
            "maintenance approval workflows, and field-to-office handoff "
            "friction are primary signal categories."
        ),
        roadmap_systems=[
            RoadmapSystemConfig(
                "sap", "SAP", TARGET_2_0_1,
                "SAP connector and pack are demand-gated for 2.0.1 - not yet connectable.",
            ),
        ],
    ),

    "manufacturing": IndustryConfig(
        industry_id="manufacturing",
        label="Manufacturing",
        # R191-R1: sqlserver_opsignal added — the shipped native-DB pack fits the
        # operational-signal role sap/dynamics365 used to (incorrectly) anchor.
        pack_hints=["service_cloud", "sqlserver_opsignal"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            # R191-R1 re-anchor: databases, documents, and Teams are shipped sources
            # that genuinely fit manufacturing operations (MES/plant-floor database
            # signal, work-order/quality documentation, cross-shift handoff chat) —
            # replacing the removed SAP/D365 anchoring below.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
            "documents":      SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "compliance_risk"]),
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["servicenow", "sqlserver", "documents"],
        llm_context_suffix=(
            "Manufacturing context. Work order approval friction, maintenance "
            "scheduling bottlenecks, and cross-system handoffs between ERP "
            "and operations systems are primary friction patterns."
        ),
        # R191-R1 "anchor-on-shipped": SAP and Dynamics 365 are genuinely relevant
        # to this industry but have no shipped ingestor/pack today. They render as
        # roadmap (target 2.0.1) rather than a connectable default — never
        # selectable by a run — until a real connector ships (CEO decision:
        # SAP/D365 connectors and packs defer to 2.0.1, demand-gated).
        roadmap_systems=[
            RoadmapSystemConfig(
                "sap", "SAP", TARGET_2_0_1,
                "SAP connector and pack are demand-gated for 2.0.1 — not yet connectable.",
            ),
            RoadmapSystemConfig(
                "dynamics365", "Dynamics 365", TARGET_2_0_1,
                "Dynamics 365 connector and pack are demand-gated for 2.0.1 — not yet connectable.",
            ),
        ],
    ),

    "technology": IndustryConfig(
        industry_id="technology",
        label="Technology",
        # R191-R1 T2: sqlserver_opsignal added — the shipped native-DB pack
        # serves the new database anchor below. T4: github_engineering added —
        # technology already anchors github as a connectable default (T2); the
        # pack that actually scores its PR/commit/branch signal was missing
        # from the hint list, leaving this industry an honest-pack-list gap
        # (the story's own example for this sub-goal). The 1.9 cloud-ops/
        # sec-ops packs the story also names are NOT present in this codebase
        # (no such pack_id exists in pack_config.PACK_REGISTRY) — anchor-on-
        # shipped means they are deliberately omitted, not guessed at.
        pack_hints=["service_cloud", "sqlserver_opsignal", "github_engineering"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_rc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals"]),
            # R191-R1 T4: reduce Salesforce-centric anchoring — technology's own
            # llm_context_suffix names engineering delivery friction, backlog
            # bottlenecks, and deployment approval delays as the PRIMARY signal
            # category for this industry, not case/service work. Jira (shipped)
            # is the honest co-primary system for that signal, not a secondary
            # one — elevated from operational_signal_source/secondary to
            # workflow_system/primary. Salesforce variants stay primary too
            # (a tech company's customer-facing side is real); this is additive
            # primary-anchor diversification, not a Salesforce removal.
            "jira":           SystemDefaultConfig("workflow_system",           "primary",   ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
            # R191-R1 T2: GitHub has a shipped connector/pack (connectors/saas/
            # github.py + github_engineering) and stays a connectable default —
            # optional priority, unchanged. GitLab has no shipped ingestor and
            # moves to roadmap_systems below (was incorrectly anchored here).
            "github":         SystemDefaultConfig("engineering_change_system", "optional",  ["change_release", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "change_release"]),
            # R191-R1 T4: Teams available wherever Slack is offered — mirrors
            # slack's priority/workflow_focus exactly.
            "teams":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "change_release"]),
            # R191-R1 T2: databases — a shipped source (native DB connector,
            # sqlserver_opsignal pack) that genuinely fits a technology company's
            # internal operational/ticketing database signal.
            "sqlserver":      SystemDefaultConfig("operational_signal_source", "secondary", ["data_analytics", "backlog_work_queues"]),
        },
        recommended_systems=["jira", "slack", "confluence", "sqlserver"],
        llm_context_suffix=(
            "Technology company context. Engineering delivery friction, "
            "backlog bottlenecks, deployment approval delays, and cross-team "
            "handoff failures are primary signal categories."
        ),
        # R191-R1 "anchor-on-shipped": GitLab is genuinely relevant to a
        # technology company but has no shipped ingestor today. It renders as
        # roadmap rather than a connectable default — never selectable by a
        # run. Unlike SAP/D365 (explicitly committed to 2.0.1), GitLab carries
        # no committed release target in the story, so it is marked
        # "unscheduled" rather than a fabricated version.
        roadmap_systems=[
            RoadmapSystemConfig(
                "gitlab", "GitLab", "unscheduled",
                "GitLab has no shipped ingestor — GitHub is the connectable "
                "engineering-change source today; GitLab is not yet connectable.",
            ),
        ],
    ),
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_industry(industry_id: str) -> Optional[IndustryConfig]:
    """Return the IndustryConfig for industry_id, or None if not found."""
    return INDUSTRY_REGISTRY.get(industry_id)


def list_industries() -> List[IndustryConfig]:
    """Return all registered industries in display order."""
    return list(INDUSTRY_REGISTRY.values())


def get_system_defaults(
    industry_id: str,
    system_id: str,
) -> Optional[SystemDefaultConfig]:
    """
    Return industry-calibrated defaults for a specific system.
    Returns None if the industry or system is not in the registry.

    Sprint 8 usage:
        In useSetupState, replace SYSTEM_DEFAULT_ASSUMPTIONS[system_id]
        with get_system_defaults(state.industryId, system_id) when
        industryId is set, falling back to SYSTEM_DEFAULT_ASSUMPTIONS
        when it is not.
    """
    config = INDUSTRY_REGISTRY.get(industry_id)
    if not config:
        return None
    return config.system_defaults.get(system_id)


def get_recommended_systems(
    industry_id: str,
    selected_ids: List[str],
) -> List[str]:
    """
    Return ordered list of recommended system IDs for this industry
    that are not already in selected_ids. Max 3 returned.

    Used by DiscoveryPlanScreen (Screen 4) recommended additions logic.
    Sprint 8: replace local calcRecommendedAdditions with this function
    called via /api/stack-builder/recommendations endpoint.
    """
    config = INDUSTRY_REGISTRY.get(industry_id)
    if not config:
        return []
    selected_set = set(selected_ids)
    recs = [s for s in config.recommended_systems if s not in selected_set]
    return recs[:3]


def get_pack_hints(industry_id: str) -> List[str]:
    """
    Return pack IDs most relevant for this industry.
    Used by ENG-PACK-SELECTOR (Sprint 5.1) to suggest pack ordering.
    """
    config = INDUSTRY_REGISTRY.get(industry_id)
    if not config:
        return []
    return config.pack_hints


def get_llm_context_suffix(industry_id: str) -> str:
    """
    Return the industry-specific LLM context suffix.
    Appended to pack llm_context in enrichment prompts when industryId is set.
    Returns empty string if industry not found.
    """
    config = INDUSTRY_REGISTRY.get(industry_id)
    if not config:
        return ""
    return config.llm_context_suffix


def get_roadmap_systems(industry_id: str) -> List[RoadmapSystemConfig]:
    """
    Return the non-connectable roadmap systems for this industry (R191-R1).

    These are systems the industry genuinely wants but that have no shipped
    ingestor yet (e.g. SAP/D365, target 2.0.1). Callers must render them as an
    explicit roadmap label — never as a connectable default, never as a run
    selection. Returns [] if the industry is not found or declares none.
    """
    config = INDUSTRY_REGISTRY.get(industry_id)
    if not config:
        return []
    return config.roadmap_systems
