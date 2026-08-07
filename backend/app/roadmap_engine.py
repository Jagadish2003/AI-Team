from __future__ import annotations
from typing import Any, Dict, List
from .roadmap_types import OpportunityCandidate, PermissionItem, PilotRoadmapModel, RoadmapStage


_STATIC_PERMISSION_DEFAULTS: Dict[str, PermissionItem] = {
    "Salesforce: read FlowVersionView (Tooling API)": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read Flow Metadata (Tooling API)": {
        "required": False,
        "satisfied": False,
    },
    "Salesforce: read CaseHistory": {
        "required": False,
        "satisfied": False,
    },
    "Salesforce: read Case": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read ProcessInstance": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read ProcessInstanceWorkitem": {
        "required": True,
        "satisfied": False,
    },
    "Salesforce: read ProcessDefinition": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read CaseArticle": {
        "required": True,
        "satisfied": False,
    },
    "Salesforce: read NamedCredential (Tooling API)": {
        "required": True,
        "satisfied": True,
    },
    "ServiceNow: read incident (if applicable)": {
        "required": False,
        "satisfied": False,
    },
    "Jira: read issues (if applicable)": {
        "required": False,
        "satisfied": False,
    },
    "Salesforce: read LLC_BI__Loan__c": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Loan__History": {
        "required": False,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Checklist__c": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Checklist__c owner/status fields": {
        "required": False,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Covenant2__c": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Covenant2__c evaluation/breach fields": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Spread_Statement_Period__c": {
        "required": True,
        "satisfied": True,
    },
    "Salesforce: read LLC_BI__Analyst__c": {
        "required": False,
        "satisfied": False,
    },
}

def readiness_from_permission(p: PermissionItem) -> str:
    required = bool(p.get("required", False))
    satisfied = bool(p.get("satisfied", False))
    if required and not satisfied:
        return "MISSING"
    if (not required) and (not satisfied):
        return "PENDING"
    return "READY"

def overall_readiness(perms: List[PermissionItem]) -> str:
    if any(readiness_from_permission(p) == "MISSING" for p in perms):
        return "Low"
    if any(readiness_from_permission(p) == "PENDING" for p in perms):
        return "Moderate"
    return "High"

def uniq_permissions_merge(perms: List[Any]) -> List[PermissionItem]:
    # Merge by label: required = OR, satisfied = AND
    merged: Dict[str, PermissionItem] = {}
    for p in perms:
        # String permissions come from static opportunity metadata. Use a stable
        # label-based mapping so the roadmap can show a realistic mix of
        # READY / PENDING / MISSING without changing the raw opportunity contract.
        if isinstance(p, str):
            label = p.strip()
            defaults = _STATIC_PERMISSION_DEFAULTS.get(label, {})
            req = bool(defaults.get("required", True))
            sat = bool(defaults.get("satisfied", False))
            pid = f"perm_{len(merged)+1:03d}"
        else:
            label = str(p.get("label", "")).strip()
            req = bool(p.get("required", False))
            sat = bool(p.get("satisfied", False))
            pid = p.get("id", f"perm_{len(merged)+1:03d}")

        if not label:
            continue

        if label not in merged:
            merged[label] = {
                "id": pid,
                "label": label,
                "required": req,
                "satisfied": sat,
            }
        else:
            merged[label]["required"] = bool(merged[label].get("required", False)) or req
            merged[label]["satisfied"] = bool(merged[label].get("satisfied", False)) and sat

    out: List[PermissionItem] =[]
    for p_obj in merged.values():
        p_obj["readiness"] = readiness_from_permission(p_obj)
        out.append(p_obj)
    return out

def _apply_projection_strength_rule(
    stage_opps: List[OpportunityCandidate],
) -> List[OpportunityCandidate]:
    """2.0-A1 AC4 — projection strength, used carefully, inside one stage.

    "Carefully" is the whole point of this function. Projection strength does
    NOT re-rank the roadmap: stage membership stays tier-driven and approved
    items stay ahead of unreviewed ones, because those orderings encode analyst
    decisions that a projection has no business overturning.

    The one rule applied here is AC4's: a finding whose confidence is capped for
    want of corroboration never presents above a corroborated equivalent. Capped
    findings sink below uncapped ones within their stage; everything else keeps
    its incoming relative order (the sort is stable), so this narrows the
    existing ranking rather than replacing it.

    Deterministic and non-blocking: an opportunity with no projection is treated
    as uncapped and keeps its place, and a malformed projection can never raise
    here — the roadmap must build regardless.
    """
    try:
        from discovery.projection import demote_capped_projections
    except Exception:  # noqa: BLE001 - a roadmap must build without projections
        return list(stage_opps)

    try:
        return demote_capped_projections(
            stage_opps, lambda opp: (opp or {}).get("projection")
        )
    except Exception:  # noqa: BLE001 - ordering is advisory, never fatal
        return list(stage_opps)


