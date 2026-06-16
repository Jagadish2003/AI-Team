from __future__ import annotations

from typing import Any, Dict, List

from .roadmap_engine import overall_readiness, uniq_permissions_merge


OPPORTUNITY_TITLE_OVERRIDES = {
    "APPLICATION_STALL": "Retirement Application Monitor",
    "BENEFIT_ELECTION_DEADLINE": "Benefit Election Guardian",
}

LEGACY_OPPORTUNITY_TITLE_OVERRIDES = {
    "Application Stall": "Retirement Application Monitor",
    "Benefit Election Deadline": "Benefit Election Guardian",
}


def with_display_title(opp: Dict[str, Any]) -> Dict[str, Any]:
    display_opp = dict(opp)
    debug = display_opp.get("_debug") or {}
    detector_id = str(debug.get("detector_id") or display_opp.get("detector_id") or "")
    title = str(display_opp.get("title") or "")
    display_title = (
        OPPORTUNITY_TITLE_OVERRIDES.get(detector_id)
        or LEGACY_OPPORTUNITY_TITLE_OVERRIDES.get(title)
    )
    if display_title:
        display_opp["title"] = display_title
    return display_opp


def with_display_titles(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [with_display_title(opp) for opp in opps]


def with_roadmap_display_titles(roadmap: Dict[str, Any]) -> Dict[str, Any]:
    display_roadmap = dict(roadmap)
    stages = display_roadmap.get("stages")
    if isinstance(stages, list):
        normalized_stages = []
        for stage in stages:
            if not isinstance(stage, dict):
                normalized_stages.append(stage)
                continue

            labels = []
            for permission in stage.get("requiredPermissions") or []:
                if isinstance(permission, dict):
                    label = str(permission.get("label") or "").strip()
                    if label:
                        labels.append(label)
                elif isinstance(permission, str):
                    label = permission.strip()
                    if label:
                        labels.append(label)

            normalized_stages.append(
                {
                    **stage,
                    "opportunities": with_display_titles(stage.get("opportunities") or []),
                    "requiredPermissions": uniq_permissions_merge(labels),
                }
            )

        display_roadmap["stages"] = normalized_stages
        all_perms = uniq_permissions_merge(
            [
                permission.get("label", "")
                for stage in normalized_stages
                if isinstance(stage, dict)
                for permission in stage.get("requiredPermissions") or []
                if isinstance(permission, dict)
            ]
        )
        display_roadmap["permissionsRequiredCount"] = sum(
            1 for permission in all_perms if permission.get("required")
        )
        display_roadmap["overallReadiness"] = overall_readiness(all_perms)
    return display_roadmap


def with_exec_report_display_titles(report: Dict[str, Any]) -> Dict[str, Any]:
    display_report = dict(report)
    confidence = display_report.get("confidence")
    if isinstance(confidence, str):
        canonical_confidence = {
            "high": "High",
            "moderate": "Moderate",
            "low": "Low",
        }.get(confidence.strip().lower())
        if canonical_confidence:
            display_report["confidence"] = canonical_confidence
    for field in ("topQuickWins", "snapshotBubbles"):
        items = display_report.get(field)
        if isinstance(items, list):
            display_report[field] = with_display_titles(items)
    return display_report
