"""
routes_stack_builder.py — SB-12 Task 12 Sprint 7
Stack Builder API — Setup State Persistence + Industry Registry Endpoints

Registers with FastAPI via register_stack_builder_routes(app) called from main.py.

Endpoints:

  GET  /api/stack-builder/industries
       Returns all registered industries with labels, pack hints, and
       recommended systems. Used by Screen 1 frontend (Sprint 8 data-driven upgrade).

  GET  /api/stack-builder/industries/{industry_id}/system-defaults
       Returns industry-calibrated system defaults for all systems in the registry.
       Used by useSetupState (Sprint 8) to replace SYSTEM_DEFAULT_ASSUMPTIONS.

  GET  /api/stack-builder/industries/{industry_id}/recommendations
       Returns recommended system additions given selected_ids query param.
       Used by DiscoveryPlanScreen (Screen 4) recommended additions (Sprint 8).

  POST /api/stack-builder/setup-state/{org_id}
       Persist setup state for an org. Upserts into kv store.
       Allows users to return to a partially configured stack builder session.
       Body: SetupStatePayload (JSON blob of frontend state shape).

  GET  /api/stack-builder/setup-state/{org_id}
       Retrieve persisted setup state for an org.
       Returns 404 if no state exists for the org.

  DELETE /api/stack-builder/setup-state/{org_id}
       Clear persisted setup state for an org.
       Used when a user starts a fresh stack builder session.

Storage:
  Uses existing kv_get / kv_set from db.py.
  Key pattern: "stack_builder_state:{org_id}"
  This is consistent with the run_kv_get / run_kv_set pattern in db.py.

Auth:
  All endpoints use require_auth (same as all existing AgentIQ API routes).

Error handling:
  404 — org not found or no state persisted
  422 — invalid payload (FastAPI default validation)
  500 — storage failure (caught and returned as 500 with detail)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from .db import kv_get, kv_set
from .security import require_auth
from .rbac import require_role

from discovery.packs.industry_registry import (
    list_industries,
    get_industry,
    get_recommended_systems,
)
from discovery.packs.template_registry import (
    list_templates,
    get_template,
)


# ── KV key helpers ────────────────────────────────────────────────────────────

_STATE_PREFIX = "stack_builder_state"


def _state_key(org_id: str) -> str:
    return f"{_STATE_PREFIX}:{org_id}"


# ── Pydantic models ───────────────────────────────────────────────────────────

class SetupStatePayload(BaseModel):
    """
    Opaque JSON blob matching the frontend StackBuilderState shape.
    Backend stores without parsing — the frontend owns the schema.
    Validated only for JSON-serializability at the FastAPI layer.
    """

    state: Dict[str, Any]
    saved_at: Optional[str] = None


class IndustryListItem(BaseModel):
    industry_id: str
    label: str
    pack_hints: List[str]
    recommended_systems: List[str]


class SystemDefaultItem(BaseModel):
    system_id: str
    role: str
    priority: str
    workflow_focus: List[str]


class RecommendationItem(BaseModel):
    system_id: str


class TemplateFocusDefaults(BaseModel):
    focus_id: str
    emphasis: List[str]


class TemplateListItem(BaseModel):
    """
    A Stack Builder template as seen by the frontend template picker (R18-C1 T1).
    Every field is a starting default the user can edit before launch.
    """
    template_id: str
    label: str
    description: str
    suggested_systems: List[str]
    suggested_roles: Dict[str, str]
    focus_defaults: TemplateFocusDefaults
    pack_id: str
    detector_emphasis: List[str]
    terminology: Dict[str, str]
    metadata: Dict[str, Any]


def _to_template_item(defn) -> "TemplateListItem":
    return TemplateListItem(
        template_id=defn.template_id,
        label=defn.label,
        description=defn.description,
        suggested_systems=defn.suggested_systems,
        suggested_roles=defn.suggested_roles,
        focus_defaults=TemplateFocusDefaults(
            focus_id=defn.focus_defaults.focus_id,
            emphasis=defn.focus_defaults.emphasis,
        ),
        pack_id=defn.pack_id,
        detector_emphasis=defn.detector_emphasis,
        terminology=defn.terminology,
        metadata=defn.metadata,
    )


# ── Route registration ────────────────────────────────────────────────────────

def register_stack_builder_routes(app: FastAPI) -> None:
    """
    Register all Stack Builder API routes.
    Called from main.py after all other route registrations.
    """

    # ── Industry list ─────────────────────────────────────────────────────────

    @app.get(
        "/api/stack-builder/industries",
        response_model=List[IndustryListItem],
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List all industries in the Stack Builder registry",
        tags=["Stack Builder"],
    )
    def list_stack_builder_industries() -> List[IndustryListItem]:
        """
        Returns all registered industries with display labels, pack hints,
        and recommended system additions.
        """

        return [
            IndustryListItem(
                industry_id=ind.industry_id,
                label=ind.label,
                pack_hints=ind.pack_hints,
                recommended_systems=ind.recommended_systems,
            )
            for ind in list_industries()
        ]

    # ── System defaults per industry ──────────────────────────────────────────

    @app.get(
        "/api/stack-builder/industries/{industry_id}/system-defaults",
        response_model=List[SystemDefaultItem],
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="Get industry-calibrated system defaults",
        tags=["Stack Builder"],
    )
    def get_industry_system_defaults(
        industry_id: str,
    ) -> List[SystemDefaultItem]:
        """
        Returns industry-calibrated role, priority, and workflow focus defaults
        for all systems registered under this industry.
        """

        config = get_industry(industry_id)

        if not config:
            raise HTTPException(
                status_code=404,
                detail=f"Industry '{industry_id}' not found in registry",
            )

        return [
            SystemDefaultItem(
                system_id=system_id,
                role=defaults.role,
                priority=defaults.priority,
                workflow_focus=defaults.workflow_focus,
            )
            for system_id, defaults in config.system_defaults.items()
        ]

    # ── Recommended additions ─────────────────────────────────────────────────

    @app.get(
        "/api/stack-builder/industries/{industry_id}/recommendations",
        response_model=List[RecommendationItem],
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="Get recommended system additions for an industry",
        tags=["Stack Builder"],
    )
    def get_industry_recommendations(
        industry_id: str,
        selected: str = Query(
            default="",
            description=(
                "Comma-separated list of already-selected "
                "system IDs to exclude"
            ),
        ),
    ) -> List[RecommendationItem]:
        """
        Returns up to 3 recommended system IDs for this industry that are
        not already in the selected list.
        """

        config = get_industry(industry_id)

        if not config:
            raise HTTPException(
                status_code=404,
                detail=f"Industry '{industry_id}' not found in registry",
            )

        selected_ids = [
            s.strip()
            for s in selected.split(",")
            if s.strip()
        ]

        recs = get_recommended_systems(
            industry_id,
            selected_ids,
        )

        return [
            RecommendationItem(system_id=s)
            for s in recs
        ]

    # ── Template model (R18-C1 T1) ────────────────────────────────────────────
    # The frontend asks the backend "what templates are available?" and renders
    # the answer, instead of owning a hardcoded TEMPLATES array. The registry is
    # the single source of truth; a new template is a config entry only (AC4/AC8).

    @app.get(
        "/api/stack-builder/templates",
        response_model=List[TemplateListItem],
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List all Stack Builder templates from backend configuration",
        tags=["Stack Builder"],
    )
    def list_stack_builder_templates() -> List[TemplateListItem]:
        """
        Returns every configured template as a bundle of editable defaults:
        suggested systems, roles, focus defaults, pack selection, terminology,
        and metadata. Renders the template picker without any frontend hardcoding.
        """

        return [_to_template_item(defn) for defn in list_templates()]

    @app.get(
        "/api/stack-builder/templates/{template_id}",
        response_model=TemplateListItem,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="Get a single Stack Builder template's default configuration",
        tags=["Stack Builder"],
    )
    def get_stack_builder_template(template_id: str) -> TemplateListItem:
        """
        Returns one template's full default configuration, so choosing it can
        pre-populate the setup experience. 404 if the template is not registered.
        """

        defn = get_template(template_id)

        if not defn:
            raise HTTPException(
                status_code=404,
                detail=f"Template '{template_id}' not found in registry",
            )

        return _to_template_item(defn)

    # ── Setup state persistence ───────────────────────────────────────────────

    @app.post(
        "/api/stack-builder/setup-state/{org_id}",
        status_code=204,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Persist stack builder setup state for an org",
        tags=["Stack Builder"],
    )
    def save_setup_state(
        org_id: str,
        payload: SetupStatePayload,
    ):
        """
        Upsert the setup state for an org.
        """

        try:
            kv_set(
                _state_key(org_id),
                {
                    "org_id": org_id,
                    "state": payload.state,
                    "saved_at": payload.saved_at,
                },
            )

        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to persist setup state: {exc}",
            ) from exc

        try:
            from app.middleware.audit import log_event
            systems = payload.state.get("systems") if isinstance(payload.state, dict) else {}
            pack_id = payload.state.get("pack") if isinstance(payload.state, dict) else None
            log_event(
                "setup_state_saved",
                system_count=len(systems) if isinstance(systems, (list, dict)) else 0,
                pack_id=pack_id,
            )
        except Exception:
            pass

        return Response(status_code=204)

    @app.get(
        "/api/stack-builder/setup-state/{org_id}",
        response_model=SetupStatePayload,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="Retrieve persisted stack builder setup state for an org",
        tags=["Stack Builder"],
    )
    def load_setup_state(org_id: str) -> SetupStatePayload:
        """
        Retrieve the persisted setup state for an org.
        """

        record = kv_get(_state_key(org_id))

        if not record:
            raise HTTPException(
                status_code=404,
                detail=f"No setup state found for org '{org_id}'",
            )

        return SetupStatePayload(
            state=record.get("state", {}),
            saved_at=record.get("saved_at"),
        )

    @app.delete(
        "/api/stack-builder/setup-state/{org_id}",
        status_code=204,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Clear persisted stack builder setup state for an org",
        tags=["Stack Builder"],
    )
    def clear_setup_state(org_id: str):
        """
        Delete the persisted setup state for an org.
        Returns 204 whether or not state existed (idempotent).
        """

        try:
            kv_set(_state_key(org_id), None)

        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to clear setup state: {exc}",
            ) from exc

        return Response(status_code=204)