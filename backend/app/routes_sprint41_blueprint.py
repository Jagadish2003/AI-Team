"""
T41-1d — Agentforce Blueprint Backend Endpoint v1.2

Changes from v1.0:
  - detector_id read from opp['_debug']['detector_id'] (actual persisted field)
    NOT derived from opp['category'] (display label — fragile, must not be used)
  - Category-to-detector mapping removed entirely
  - Shape guards: all field accesses use .get() with safe defaults
  - Frontend renders this response directly (single source of truth)

GET /api/runs/{runId}/opportunities/{oppId}/blueprint

Deterministic: same run_id + opp_id always returns the same blueprint.
Computed on read from existing run KV — not stored separately.

Wire-in (main.py):
  from .routes_sprint41_blueprint import register_blueprint_routes
  register_blueprint_routes(app)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from .security import require_auth
from . import db
from .llm_enrichment import KV_LLM_ENRICHMENT


# ── Detector metadata keyed by detector_id ───────────────────────────────────
# detector_id values are stable constants defined in runner.py detectors.
# They must never change for blueprint derivation to remain correct.
# Category labels are NOT used here — they are display-only.

_DETECTOR_META: Dict[str, Dict[str, Any]] = {
    "REPETITIVE_AUTOMATION": {
        "agent_name": "Flow Automation Agent",
        "actions": [
            {
                "action": "Evaluate trigger conditions",
                "object": "AutoLaunchedFlow (Tooling API)",
                "detail": "Assess whether the flow logic can be expressed as agent reasoning rather than static automation.",
            },
            {
                "action": "Handle decisioning and exception routing",
                "object": "Case object",
                "detail": "Agent replaces low-complexity flow logic with dynamic decisioning based on case context.",
            },
            {
                "action": "Escalate to human when exception threshold exceeded",
                "object": "Case owner assignment",
                "detail": "Agent escalates cases that do not match known patterns to a human agent.",
            },
        ],
        "guardrails": [
            "Agent must not modify flow metadata or deactivate existing flows autonomously.",
            "Agent must escalate any case type it has not previously handled successfully.",
        ],
    },
    "HANDOFF_FRICTION": {
        "agent_name": "Case Routing Agent",
        "actions": [
            {
                "action": "Analyse case attributes at creation",
                "object": "Case object",
                "detail": "Agent reads Case subject, type, category, and description to determine correct team assignment.",
            },
            {
                "action": "Assign directly to correct queue or agent",
                "object": "OwnerId (Case)",
                "detail": "Agent sets OwnerId or assigns to Queue without intermediate routing steps.",
            },
            {
                "action": "Monitor reassignment patterns and flag anomalies",
                "object": "CaseHistory",
                "detail": "Agent tracks owner change frequency and surfaces cases with repeated reassignments for review.",
            },
        ],
        "guardrails": [
            "Agent must not reassign cases that have active customer communication in progress.",
            "Agent must preserve the existing escalation path for Priority 1 cases.",
        ],
    },
    "APPROVAL_BOTTLENECK": {
        "agent_name": "Approval Automation Agent",
        "actions": [
            {
                "action": "Evaluate approval criteria against policy thresholds",
                "object": "ProcessInstance / ProcessInstanceStep",
                "detail": "Agent checks the relevant approval fields against pre-approved policy rules.",
            },
            {
                "action": "Auto-approve standard exceptions within policy",
                "object": "ProcessInstanceWorkitem",
                "detail": "Agent approves items that meet all standard criteria without requiring human approver action.",
            },
            {
                "action": "Escalate non-standard exceptions to approver",
                "object": "Approver user record",
                "detail": "Agent routes genuinely non-standard requests to the appropriate approver with context summary.",
            },
        ],
        "guardrails": [
            "Agent must not approve items above the configured threshold without human review.",
            "Agent must not modify approval process configuration or approver assignments.",
        ],
    },
    "KNOWLEDGE_GAP": {
        "agent_name": "Knowledge Assist Agent",
        "actions": [
            {
                "action": "Surface relevant Knowledge Articles at case creation",
                "object": "KnowledgeArticle / CaseArticle",
                "detail": "Agent queries Knowledge base using case subject and description to find matching articles.",
            },
            {
                "action": "Attach recommended article to case record",
                "object": "CaseArticle (junction object)",
                "detail": "Agent links the most relevant article to the case so resolution is documented.",
            },
            {
                "action": "Flag cases closed without KB linkage for knowledge gap analysis",
                "object": "Case (closed)",
                "detail": "Agent identifies resolution patterns that do not have a corresponding Knowledge Article.",
            },
        ],
        "guardrails": [
            "Agent must not modify or publish Knowledge Articles autonomously.",
            "Agent must not close cases — it surfaces knowledge and assists, escalating resolution to a human agent.",
        ],
    },
    "INTEGRATION_CONCENTRATION": {
        "agent_name": "Integration Health Agent",
        "actions": [
            {
                "action": "Monitor named credential usage and failure rates",
                "object": "NamedCredential (Tooling API)",
                "detail": "Agent tracks which external systems are called most frequently and surfaces failure patterns.",
            },
            {
                "action": "Alert on dependency concentration risk",
                "object": "Flow and process metadata",
                "detail": "Agent identifies when a single external system is referenced by a disproportionate number of automations.",
            },
        ],
        "guardrails": [
            "Agent must not modify Named Credentials or external system configurations.",
            "Agent must escalate any integration failures to the responsible system owner.",
        ],
    },
    "PERMISSION_BOTTLENECK": {
        "agent_name": "Approval Queue Agent",
        "actions": [
            {
                "action": "Monitor approval queue depth in real time",
                "object": "ProcessInstanceWorkitem",
                "detail": "Agent tracks pending items per approver and surfaces queue depth to operations teams.",
            },
            {
                "action": "Distribute load across available approvers",
                "object": "ProcessInstanceWorkitem / User",
                "detail": "Agent reassigns pending items when a single approver queue exceeds the configured threshold.",
            },
            {
                "action": "Notify approvers of pending items approaching SLA breach",
                "object": "Approver user record",
                "detail": "Agent sends structured notifications when items have been pending beyond the expected review window.",
            },
        ],
        "guardrails": [
            "Agent must not approve items — it manages queue routing and notifications only.",
            "Agent must not modify permission sets, profiles, or approval process configuration.",
        ],
    },
    "CROSS_SYSTEM_ECHO": {
        "agent_name": "Cross-System Sync Agent",
        "actions": [
            {
                "action": "Detect duplicate ticket creation across systems",
                "object": "Case (Salesforce) / Incident (ServiceNow)",
                "detail": "Agent identifies when the same issue has been logged in multiple systems.",
            },
            {
                "action": "Create bidirectional reference links",
                "object": "Case ExternalId / ServiceNow correlation_id",
                "detail": "Agent writes cross-system IDs to both records so teams have a joined view without switching systems.",
            },
            {
                "action": "Notify the service agent when a linked ticket is resolved",
                "object": "Case owner / Incident assignee",
                "detail": "Agent triggers a notification when the counterpart record in the other system is closed.",
            },
        ],
        "guardrails": [
            "Agent must not close or resolve tickets in either system autonomously.",
            "Agent must not expose data from one system in records of another system without data governance approval.",
        ],
    },
}

_FALLBACK_META: Dict[str, Any] = {
    "agent_name": "Custom Agent",
    "actions": [
        {
            "action": "Analyse opportunity signals",
            "object": "Salesforce records",
            "detail": "Agent evaluates the patterns identified by AgentIQ and takes appropriate automated action.",
        },
        {
            "action": "Escalate exceptions to a human reviewer",
            "object": "Record owner",
            "detail": "Agent routes non-standard cases to the appropriate team member.",
        },
    ],
    "guardrails": [
        "Agent must escalate to a human reviewer for any action that modifies records or financial data.",
    ],
}


def _derive_complexity(effort: int, tier: str) -> Dict[str, str]:
    """Derive implementation complexity from effort score. Deterministic."""
    if effort <= 3:
        note = (
            "Quick Win — Recommended as first pilot."
            if tier == "Quick Win"
            else "Standard complexity."
        )
        return {
            "label": "Standard Configuration",
            "description": (
                "Single-system agent using standard Salesforce Flow and Process Builder "
                "integration. No custom Apex required."
            ),
            "tier": note,
        }
    if effort <= 6:
        return {
            "label": "Custom Logic Required",
            "description": (
                "Requires custom Flow logic, approval process configuration, "
                "or multi-step agent reasoning."
            ),
            "tier": "Strategic",
        }
    return {
        "label": "Architecture Design Required",
        "description": (
            "Multi-system coordination, custom Apex, or complex agent orchestration. "
            "Requires a dedicated design phase before implementation."
        ),
        "tier": "Complex",
    }


def _build_blueprint(
    opp: Dict[str, Any],
    enrichment_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Derive blueprint from opportunity and optional LLM enrichment.

    detector_id is read from opp['_debug']['detector_id'] — the stable
    value persisted by track_a_adapter.py from the Track B runner payload.
    Category label is NEVER used for blueprint derivation.

    All field accesses use .get() with safe defaults.
    """
    # ── Read detector_id from _debug (stable) ────────────────────────────────
    debug = opp.get("_debug") or {}
    detector_id = debug.get("detector_id", "UNKNOWN") or "UNKNOWN"

    meta = _DETECTOR_META.get(detector_id, _FALLBACK_META)

    # Agent name — deterministic from detector_id
    opp_title = opp.get("title", "")
    agent_name = (
        f"Custom Agent — {opp_title}".strip(" —")
        if detector_id == "UNKNOWN"
        else meta["agent_name"]
    )

    # Agent topic — LLM if available, aiRationale fallback
    opp_id = opp.get("id", "")
    per_opp: Dict[str, Any] = {}
    if enrichment_data:
        per_opp = enrichment_data.get("perOpportunity") or {}

    opp_enrich = per_opp.get(opp_id) or {}
    llm_generated: bool = bool(opp_enrich.get("llmGenerated", False))
    ai_summary: str = opp_enrich.get("aiSummary") or ""

    if llm_generated and ai_summary.strip():
        agent_topic = ai_summary
        agent_topic_is_llm = True
    else:
        agent_topic = opp.get("aiRationale") or ""
        agent_topic_is_llm = False

    effort_raw = opp.get("effort") or 5
    try:
        effort = int(float(effort_raw))
    except (TypeError, ValueError):
        effort = 5
    tier = opp.get("tier") or "Strategic"

    return {
        "oppId":                opp_id,
        "agentName":            agent_name,
        "agentTopic":           agent_topic,
        "agentTopicIsLlm":      agent_topic_is_llm,
        "suggestedActions":     meta["actions"],
        "guardrails":           meta["guardrails"],
        "agentforcePermissions": opp.get("requiredPermissions") or [],
        "complexity":           _derive_complexity(effort, tier),
        "evidenceIds":          opp.get("evidenceIds") or [],
        "detectorId":           detector_id,
    }