def build_roadmap(opps: List[OpportunityCandidate]) -> PilotRoadmapModel:
    # Selection rules (match the TypeScript intent):
    # - Always include APPROVED items (they represent explicit analyst decisions)
    # - Fill remaining slots per tier with UNREVIEWED items (demo realism + stage coverage)
    approved = [o for o in opps if o.get("decision") == "APPROVED"]
    unreviewed = [o for o in opps if o.get("decision") == "UNREVIEWED"]

    # Bucket approved by tier (do not drop items with missing tier)
    approved_qw = [o for o in approved if o.get("tier") == "Quick Win"]
    approved_strat = [o for o in approved if o.get("tier") == "Strategic"]
    approved_complex = [o for o in approved if o.get("tier") == "Complex"]
    approved_unknown = [o for o in approved if o.get("tier") not in ("Quick Win", "Strategic", "Complex")]

    # UNREVIEWED candidates by tier
    unreviewed_qw = [o for o in unreviewed if o.get("tier") == "Quick Win"]
    unreviewed_strat = [o for o in unreviewed if o.get("tier") == "Strategic"]
    unreviewed_complex = [o for o in unreviewed if o.get("tier") == "Complex"]

    # No stage caps: every opportunity appears in its tier's stage
    # (Quick Win -> Phase 1, Strategic -> Phase 2, Complex -> Phase 3).
    # Approved items still take priority ordering ahead of unreviewed ones.
    stage30_opps = _apply_projection_strength_rule(approved_qw + unreviewed_qw)
    stage60_opps = _apply_projection_strength_rule(approved_strat + unreviewed_strat)
    stage90_opps = _apply_projection_strength_rule(approved_complex + unreviewed_complex)

    # If any APPROVED items are missing a tier, do NOT drop them silently.
    # For now, place them in the earliest stage so they remain visible to users.
    if approved_unknown:
        stage30_opps = approved_unknown + stage30_opps

    # Derived selection (deduped) for summary counts
    selected = []
    seen_ids = set()
    for o in (stage30_opps + stage60_opps + stage90_opps):
        oid = o.get("id")
        if oid and oid not in seen_ids:
            selected.append(o)
            seen_ids.add(oid)


    def mk_stage(title: str, sid: str, summary: str, stage_opps: List[OpportunityCandidate]) -> RoadmapStage:
        perms_raw: List[PermissionItem] = []
        for o in stage_opps:
            perms_raw.extend(o.get("requiredPermissions") or o.get("permissions") or [])
        perms = uniq_permissions_merge(perms_raw)
        return {
            "id": sid,
            "title": title,
            "summary": summary,
            "opportunities": stage_opps,
            "requiredPermissions": perms,
            "dependencies": [],
            "readiness": overall_readiness(perms),
        }

    s30 = mk_stage("Next 30 Days", "NEXT_30", "Prove value fast with low-effort quick wins.", stage30_opps)
    s60 = mk_stage("Next 60 Days", "NEXT_60", "Scale into strategic pilots with cross-team alignment.", stage60_opps)
    s90 = mk_stage("Next 90 Days", "NEXT_90", "Invest in complex opportunities requiring deeper data + governance.", stage90_opps)

    all_perms = uniq_permissions_merge((s30["requiredPermissions"] + s60["requiredPermissions"] + s90["requiredPermissions"]))
    return {
        "stages": [s30, s60, s90],
        "selectedCount": len(selected),
        "permissionsRequiredCount": sum(1 for p in all_perms if p.get("required")),
        "dependenciesCount": 0,
        "overallReadiness": overall_readiness(all_perms),
    }


def apply_learned_adjustment(roadmap: PilotRoadmapModel) -> PilotRoadmapModel:
    """2.0-A3 T2 — the bounded learned adjustment, applied at SERVE time.

    Deliberately NOT called from :func:`build_roadmap`. ``build_roadmap`` runs
    during materialization and its result is STORED (``run_kv_set("roadmap", …)``
    in ``materialize_t2`` and ``routes_sprint4_t1``). Adjusting inside it would
    bake the learned order into storage, and then the stored roadmap would no
    longer be base order — so disabling learning could not restore it, and "what
    would this have ranked without learning?" would have no answer for the
    roadmap surface. Materialization also runs without a request-scoped tenancy
    context, so the org would be wrong or absent.

    So the roadmap is BUILT in base order and stored that way, and this reorders
    a COPY on the way out, exactly as ``list_opportunities`` does.

    Routed through the single adjustment function in ``app.learning_adjustment``;
    this is a call site, not a second implementation.

    Reorders WITHIN each stage only — stage membership is tier-driven and stays
    untouched, so tier placement, approved-before-unreviewed and A1 T4's
    capped-confidence demotion (already applied at build time) all survive.

    Non-blocking: a roadmap must serve whether or not learning is available.
    """
    if not isinstance(roadmap, dict) or not roadmap.get("stages"):
        return roadmap
    try:
        from .learning_adjustment import RANK_SCOPE_ROADMAP_STAGE, adjust_ranking
        from .learning_adjustment_state import get_adjustments
        from .learning_signals import collect_learning_signals
        from .middleware.tenancy import get_current_org_id

        org_id = get_current_org_id()
        adjustments = get_adjustments(org_id)
        if not adjustments:
            return roadmap

        # Resolved ONCE for the whole roadmap rather than per stage: three
        # stages would otherwise mean three identical signal-set reads.
        signal_set = collect_learning_signals(org_id)
        if not signal_set.is_active:
            return roadmap

        adjusted = dict(roadmap)
        adjusted["stages"] = [
            {
                **stage,
                "opportunities": list(
                    adjust_ranking(
                        stage.get("opportunities") or [],
                        adjustments,
                        is_active=True,
                        inactive_reason=signal_set.inactive_reason,
                        # Ranks here index THIS STAGE, not the run. The explain
                        # endpoint adjusts the flat list and so reports a
                        # run-global rank for the same finding — declaring the
                        # scope is what stops the two "moved N places" figures
                        # being read as a contradiction.
                        rank_scope=RANK_SCOPE_ROADMAP_STAGE,
                    ).ordered
                ),
            }
            for stage in roadmap["stages"]
        ]
        return adjusted
    except Exception:  # noqa: BLE001 - ordering is advisory, never fatal
        return roadmap
