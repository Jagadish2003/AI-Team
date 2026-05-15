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
  recommended_systems — system IDs to suggest as additions on Screen 4
                        when not already selected. Ordered by priority.
  llm_context_suffix  — appended to pack llm_context for industry specificity

SystemDefaultConfig fields:
  role           — SystemRole default for this system in this industry
  priority       — SystemPriority default
  workflow_focus — list of WorkflowFocusTag defaults (max 3)

Public API:
  get_industry(industry_id) -> Optional[IndustryConfig]
  list_industries() -> list[IndustryConfig]
  get_system_defaults(industry_id, system_id) -> Optional[SystemDefaultConfig]
  get_recommended_systems(industry_id, selected_ids) -> list[str]
  get_pack_hints(industry_id) -> list[str]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


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


# ── Registry ──────────────────────────────────────────────────────────────────

INDUSTRY_REGISTRY: Dict[str, IndustryConfig] = {

    "financial_services": IndustryConfig(
        industry_id="financial_services",
        label="Financial services",
        pack_hints=["ncino", "service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "salesforce_pss": SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests", "compliance_risk"]),
            "salesforce_ncino":SystemDefaultConfig("system_of_record",         "primary",   ["approvals", "compliance_risk", "handoffs_routing"]),
            "salesforce_fsc": SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["jira", "servicenow", "confluence"],
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
        pack_hints=["strs_benefits", "service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "salesforce_pss": SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests", "compliance_risk"]),
            "neospin":        SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "vitech":         SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
            "teams":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["slack", "sharepoint", "servicenow"],
        llm_context_suffix=(
            "Public sector context. Regulatory deadline compliance, fiduciary "
            "obligations, and audit-trail completeness are highest-weight signals. "
            "Member/beneficiary outcomes are the primary measure of friction cost. "
            "Reference applicable state statutes (e.g. Ohio Revised Code 3307 for "
            "STRS) where relevant. Never suggest automated benefit decisions."
        ),
    ),

   "logistics_supply_chain": IndustryConfig(
    industry_id="logistics_supply_chain",
    label="Logistics & supply chain",
    pack_hints=["service_cloud"],
    system_defaults={
        "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing", "approvals"]),
        "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing"]),
        "sap":            SystemDefaultConfig("system_of_record",          "primary",   ["handoffs_routing", "approvals", "compliance_risk"]),
        "dynamics365":    SystemDefaultConfig("system_of_record",          "primary",   ["handoffs_routing", "approvals"]),
        "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "handoffs_routing"]),
        "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
        "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
        "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "handoffs_routing"]),
    },
    recommended_systems=["jira", "slack", "confluence"],
    llm_context_suffix=(
        "Logistics and supply chain context. Cross-system handoffs, "
        "throughput bottlenecks, and approval delays in procurement and "
        "dispatch workflows are the primary friction categories."
    ),
),

    "retail_commerce": IndustryConfig(
        industry_id="retail_commerce",
        label="Retail & commerce",
        pack_hints=["service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_rc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["service_casework", "backlog_work_queues"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "optional",  ["documents_knowledge"]),
        },
        recommended_systems=["jira", "slack", "confluence"],
        llm_context_suffix=(
            "Retail and commerce context. Customer service case routing, "
            "returns processing, and order-to-fulfilment handoffs are "
            "primary friction patterns."
        ),
    ),

    "healthcare": IndustryConfig(
        industry_id="healthcare",
        label="Healthcare",
        pack_hints=["service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_hc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "compliance_risk"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "optional",  ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["servicenow", "sharepoint", "slack"],
        llm_context_suffix=(
            "Healthcare context. HIPAA compliance, clinical workflow approvals, "
            "and patient-facing service SLAs are highest-weight signal categories. "
            "Never suggest automated clinical or treatment decisions."
        ),
    ),

    "energy_utilities": IndustryConfig(
        industry_id="energy_utilities",
        label="Energy & utilities",
        pack_hints=["service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "compliance_risk"]),
            "sap":            SystemDefaultConfig("system_of_record",          "primary",   ["approvals", "compliance_risk", "handoffs_routing"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["compliance_risk", "backlog_work_queues"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "sharepoint":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge", "compliance_risk"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["servicenow", "sharepoint", "jira"],
        llm_context_suffix=(
            "Energy and utilities context. Regulatory compliance, asset "
            "maintenance approval workflows, and field-to-office handoff "
            "friction are primary signal categories."
        ),
    ),

    "manufacturing": IndustryConfig(
        industry_id="manufacturing",
        label="Manufacturing",
        pack_hints=["service_cloud"],
        system_defaults={
            "sap":            SystemDefaultConfig("system_of_record",          "primary",   ["approvals", "handoffs_routing", "compliance_risk"]),
            "dynamics365":    SystemDefaultConfig("system_of_record",          "primary",   ["approvals", "handoffs_routing"]),
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "approvals"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "handoffs_routing"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "optional",  ["communications"]),
        },
        recommended_systems=["servicenow", "jira", "confluence"],
        llm_context_suffix=(
            "Manufacturing context. Work order approval friction, maintenance "
            "scheduling bottlenecks, and cross-system handoffs between ERP "
            "and operations systems are primary friction patterns."
        ),
    ),

    "technology": IndustryConfig(
        industry_id="technology",
        label="Technology",
        pack_hints=["service_cloud"],
        system_defaults={
            "salesforce":     SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_sc":  SystemDefaultConfig("system_of_record",          "primary",   ["service_casework", "intake_requests"]),
            "salesforce_rc":  SystemDefaultConfig("system_of_record",          "primary",   ["intake_requests", "approvals"]),
            "jira":           SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "change_release"]),
            "servicenow":     SystemDefaultConfig("operational_signal_source", "secondary", ["backlog_work_queues", "compliance_risk"]),
            "github":         SystemDefaultConfig("engineering_change_system", "optional",  ["change_release", "backlog_work_queues"]),
            "gitlab":         SystemDefaultConfig("engineering_change_system", "optional",  ["change_release", "backlog_work_queues"]),
            "confluence":     SystemDefaultConfig("documentation_system",      "secondary", ["documents_knowledge"]),
            "slack":          SystemDefaultConfig("operational_signal_source", "secondary", ["communications", "change_release"]),
        },
        recommended_systems=["jira", "slack", "confluence"],
        llm_context_suffix=(
            "Technology company context. Engineering delivery friction, "
            "backlog bottlenecks, deployment approval delays, and cross-team "
            "handoff failures are primary signal categories."
        ),
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
