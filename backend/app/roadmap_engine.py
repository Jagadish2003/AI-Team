from __future__ import annotations
from typing import List, Dict
from .roadmap_types import OpportunityCandidate, PermissionItem, PilotRoadmapModel, RoadmapStage

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

def uniq_permissions_merge(perms: List[PermissionItem]) -> List[PermissionItem]:
    # Merge by label: required = OR, satisfied = AND
    merged: Dict[str, PermissionItem] = {}
    for p in perms:
        label = str(p.get("label", "")).strip()
        if not label:
            continue
        if label not in merged:
            merged[label] = {
                "id": p.get("id", f"perm_{len(merged)+1:03d}"),
                "label": label,
                "required": bool(p.get("required", False)),
                "satisfied": bool(p.get("satisfied", False)),
            }
        else:
            merged[label]["required"] = bool(merged[label].get("required", False)) or bool(p.get("required", False))
            merged[label]["satisfied"] = bool(merged[label].get("satisfied", False)) and bool(p.get("satisfied", False))
    out: List[PermissionItem] = []
    for p in merged.values():
        p["readiness"] = readiness_from_permission(p)
        out.append(p)
    return out

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

    # Stage caps (match UI expectations): 30d=3 QW, 60d=2 Strategic, 90d=1 Complex
    # Approved items are never capped; they take priority and then we fill remaining slots.
    stage30_opps = approved_qw + unreviewed_qw[: max(0, 3 - len(approved_qw))]
    stage60_opps = approved_strat + unreviewed_strat[: max(0, 2 - len(approved_strat))]
    stage90_opps = approved_complex + unreviewed_complex[: max(0, 1 - len(approved_complex))]

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
