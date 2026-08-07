"""
routes_stack_builder_launch.py — SB-13 Task 13 Sprint 7
Stack Builder Launch Endpoint

Adds POST /api/stack-builder/launch to the Stack Builder API.
Called by StackBuilderRouter.handleLaunch after the user clicks
"Start discovery" on Screen 4.

Registered by adding one call to register_stack_builder_launch_routes(app)
in routes_stack_builder.py (or directly in main.py).

What this endpoint does:
  1. Accepts the full setup state from the frontend
  2. Persists it as a run-scoped context (run_kv_set)
  3. Creates and starts a run via start_run_()
  4. Stores pack_id, focus_id, industry_id on the run record
  5. Returns runId — frontend then POSTs to /api/runs/{runId}/compute

Why a dedicated launch endpoint (not just POST /api/runs):
  The existing POST /api/runs/{run_id}/compute accepts a ComputeRequest
  (mode, systems, pack). The setup state carries significantly more context
  (focus_id, industry_id, weightings, selected_system_ids, template_id)
  that the discovery pipeline needs for pack-aware enrichment and scoring.
  The launch endpoint bridges the setup state shape to the run store shape
  without modifying the existing compute endpoint.

Pack selection:
  pack_id is resolved by the frontend (StackBuilderRouter.resolvePackId)
  and passed explicitly. Backend stores it and uses it when the compute
  endpoint is called. This keeps pack resolution in one place (the frontend
  industry pack hints map) while allowing backend override if needed.

Multi-pack note (Sprint 5.1):
  ENG-PACK-SELECTOR (Sprint 5.1) will extend this endpoint to support
  multi-pack runs. The launch endpoint will accept a packs: list[str]
  instead of a single pack_id, and the compute step will run one discovery
  per pack and merge results. The current single-pack design is the correct
  Sprint 7 foundation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from .db import run_kv_set, next_run_id, upsert_run
from .middleware.tenancy import get_current_org_id
from .security import require_auth
from .middleware.audit import OUTCOME_SUCCESS, RUN_STARTED, log_event as audit_log_event
from .rbac import _get_user_id_from_token, require_role

from discovery.packs.pack_config import get_pack_version, normalize_pack_ids
from discovery.packs.template_registry import (
    get_template,
    normalize_template_ids,
    resolve_launch_config,
)


# ── Request / response models ─────────────────────────────────────────────────

class LaunchRequest(BaseModel):
    """
    Setup state payload from StackBuilderRouter.handleLaunch.
    All fields mirror the frontend StackBuilderState shape.
    """
    org_id: str = Field(description="Org identifier for run attribution")
    focus_id: Optional[str] = Field(default=None, description="Selected FocusId")
    industry_id: Optional[str] = Field(default=None, description="Selected IndustryId")
    template_id: Optional[str] = Field(default=None, description="Selected TemplateId")
    template_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Order-preserving template selection. template_id remains the primary "
            "backward-compatible alias."
        ),
    )
    selected_system_ids: List[str] = Field(
        default_factory=list,
        description="All selected system IDs including Salesforce cloud IDs",
    )
    pack_id: Optional[str] = Field(
        default=None,
        description=(
            "Pack resolved by frontend from industry pack hints. "
            "Falls back to 'service_cloud' if no industry or hints. "
            "Optional when template_id is set — the template supplies its pack "
            "for an untouched launch (R18-C1 T2). Backward-compatible singular "
            "alias for pack_ids — see pack_ids (R191-P1 T1)."
        ),
    )
    pack_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "R191-P1 T1: multi-pack selection (order-preserving, de-duplicated). "
            "Supersedes the singular pack_id, which stays accepted as the "
            "primary-pack alias. A single-element list behaves exactly as a "
            "singular pack_id — the first id is the primary pack. Reconciled with "
            "pack_id in the validator; both are always kept in sync."
        ),
    )
    weightings: Dict[str, Any] = Field(
        default_factory=dict,
        description="SystemWeighting per system_id — role, priority, workflowFocus, confirmed",
    )

    @model_validator(mode="after")
    def _reconcile_and_require_pack(self) -> "LaunchRequest":
        combined_templates = normalize_template_ids(
            list(self.template_ids or [])
            + ([self.template_id] if self.template_id else [])
        )
        self.template_ids = combined_templates
        self.template_id = combined_templates[0] if combined_templates else None

        # R191-P1 T1: reconcile the singular pack_id with the pack_ids list into
        # ONE order-preserving, de-duplicated selection. The list wins ordering;
        # the singular alias is appended (dedup collapses the common case where a
        # caller sends the same id both ways). pack_id is then re-derived as the
        # primary (first) pack, so every existing single-pack caller — and all
        # backward-compatible reads of pack_id — see exactly today's value.
        from discovery.packs.pack_config import normalize_pack_ids

        combined = normalize_pack_ids(
            list(self.pack_ids or []) + ([self.pack_id] if self.pack_id else [])
        )
        self.pack_ids = combined
        self.pack_id = combined[0] if combined else None

        # A pack is mandatory unless a REGISTERED template is selected to supply
        # one — this keeps the pre-T2 contract (no pack, and no template that can
        # provide a pack → 422) intact while letting an untouched template drive
        # the pack (R18-C1 T2 / AC2). An unknown template_id cannot supply a pack.
        if self.pack_id:
            return self
        if any(get_template(template_id) is not None for template_id in combined_templates):
            return self
        raise ValueError(
            "pack_id is required when no known template_id is provided"
        )


class LaunchResponse(BaseModel):
    ok: bool = True
    runId: str
    packId: str
    # R191-P1 T1: the full order-preserving, de-duplicated pack selection. packId
    # stays the primary (first) pack for backward compatibility; packIds carries
    # the whole list so multi-pack execution (a later P1 task) has its config.
    packIds: List[str] = Field(default_factory=list)
    templateIds: List[str] = Field(default_factory=list)
    focusId: Optional[str] = None
    industryId: Optional[str] = None
    systemCount: int


# ── Route registration ────────────────────────────────────────────────────────

def register_stack_builder_launch_routes(app: FastAPI) -> None:
    """
    Register the Stack Builder launch endpoint.
    Call from main.py after register_stack_builder_routes(app).
    """

    @app.post(
        "/api/stack-builder/launch",
        response_model=LaunchResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Launch a discovery run from Stack Builder setup state",
        tags=["Stack Builder"],
    )
    def launch_stack_builder_run(
        body: LaunchRequest,
        token: str = Depends(require_auth),
    ) -> LaunchResponse:
        """
        Creates a run from the full Stack Builder setup state.

        Steps:
          1. Generate a new run_id
          2. Persist the run record with setup state metadata
          3. Store setup context as run-scoped KV entries:
               pack_id:{run_id}       → body.pack_id
               focus_id:{run_id}      → body.focus_id
               industry_id:{run_id}   → body.industry_id
               setup_context:{run_id} → full setup state blob
          4. Return runId for the frontend to use in /api/runs/{runId}/compute

        The frontend follows with:
          POST /api/runs/{runId}/compute
            { mode: 'offline', systems: [...normalised], pack: packId }

        Returns 400 if selected_system_ids is empty (cannot run with no systems).
        """
        # R18-C1 T2: resolve the effective launch config against the selected
        # template. Every template value is an editable default — a field the
        # caller submitted wins (and is recorded as a user edit); a field the
        # caller left empty inherits the template default, so an UNTOUCHED
        # template applies its lending pack + focus (AC1/AC2/AC5). With no
        # template_id this is a pass-through of the submitted values.
        resolved = resolve_launch_config(
            body.template_id,
            template_ids=body.template_ids,
            pack_id=body.pack_id,
            pack_ids=body.pack_ids,
            focus_id=body.focus_id,
            selected_system_ids=body.selected_system_ids,
            weightings=body.weightings,
        )
        effective = resolved["effective"]
        provenance = resolved["provenance"]

        eff_pack = effective["pack_id"]
        eff_pack_ids = effective["pack_ids"]
        eff_template_ids = effective["template_ids"]
        eff_template = eff_template_ids[0] if eff_template_ids else None
        eff_focus = effective["focus_id"]
        eff_systems = effective["selected_system_ids"]
        eff_roles = effective["roles"]

        if not eff_systems:
            raise HTTPException(
                status_code=400,
                detail="selected_system_ids must not be empty",
            )

        if not eff_pack:
            raise HTTPException(
                status_code=400,
                detail="pack_id is required",
            )

        # Versions are captured at launch so historical provenance cannot drift
        # when a registry entry or pack is updated later.
        pack_versions = {
            selected_pack_id: get_pack_version(selected_pack_id)
            for selected_pack_id in eff_pack_ids
        }
        template_versions = {
            snapshot["template_id"]: snapshot["template_version"]
            for snapshot in provenance.get("template_defaults_list", [])
        }

        # Generate run ID
        run_id = next_run_id()
        now = datetime.now(timezone.utc).isoformat()
        org_id = get_current_org_id()

        # Build the exact weighting map the discovery engine will read. An
        # untouched template request may omit weightings entirely, so seed its
        # configured roles here instead of persisting an empty map (which would
        # make weighting_context.load_for_run fall back to neutral behavior).
        # Submitted values still win and systems removed by the user are omitted.
        effective_weightings: Dict[str, Any] = {}
        for system_id in eff_systems:
            submitted = body.weightings.get(system_id)
            weighting = dict(submitted) if isinstance(submitted, dict) else {}
            role = eff_roles.get(system_id)
            if role and not weighting.get("role"):
                weighting["role"] = role
            weighting.setdefault("systemId", system_id)
            weighting.setdefault(
                "priority",
                "primary" if role == "system_of_record" else "secondary",
            )
            weighting.setdefault("workflowFocus", [])
            weighting.setdefault("confirmed", False)
            effective_weightings[system_id] = weighting

        # Persist run record. Template provenance (which template was selected,
        # which fields the user edited vs. the template defaults, and whether the
        # template was launched untouched) lives on the run so analysts can see
        # what configuration shaped the output (AC2/AC5).
        run_record: Dict[str, Any] = {
            "id": run_id,
            "status": "created",
            "startedAt": now,
            "updatedAt": now,
            "orgId": org_id,
            "packId": eff_pack,
            "packIds": eff_pack_ids,
            "packVersions": pack_versions,
            "focusId": eff_focus,
            "industryId": body.industry_id,
            "templateId": eff_template,
            "templateIds": eff_template_ids,
            "templateVersions": template_versions,
            "selectedSystemIds": eff_systems,
            "weightings": effective_weightings,
            "systemCount": len(eff_systems),
            "source": "stack_builder",
            "templateProvenance": provenance,
            "packBoundaries": effective["pack_boundaries"],
            "effectiveConfiguration": effective,
        }
        upsert_run(run_id, run_record)

        # Store run-scoped context entries
        # pack_id is used by normalization and evidence_builder for pack routing
        run_kv_set("pack_id", run_id, eff_pack)
        # R191-P1 T1: the full multi-pack selection, alongside the singular
        # pack_id (kept as the primary-pack alias) so nothing reading pack_id
        # changes and downstream multi-pack execution can read pack_ids.
        run_kv_set("pack_ids", run_id, eff_pack_ids)
        run_kv_set("pack_versions", run_id, pack_versions)
        run_kv_set("template_ids", run_id, eff_template_ids)
        run_kv_set("template_versions", run_id, template_versions)
        run_kv_set("effective_configuration", run_id, effective)

        # focus_id and industry_id used by LLM enrichment (Sprint 8)
        # for industry-aware prompt injection
        if eff_focus:
            run_kv_set("focus_id", run_id, eff_focus)
        if body.industry_id:
            run_kv_set("industry_id", run_id, body.industry_id)

        # Full setup context — weightings, template, all system IDs, plus the
        # template provenance. Available for evidence_builder and enrichment to
        # consume. Effective (template-resolved) values are stored so the
        # pipeline reads the same config the run record reports.
        run_kv_set("setup_context", run_id, {
            "org_id":              org_id,
            "focus_id":            eff_focus,
            "industry_id":         body.industry_id,
            "template_id":         eff_template,
            "template_ids":        eff_template_ids,
            "selected_system_ids": eff_systems,
            "pack_id":             eff_pack,
            "pack_ids":            eff_pack_ids,
            "pack_versions":       pack_versions,
            "weightings":          effective_weightings,
            "template_provenance": provenance,
            "pack_boundaries":      effective["pack_boundaries"],
            "effective_configuration": effective,
        })

        # 2.0-D4 T1 (AC1): D4 names "run start". routes_sprint4_t2 emitted
        # run_started for its own path only, so a run launched from the Stack
        # Builder — the path the product actually uses — left no audit row. The
        # payload records WHAT was launched (packs, systems, focus), because
        # "which sources did this run read?" is the question a reviewer follows
        # a run start with.
        audit_log_event(
            RUN_STARTED,
            run_id=run_id,
            user_id=_get_user_id_from_token(token),
            target=run_id,
            pack_ids=eff_pack_ids,
            template_ids=eff_template_ids,
            focus_id=eff_focus,
            system_count=len(eff_systems),
            systems=eff_systems,
            source="stack_builder",
            outcome=OUTCOME_SUCCESS,
        )

        return LaunchResponse(
            runId=run_id,
            packId=eff_pack,
            packIds=eff_pack_ids,
            templateIds=eff_template_ids,
            focusId=eff_focus,
            industryId=body.industry_id,
            systemCount=len(eff_systems),
        )
