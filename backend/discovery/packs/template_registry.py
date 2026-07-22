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
    # Detector IDs this template emphasises — provenance/documentation of what
    # the template's pack surfaces. The REAL scoring is already wired: pack_id
    # activates the pack's detectors + scorer, and focus_defaults.focus_id drives
    # focus_affinity ranking. This field records that emphasis for the run and UI;
    # it does not itself change scoring. Empty = "whatever the pack emphasises".
    detector_emphasis: List[str] = field(default_factory=list)
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
            "of record, workflow, communication, and documentation sources for corroboration, "
            "the lending pack, and approvals & compliance focus."
        ),
        suggested_systems=[
            "salesforce_ncino",
            "jira",
            "servicenow",
            "slack",
            "teams",
            "confluence",
        ],
        suggested_roles={
            "salesforce_ncino": "system_of_record",
            "jira": "workflow_system",
            "servicenow": "workflow_system",
            "slack": "operational_signal_source",
            "teams": "operational_signal_source",
            "confluence": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="approvals_compliance",
            emphasis=["approvals", "compliance_risk", "backlog_work_queues"],
        ),
        # nCino Lending pack (pack_config.PACK_REGISTRY["ncino"]).
        pack_id="ncino",
        # The lending detector set emphasised by this template — the exact keys
        # scored by discovery/lending_scorer._LENDING_SCORES (covenant tracking
        # gaps, approval bottlenecks, exception/checklist queues, spreading and
        # loan-origination friction). Launching the untouched template with
        # pack_id="ncino" + focus_id="approvals_compliance" applies this emphasis
        # through the already-wired lending scorer and focus-affinity ranking.
        detector_emphasis=[
            "COVENANT_TRACKING_GAP",
            "APPROVAL_BOTTLENECK",
            "CHECKLIST_BOTTLENECK",
            "SPREADING_BOTTLENECK",
            "LOAN_ORIGINATION_ROUTING_FRICTION",
        ],
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

    # MSP-B6 T5 (AT-740): the Managed Cloud Operations template — the SECOND
    # production instance of the R18-C1 model, proving a new template is
    # configuration only (a dict entry here + the already-served registry route),
    # a domain away from lending. It activates the Cloud-Operations (NOC) pack and
    # the core-operations focus, both already wired: pack_config.PACK_REGISTRY
    # ["cloud_ops"] supplies the detectors + T4 ops-impact scorer, and
    # focus_affinity.FOCUS_CORE_OPERATIONS already emphasises this pack's
    # detectors. Every field below is an EDITABLE default (resolve_launch_config).
    "managed_cloud_operations": TemplateDefinition(
        template_id="managed_cloud_operations",
        label="Managed Cloud Operations",
        description=(
            "Managed cloud-operations (NOC) starting point: ServiceNow as the "
            "system of record, AWS/Azure event sources for operational signal, "
            "and a runbook library for supporting context — the Cloud-Operations "
            "pack with a core-operations focus. Speaks NOC: alerts, incidents, "
            "runbooks, MTTR, toil, escalation."
        ),
        # ServiceNow (system of record) + AWS/Azure event sources (operational
        # signal) + runbook library (supporting/documentation), per scope §2.
        suggested_systems=[
            "servicenow",
            "aws_event_source",
            "azure_event_source",
            "runbook_library",
        ],
        suggested_roles={
            "servicenow": "system_of_record",
            "aws_event_source": "operational_signal_source",
            "azure_event_source": "operational_signal_source",
            # The runbook library is the supporting documentation source (same
            # lane as Confluence in the lending template).
            "runbook_library": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            # core_operations already emphasises this pack's detectors
            # (focus_affinity.FOCUS_CORE_OPERATIONS) — no code change required.
            focus_id="core_operations",
            emphasis=["backlog_work_queues", "handoffs_routing", "communications"],
        ),
        # Cloud-Operations pack (pack_config.PACK_REGISTRY["cloud_ops"]).
        pack_id="cloud_ops",
        # The five Cloud-Operations detectors this template surfaces (MSP-B6
        # T2 record/stream detectors + the T3 shared-CI hotspot). Launching the
        # untouched template applies this emphasis through the already-wired
        # cloud_ops scorer (T4) and core-operations focus-affinity ranking.
        detector_emphasis=[
            "RECURRING_RESOLUTION_LOOP",
            "ALERT_TRIAGE_TOIL",
            "REASSIGNMENT_PING_PONG",
            "QUEUE_AGEING",
            "SHARED_CI_HOTSPOT",
        ],
        # NOC vocabulary — mirrors the pack's language_map in
        # cloud_ops_pack_config.json so template + pack speak the same language.
        terminology={
            "opportunity": "finding",
            "ticket": "incident",
            "notification": "alert",
            "documentation": "runbook",
            "resolution_time": "MTTR",
            "friction": "toil",
            "handoff": "escalation",
        },
        metadata={
            "industry_id": "technology",
            "lane": "managed_services",
            "source": "MSP-B6",
            "version": "1.0.0",
            "evidence_contract": "four_part_observed_finding",
            "compatible_templates": ["security_operations"],
        },
    ),

    # MSP-B12 T5: Security Operations is a registry-only instance of the same
    # generic TemplateDefinition used by every Stack Builder template. All
    # values are editable defaults; no customer configuration is locked here.
    "security_operations": TemplateDefinition(
        template_id="security_operations",
        label="Security Operations",
        description=(
            "Security operations starting point: ServiceNow ITSM and Security "
            "Operations as the system of record, AWS and Azure operational events "
            "as supporting signals, and the runbook or playbook library as the "
            "supporting documentation source. Activates the Security Operations "
            "pack for recurring remediation work, routing loops, ageing, shared "
            "infrastructure concentration, and incident-response triage effort."
        ),
        suggested_systems=[
            "servicenow",
            "aws_event_source",
            "azure_event_source",
            "runbook_library",
        ],
        suggested_roles={
            "servicenow": "system_of_record",
            "aws_event_source": "operational_signal_source",
            "azure_event_source": "operational_signal_source",
            "runbook_library": "documentation_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="core_operations",
            emphasis=[
                "backlog_work_queues",
                "handoffs_routing",
                "compliance_risk",
            ],
        ),
        pack_id="security_ops",
        detector_emphasis=[
            "SECOPS_REMEDIATION_RECURRENCE",
            "SECOPS_SECURITY_IT_PING_PONG",
            "SECOPS_SLA_DEFERRAL_AGEING",
            "SECOPS_SHARED_INFRA_CONCENTRATION",
            "SECOPS_SIR_TRIAGE_TOIL",
        ],
        terminology={
            "opportunity": "finding",
            "friction": "toil",
            "ticket": "remediation task",
            "resolution_time": "time-in-state",
            "handoff": "reassignment",
            "queue": "security queue",
            "sla_breach": "SLA ageing",
            "documentation": "playbook",
        },
        metadata={
            "industry_id": "technology",
            "lane": "managed_security",
            "source": "MSP-B12",
            "version": "1.0.0",
            "servicenow_capabilities": ["ITSM", "Security Operations"],
            "evidence_contract": "four_part_observed_finding",
            "compatible_templates": ["managed_cloud_operations"],
        },
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


# ── Launch resolution + provenance (R18-C1 T2) ────────────────────────────────

def template_defaults_snapshot(defn: TemplateDefinition) -> Dict[str, Any]:
    """A plain-dict snapshot of the defaults a template contributes to a launch."""
    from discovery.packs.pack_config import get_pack_version

    return {
        "template_id": defn.template_id,
        "template_version": str(defn.metadata.get("version") or "1.0.0"),
        "label": defn.label,
        "description": defn.description,
        "pack_id": defn.pack_id,
        "pack_version": get_pack_version(defn.pack_id),
        "focus_id": defn.focus_defaults.focus_id,
        "focus_emphasis": list(defn.focus_defaults.emphasis),
        "suggested_systems": list(defn.suggested_systems),
        "suggested_roles": dict(defn.suggested_roles),
        "detector_emphasis": list(defn.detector_emphasis),
        "terminology": dict(defn.terminology),
        "metadata": dict(defn.metadata),
    }


def normalize_template_ids(template_ids: Optional[List[str]]) -> List[str]:
    """Return an order-preserving, de-duplicated template selection.

    Composition lives here, outside ``TemplateDefinition``. This keeps the
    generic model unchanged while allowing one run to select multiple registry
    entries.
    """
    normalized: List[str] = []
    seen = set()
    for raw in template_ids or []:
        template_id = str(raw or "").strip()
        if template_id and template_id not in seen:
            normalized.append(template_id)
            seen.add(template_id)
    return normalized


def _stable_union(groups: List[List[str]]) -> List[str]:
    values: List[str] = []
    seen = set()
    for group in groups:
        for raw in group:
            value = str(raw or "").strip()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
    return values


def resolve_launch_config(
    template_id: Optional[str],
    *,
    template_ids: Optional[List[str]] = None,
    pack_id: Optional[str] = None,
    pack_ids: Optional[List[str]] = None,
    focus_id: Optional[str] = None,
    selected_system_ids: Optional[List[str]] = None,
    weightings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve the effective launch configuration for a template-driven run and the
    provenance of how it was assembled (R18-C1 T2, AC1/AC2/AC5).

    Every value a template sets is an EDITABLE default:
      * When the caller leaves a field empty, the template default fills it —
        so a run launched from the UNTOUCHED template applies the lending pack
        and focus (AC2). This is why an "untouched" launch works even before the
        frontend is wired to send every field (T3).
      * When the caller submits a value, THAT value wins (edits are preserved,
        AC1) and the field is recorded as a user edit (AC5).

    Returns a dict:
      {
        "effective": {pack_id, focus_id, selected_system_ids, roles},
        "provenance": {
          "template_id", "applied" (bool),
          "template_defaults" (snapshot),
          "edited_fields" (list[str]),      # which fields the user changed
          "untouched" (bool),               # no edits vs the template
        }
      }

    When template_id is None/unknown, `effective` echoes the submitted values and
    provenance records that no template was applied (fully backward compatible).
    """
    from discovery.packs.pack_config import normalize_pack_ids

    submitted_systems = list(selected_system_ids or [])
    submitted_weightings = dict(weightings or {})
    selected_template_ids = normalize_template_ids(
        list(template_ids or []) + ([template_id] if template_id else [])
    )
    definitions = [
        defn
        for selected_id in selected_template_ids
        if (defn := get_template(selected_id)) is not None
    ]
    submitted_pack_ids = normalize_pack_ids(
        list(pack_ids or []) + ([pack_id] if pack_id else [])
    )

    if not definitions:
        eff_pack_ids = submitted_pack_ids
        return {
            "effective": {
                "pack_id": eff_pack_ids[0] if eff_pack_ids else pack_id,
                "pack_ids": eff_pack_ids,
                "template_ids": selected_template_ids,
                "focus_id": focus_id,
                "selected_system_ids": submitted_systems,
                "roles": _roles_from_weightings(submitted_weightings),
                "pack_boundaries": [],
            },
            "provenance": {
                "template_id": template_id,
                "template_ids": selected_template_ids,
                "applied": False,
                "template_defaults": None,
                "template_defaults_list": [],
                "pack_boundaries": [],
                "edited_fields": [],
                "untouched": False,
            },
        }

    snapshots = [template_defaults_snapshot(defn) for defn in definitions]
    primary = definitions[0]
    default_pack_ids = normalize_pack_ids([defn.pack_id for defn in definitions])
    default_systems = _stable_union(
        [list(defn.suggested_systems) for defn in definitions]
    )
    default_roles: Dict[str, str] = {}
    for defn in definitions:
        for system_id, role in defn.suggested_roles.items():
            # The first selected template is primary when defaults ever conflict.
            default_roles.setdefault(system_id, role)

    edited_fields: List[str] = []

    # Explicit pack selections replace the composed defaults. With no explicit
    # pack selection, every selected template contributes its pack.
    eff_pack_ids = submitted_pack_ids or default_pack_ids
    if submitted_pack_ids and submitted_pack_ids != default_pack_ids:
        edited_fields.append("pack_id")
    eff_pack = eff_pack_ids[0] if eff_pack_ids else None

    # A run has one workflow focus. The first selected template supplies it;
    # target combined templates both use core_operations. Separate template
    # snapshots retain every template's own focus defaults for traceability.
    eff_focus = focus_id or primary.focus_defaults.focus_id
    if focus_id and focus_id != primary.focus_defaults.focus_id:
        edited_fields.append("focus_id")

    if submitted_systems:
        eff_systems = submitted_systems
        if set(submitted_systems) != set(default_systems):
            edited_fields.append("selected_system_ids")
    else:
        eff_systems = default_systems

    submitted_roles = _roles_from_weightings(submitted_weightings)
    eff_roles = dict(default_roles)
    roles_edited = False
    for system_id, role in submitted_roles.items():
        if eff_roles.get(system_id) != role:
            roles_edited = True
        eff_roles[system_id] = role
    # Effective configuration contains roles only for sources that will actually
    # be used. A removed template default must not survive as a stale role.
    selected_system_set = set(eff_systems)
    eff_roles = {
        system_id: role
        for system_id, role in eff_roles.items()
        if system_id in selected_system_set
    }
    if roles_edited:
        edited_fields.append("roles")

    active_pack_set = set(eff_pack_ids)
    pack_boundaries = [
        {
            "template_id": snapshot["template_id"],
            "template_version": snapshot["template_version"],
            "pack_id": snapshot["pack_id"],
            "pack_version": snapshot["pack_version"],
            "focus_id": snapshot["focus_id"],
            "focus_emphasis": list(snapshot["focus_emphasis"]),
            "detector_emphasis": list(snapshot["detector_emphasis"]),
            "terminology": dict(snapshot["terminology"]),
            "evidence_contract": snapshot["metadata"].get("evidence_contract"),
        }
        for snapshot in snapshots
        if snapshot["pack_id"] in active_pack_set
    ]

    effective = {
        "pack_id": eff_pack,
        "pack_ids": eff_pack_ids,
        "template_ids": [defn.template_id for defn in definitions],
        "focus_id": eff_focus,
        "selected_system_ids": eff_systems,
        "roles": eff_roles,
        "pack_boundaries": pack_boundaries,
    }
    return {
        "effective": effective,
        "provenance": {
            "template_id": primary.template_id,
            "template_ids": [defn.template_id for defn in definitions],
            "applied": True,
            # Singular alias retained for every R18-C1 consumer.
            "template_defaults": snapshots[0],
            "template_defaults_list": snapshots,
            "pack_boundaries": pack_boundaries,
            "edited_fields": edited_fields,
            "untouched": not edited_fields,
            "effective_configuration": effective,
        },
    }


def _roles_from_weightings(weightings: Dict[str, Any]) -> Dict[str, str]:
    """Extract system_id -> role from a SystemWeighting map (role key optional)."""
    roles: Dict[str, str] = {}
    for system_id, w in (weightings or {}).items():
        if isinstance(w, dict) and w.get("role"):
            roles[system_id] = w["role"]
    return roles