# ── Response models ───────────────────────────────────────────────────────────

class BlueprintAction(BaseModel):
    action: str
    object: str
    detail: str


class BlueprintComplexity(BaseModel):
    label: str
    description: str
    tier: str


class BlueprintResponse(BaseModel):
    oppId: str
    agentName: str
    agentTopic: str
    agentTopicIsLlm: bool
    suggestedActions: List[BlueprintAction]
    guardrails: List[str]
    agentforcePermissions: List[str]
    complexity: BlueprintComplexity
    evidenceIds: List[str]
    detectorId: str


# ── Route registration ────────────────────────────────────────────────────────

def register_blueprint_routes(app) -> None:

    @app.get(
        "/api/runs/{run_id}/opportunities/{opp_id}/blueprint",
        response_model=BlueprintResponse,
        dependencies=[Depends(require_auth)],
        tags=["blueprint"],
    )
    def get_opportunity_blueprint(run_id: str, opp_id: str) -> BlueprintResponse:
        """
        Get the Agentforce Blueprint for a specific opportunity.

        Computed on read from existing run data.
        Deterministic: same run_id + opp_id always returns the same output.
        detector_id is read from opp._debug.detector_id — never from category.
        Returns 404 for unknown run_id or opp_id.
        """
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )

        opps: List[Dict[str, Any]] = db.run_kv_get("opps", run_id, [])
        opp = next((o for o in opps if o.get("id") == opp_id), None)
        if opp is None:
            raise HTTPException(
                status_code=404,
                detail=f"Opportunity '{opp_id}' not found in run '{run_id}'",
            )

        enrichment = db.run_kv_get(KV_LLM_ENRICHMENT, run_id, None)
        blueprint = _build_blueprint(opp, enrichment)

        return BlueprintResponse(**blueprint)
