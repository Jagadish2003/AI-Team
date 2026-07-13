"""
template_registry.py — R18-C1 T1: Stack Builder Template Definition Model

A template is a generic, named bundle of *editable defaults* for the Guided
Discovery Stack Builder — never a fork of the discovery engine (R18-C1 scope
principle). Selecting a template pre-populates the setup experience (systems,
roles, focus, pack, terminology) with sensible starting choices the user can
still change before launch.

This module makes a template a reusable BACKEND configuration object instead of
frontend-only UI text. The frontend currently owns hardcoded `TEMPLATES` arrays
(DiscoveryFocusPage.tsx); R18-C1 T3 will replace those by fetching this registry
through `GET /api/stack-builder/templates`. This T1 delivers the generic model +
listing so the backend is the source of truth.

Design mirrors industry_registry.py exactly (dataclass config + a module-level
`*_REGISTRY` dict + `get_*`/`list_*` accessors) so the two read the same way.

Genericness (R18-C1 AC4 / AC8): the model is industry-agnostic. Commercial
Lending is the first PRODUCTION instance, but a second template — Insurance,
Healthcare, or a test fixture — can be added by a single dict entry with NO code
change. `register_template()` / `unregister_template()` exist so a test (or a
future business-team config loader) can prove that by adding a template purely as
configuration. See tests/contract/test_stack_builder_templates.py.

TemplateDefinition fields:
  template_id        — stable ID; the value stored on the run record
                       (LaunchRequest.template_id) and used by the frontend.
  label              — display name.
  description         — one-line explanation shown in the template picker.
  suggested_systems  — system IDs pre-selected when the template is chosen
                       (matches the frontend `preselectedSystems`). Editable.
  suggested_roles    — system_id -> default SystemRole, feeding the R16-C1
                       weighting model. Editable before launch.
  focus_defaults     — default WorkflowFocus emphasis (focus_id + optional
                       emphasised focus tags). Editable.
  pack_id            — the discovery pack this template activates; validated
                       against pack_config on registration.
  terminology        — domain language (borrowers, facilities, covenants, …)
                       surfaced across findings/roadmap/report when the template
                       is active (consumed by later tasks; net-new here).
  metadata           — free-form provenance/config (industry_id, source, version).

Public API:
  get_template(template_id) -> Optional[TemplateDefinition]
  list_templates() -> list[TemplateDefinition]
  register_template(defn) -> None          # config-only extension point
  unregister_template(template_id) -> None
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FocusDefaults:
    """Default workflow-focus emphasis a template applies. All editable."""
    focus_id: str                       # FocusId literal (frontend `suggestedFocus`)
    emphasis: List[str] = field(default_factory=list)  # WorkflowFocusTag emphasis


@dataclass
class TemplateDefinition:
    """
    A generic, named bundle of editable Stack Builder defaults.

    Industry-agnostic by construction: nothing here is lending-specific except
    the VALUES of a given instance. Adding a template is adding a dict entry.
    """
    template_id: str
    label: str
    description: str
    suggested_systems: List[str]
    suggested_roles: Dict[str, str]          # system_id -> SystemRole
    focus_defaults: FocusDefaults
    pack_id: str
    terminology: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Registry ──────────────────────────────────────────────────────────────────
# Keyed by template_id, insertion order = display order. The three production
# templates mirror the frontend TEMPLATES array (DiscoveryFocusPage.tsx) so T3
# can switch the UI to this backend source with no behaviour change.

TEMPLATE_REGISTRY: Dict[str, TemplateDefinition] = {

    "commercial_lending": TemplateDefinition(
        template_id="commercial_lending",
        label="Commercial lending",
        description=(
            "Commercial lending starting point: nCino/Salesforce as the system "
            "of record, workflow and documentation sources for corroboration, "
            "the lending pack, and approvals & compliance focus."
        ),
        suggested_systems=["salesforce_ncino", "jira", "servicenow", "confluence"],
        suggested_roles={
            "salesforce_ncino": "system_of_record",
            "jira": "workflow_system",
            "servicenow": "workflow_system",
            "confluence": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="approvals_compliance",
            emphasis=["approvals", "compliance_risk"],
        ),
        # nCino Lending pack (pack_config.PACK_REGISTRY["ncino"]).
        pack_id="ncino",
        terminology={
            "customer": "borrower",
            "account": "facility",
            "obligation": "covenant",
            "rationale": "credit memo",
            "approval": "approval gate",
        },
        metadata={
            "industry_id": "financial_services",
            "source": "R18-C1",
            "version": "1.0.0",
        },
    ),

    "service_operations": TemplateDefinition(
        template_id="service_operations",
        label="Service operations",
        description=(
            "Service operations starting point: Service Cloud as the system of "
            "record with workflow and documentation corroboration, focused on "
            "member/customer service casework."
        ),
        suggested_systems=["salesforce_sc", "servicenow", "confluence"],
        suggested_roles={
            "salesforce_sc": "system_of_record",
            "servicenow": "workflow_system",
            "confluence": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="member_customer_service",
            emphasis=["service_casework", "intake_requests"],
        ),
        pack_id="service_cloud",
        terminology={},
        metadata={"source": "R18-C1", "version": "1.0.0"},
    ),

    "revenue_operations": TemplateDefinition(
        template_id="revenue_operations",
        label="Revenue operations",
        description=(
            "Revenue operations starting point: Revenue Cloud as the system of "
            "record with workflow and documentation corroboration, focused on "
            "core operations and intake."
        ),
        suggested_systems=["salesforce_rc", "jira", "confluence"],
        suggested_roles={
            "salesforce_rc": "system_of_record",
            "jira": "workflow_system",
            "confluence": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="core_operations",
            emphasis=["intake_requests", "approvals"],
        ),
        pack_id="service_cloud",
        terminology={},
        metadata={"source": "R18-C1", "version": "1.0.0"},
    ),
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_template(template_id: str) -> Optional[TemplateDefinition]:
    """Return the TemplateDefinition for template_id, or None if not found."""
    return TEMPLATE_REGISTRY.get(template_id)


def list_templates() -> List[TemplateDefinition]:
    """Return all registered templates in display (insertion) order."""
    return list(TEMPLATE_REGISTRY.values())


def register_template(defn: TemplateDefinition, *, validate_pack: bool = True) -> None:
    """
    Add (or replace) a template by configuration only — the genericness hook
    (AC4/AC8). Adding an Insurance/Healthcare template later, or a test-fixture
    template, goes through here (or a literal dict entry) with no route/model
    change.

    Validates the referenced pack against pack_config so a template cannot point
    at a pack that does not exist. Import is local to avoid a heavy import at
    module load and any circular-import risk.
    """
    if validate_pack:
        from discovery.packs.pack_config import list_packs

        if defn.pack_id not in list_packs():
            raise ValueError(
                f"Template '{defn.template_id}' references unknown pack "
                f"'{defn.pack_id}'. Known packs: {sorted(list_packs())}"
            )
    TEMPLATE_REGISTRY[defn.template_id] = defn


def unregister_template(template_id: str) -> None:
    """Remove a template if present (idempotent). Used by test teardown."""
    TEMPLATE_REGISTRY.pop(template_id, None)
