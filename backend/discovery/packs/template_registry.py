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
    # R191-P1 T5: a template may activate MORE THAN ONE pack. `packs` is the full
    # ordered, de-duplicated pack selection; `pack_id` stays the PRIMARY (first)
    # pack for backward compatibility. Declaring only `pack_id` makes
    # packs == [pack_id]; declaring `packs` re-derives pack_id as its first entry
    # (see __post_init__). A multi-pack template (e.g. combined operations) runs
    # every listed pack on run creation — honored end to end via
    # resolve_launch_config → the launch endpoint's pack_ids.
    packs: List[str] = field(default_factory=list)
    # Detector IDs this template emphasises — provenance/documentation of what
    # the template's pack surfaces. The REAL scoring is already wired: pack_id
    # activates the pack's detectors + scorer, and focus_defaults.focus_id drives
    # focus_affinity ranking. This field records that emphasis for the run and UI;
    # it does not itself change scoring. Empty = "whatever the pack emphasises".
    detector_emphasis: List[str] = field(default_factory=list)
    terminology: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Reconcile the singular pack_id with the packs list into ONE
        # order-preserving, de-duplicated selection (the shared primitive), then
        # re-derive pack_id as the primary (first) pack. So a template author may
        # set either field and both stay consistent; a single-pack template is
        # unchanged (packs == [pack_id]).
        from discovery.packs.pack_config import normalize_pack_ids

        combined = normalize_pack_ids(
            list(self.packs or []) + ([self.pack_id] if self.pack_id else [])
        )
        self.packs = combined
        if combined:
            self.pack_id = combined[0]


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

        known = list_packs()
        # R191-P1 T5: validate EVERY declared pack (a template may activate more
        # than one), not just the primary — a template cannot point at a pack that
        # does not exist.
        for pid in (defn.packs or [defn.pack_id]):
            if pid not in known:
                raise ValueError(
                    f"Template '{defn.template_id}' references unknown pack "
                    f"'{pid}'. Known packs: {sorted(known)}"
                )
    TEMPLATE_REGISTRY[defn.template_id] = defn


def unregister_template(template_id: str) -> None:
    """Remove a template if present (idempotent). Used by test teardown."""
    TEMPLATE_REGISTRY.pop(template_id, None)


# ── Launch resolution + provenance (R18-C1 T2) ────────────────────────────────

def template_defaults_snapshot(defn: TemplateDefinition) -> Dict[str, Any]:
    """A plain-dict snapshot of the defaults a template contributes to a launch."""
    return {
        "template_id": defn.template_id,
        "pack_id": defn.pack_id,
        "packs": list(defn.packs),
        "focus_id": defn.focus_defaults.focus_id,
        "focus_emphasis": list(defn.focus_defaults.emphasis),
        "suggested_systems": list(defn.suggested_systems),
        "suggested_roles": dict(defn.suggested_roles),
        "detector_emphasis": list(defn.detector_emphasis),
        "terminology": dict(defn.terminology),
    }


def resolve_launch_config(
    template_id: Optional[str],
    *,
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
    # R191-P1 T5: the caller's explicit pack selection (multi-select UI or an
    # explicit pack_ids/pack_id on the request), folded into ONE order-preserving,
    # de-duplicated list. Non-empty => the caller is authoritative over the
    # template's packs (an edit); empty => the template's packs apply.
    submitted_pack_ids = normalize_pack_ids(
        list(pack_ids or []) + ([pack_id] if pack_id else [])
    )

    defn = get_template(template_id) if template_id else None

    if defn is None:
        eff_pack_ids = submitted_pack_ids
        return {
            "effective": {
                "pack_id": pack_id or (eff_pack_ids[0] if eff_pack_ids else None),
                "pack_ids": eff_pack_ids,
                "focus_id": focus_id,
                "selected_system_ids": submitted_systems,
                "roles": _roles_from_weightings(submitted_weightings),
            },
            "provenance": {
                "template_id": template_id,
                "applied": False,
                "template_defaults": None,
                "edited_fields": [],
                "untouched": False,
            },
        }

    defaults = template_defaults_snapshot(defn)
    edited_fields: List[str] = []

    # pack selection (R191-P1 T5) — the template's full `packs` list is honored
    # end to end. An explicit caller submission (singular pack_id or plural
    # pack_ids) is authoritative and overrides the template; an empty submission
    # inherits the template's packs, so an UNTOUCHED multi-pack template activates
    # every one of its packs on run creation (AC5). `eff_pack` is always the
    # primary (first) of the effective list, consistent with the singular pack_id.
    if submitted_pack_ids:
        eff_pack_ids = submitted_pack_ids
    else:
        eff_pack_ids = normalize_pack_ids(list(defn.packs))
    eff_pack = eff_pack_ids[0] if eff_pack_ids else defn.pack_id
    # An explicit selection whose primary diverges from the template's primary is
    # recorded as a pack edit (provenance / AC5 of R18-C1).
    if submitted_pack_ids and eff_pack != defn.pack_id:
        edited_fields.append("pack_id")

    # focus_id — same rule.
    eff_focus = focus_id or defn.focus_defaults.focus_id
    if focus_id and focus_id != defn.focus_defaults.focus_id:
        edited_fields.append("focus_id")

    # selected_system_ids — empty submission inherits the template's suggested
    # systems; a non-empty submission that differs (as a set) is an edit.
    if submitted_systems:
        eff_systems = submitted_systems
        if set(submitted_systems) != set(defn.suggested_systems):
            edited_fields.append("selected_system_ids")
    else:
        eff_systems = list(defn.suggested_systems)

    # roles — start from the template's suggested roles, overlay any caller
    # weightings' roles; a role that differs from the template default is an edit.
    submitted_roles = _roles_from_weightings(submitted_weightings)
    eff_roles = dict(defn.suggested_roles)
    roles_edited = False
    for system_id, role in submitted_roles.items():
        if eff_roles.get(system_id) != role:
            roles_edited = True
        eff_roles[system_id] = role
    if roles_edited:
        edited_fields.append("roles")

    return {
        "effective": {
            "pack_id": eff_pack,
            "pack_ids": eff_pack_ids,
            "focus_id": eff_focus,
            "selected_system_ids": eff_systems,
            "roles": eff_roles,
        },
        "provenance": {
            "template_id": defn.template_id,
            "applied": True,
            "template_defaults": defaults,
            "edited_fields": edited_fields,
            "untouched": not edited_fields,
        },
    }


def _roles_from_weightings(weightings: Dict[str, Any]) -> Dict[str, str]:
    """Extract system_id -> role from a SystemWeighting map (role key optional)."""
    roles: Dict[str, str] = {}
    for system_id, w in (weightings or {}).items():
        if isinstance(w, dict) and w.get("role"):
            roles[system_id] = w["role"]
    return roles
