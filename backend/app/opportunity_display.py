from __future__ import annotations

from typing import Any, Dict, List


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
        display_roadmap["stages"] = [
            {
                **stage,
                "opportunities": with_display_titles(stage.get("opportunities") or []),
            }
            if isinstance(stage, dict)
            else stage
            for stage in stages
        ]
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
