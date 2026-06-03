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
from pydantic import BaseModel, Field

from .db import run_kv_set, next_run_id, upsert_run
from .security import require_auth
from .rbac import require_role


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
    selected_system_ids: List[str] = Field(
        default_factory=list,
        description="All selected system IDs including Salesforce cloud IDs",
    )
    pack_id: str = Field(
        description=(
            "Pack resolved by frontend from industry pack hints. "
            "Falls back to 'service_cloud' if no industry or hints."
        ),
    )
    weightings: Dict[str, Any] = Field(
        default_factory=dict,
        description="SystemWeighting per system_id — role, priority, workflowFocus, confirmed",
    )


class LaunchResponse(BaseModel):
    ok: bool = True
    runId: str
    packId: str
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
    def launch_stack_builder_run(body: LaunchRequest) -> LaunchResponse:
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
        if not body.selected_system_ids:
            raise HTTPException(
                status_code=400,
                detail="selected_system_ids must not be empty",
            )

        if not body.pack_id:
            raise HTTPException(
                status_code=400,
                detail="pack_id is required",
            )

        # Generate run ID
        run_id = next_run_id()
        now = datetime.now(timezone.utc).isoformat()

        # Persist run record
        run_record: Dict[str, Any] = {
            "id": run_id,
            "status": "created",
            "startedAt": now,
            "updatedAt": now,
            "orgId": body.org_id,
            "packId": body.pack_id,
            "focusId": body.focus_id,
            "industryId": body.industry_id,
            "templateId": body.template_id,
            "selectedSystemIds": body.selected_system_ids,
            "systemCount": len(body.selected_system_ids),
            "source": "stack_builder",
        }
        upsert_run(run_id, run_record)

        # Store run-scoped context entries
        # pack_id is used by normalization and evidence_builder for pack routing
        run_kv_set("pack_id", run_id, body.pack_id)

        # focus_id and industry_id used by LLM enrichment (Sprint 8)
        # for industry-aware prompt injection
        if body.focus_id:
            run_kv_set("focus_id", run_id, body.focus_id)
        if body.industry_id:
            run_kv_set("industry_id", run_id, body.industry_id)

        # Full setup context — weightings, template, all system IDs
        # Available for evidence_builder and enrichment to consume
        run_kv_set("setup_context", run_id, {
            "org_id":              body.org_id,
            "focus_id":            body.focus_id,
            "industry_id":         body.industry_id,
            "template_id":         body.template_id,
            "selected_system_ids": body.selected_system_ids,
            "pack_id":             body.pack_id,
            "weightings":          body.weightings,
        })

        return LaunchResponse(
            runId=run_id,
            packId=body.pack_id,
            focusId=body.focus_id,
            industryId=body.industry_id,
            systemCount=len(body.selected_system_ids),
        )
