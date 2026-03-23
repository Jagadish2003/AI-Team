from typing import Any, Dict, List

def uniq_by_label(perms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    m: Dict[str, Dict[str, Any]] = {}
    for p in perms:
        key = p.get("label")
        if key not in m:
            m[key] = dict(p)
        else:
            m[key]["required"] = bool(m[key].get("required")) or bool(p.get("required"))
            m[key]["satisfied"] = bool(m[key].get("satisfied")) and bool(p.get("satisfied"))
    return list(m.values())

def readiness_from_permission(p: Dict[str, Any]) -> str:
    if p.get("satisfied") is True:
        return "READY"
    return "MISSING" if p.get("required") else "PENDING"

def overall_readiness(perms: List[Dict[str, Any]]) -> str:
    if any(readiness_from_permission(p) == "MISSING" for p in perms):
        return "Low"
    if any(readiness_from_permission(p) == "PENDING" for p in perms):
        return "Moderate"
    return "High"

def build_pilot_roadmap(opps: List[Dict[str, Any]]) -> Dict[str, Any]:
    approved = [o for o in opps if o.get("decision") == "APPROVED"]
    unreviewed = [o for o in opps if o.get("decision") == "UNREVIEWED"]

    def score(o): return (o.get("impact",0) - o.get("effort",0), o.get("impact",0))
    unreviewed.sort(key=score, reverse=True)

    unreviewed_qw = [o for o in unreviewed if o.get("tier") == "Quick Win"][:3]
    unreviewed_strat = [o for o in unreviewed if o.get("tier") == "Strategic"][:2]
    unreviewed_complex = [o for o in unreviewed if o.get("tier") == "Complex"][:1]

    selected = approved + unreviewed_qw + unreviewed_strat + unreviewed_complex

    stage30 = [o for o in selected if o.get("tier") == "Quick Win"][:3]
    stage60 = [o for o in selected if o.get("tier") == "Strategic"][:3]
    stage90 = [o for o in selected if o.get("tier") == "Complex"][:2]

    def mk_deps(stage_id: str):
        base = [
            {"id":"dep_owner","label":"Business owner + success metrics confirmed","status":"READY"},
            {"id":"dep_access","label":"Data access approvals completed","status":"PENDING"},
            {"id":"dep_security","label":"Security review (read-only connectors)","status":"PENDING"},
            {"id":"dep_runbook","label":"Runbook / SOP evidence set identified","status":"PENDING"},
        ]
        if stage_id == "NEXT_30":
            return base[:3]
        if stage_id == "NEXT_60":
            return base
        return base + [
            {"id":"dep_audit","label":"Audit & compliance sign-off","status":"PENDING"},
            {"id":"dep_prod","label":"Production readiness gate","status":"MISSING"},
        ]

    def mk_stage(stage_id: str, title: str, summary: str, stage_opps: List[Dict[str, Any]]):
        perms = uniq_by_label([p for o in stage_opps for p in o.get("permissions", [])])
        return {
            "id": stage_id,
            "title": title,
            "summary": summary,
            "opportunities": stage_opps,
            "requiredPermissions": perms,
            "dependencies": mk_deps(stage_id),
        }

    stages = [
        mk_stage("NEXT_30","Next 30 Days","Prove value fast with low-effort quick wins; establish access + governance.", stage30),
        mk_stage("NEXT_60","Next 60 Days","Expand evidence coverage and scale adoption across one shared process.", stage60),
        mk_stage("NEXT_90","Next 90 Days","Production-hardening and long-term automation with audit-ready controls.", stage90),
    ]

    all_perms = uniq_by_label([p for s in stages for p in s["requiredPermissions"]])
    required_perms = [p for p in all_perms if p.get("required")]
    return {
        "selectedOpportunityCount": len(selected),
        "requiredPermissionsCount": len(required_perms),
        "dependencyCount": sum(len(s["dependencies"]) for s in stages),
        "overallReadiness": overall_readiness(all_perms),
        "stages": stages,
    }
