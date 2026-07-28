"""
Sprint 4 T6 — Executive Report Engine

Builds the executive report from run-scoped opportunities and roadmap.
This module was imported by materialize_t2.py but never existed.
T6 creates it so the import succeeds and the fallback block is never hit.

The executive report shape matches what the frontend ExecutiveReportPage
expects — confirmed from frontend/src/pages/ExecutiveReportPage.tsx.
2.0-A1 T5: the report is a projection surface, so everything it emits passes the
projection vocabulary guard. The executive summary and every quick-win narrative
field are scrubbed of point-estimate savings claims and guarantee language before
they leave this module — AC3 covers "API, UI, report, or export", and this is the
report.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


#: Narrative fields on an opportunity that reach the executive report and its
#: PDF export. Measured fields (impact, effort, evidence ids) are untouched —
#: the guard is about claims, not about numbers.
_NARRATIVE_FIELDS = ("title", "aiRationale", "aiSummary")


def _scrub_opportunity_narrative(opp: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``opp`` with projection claims stripped from its prose.

    Copied rather than mutated: the report must not rewrite the stored
    opportunity a run persisted, or a replay would serve different text than the
    run produced.
    """
    from discovery.projection.vocabulary import contains_prohibited, sanitize_text

    if not isinstance(opp, dict):
        return opp
    if not any(contains_prohibited(opp.get(f)) for f in _NARRATIVE_FIELDS):
        return opp

    cleaned = dict(opp)
    for field in _NARRATIVE_FIELDS:
        value = cleaned.get(field)
        if isinstance(value, str) and value:
            cleaned[field] = sanitize_text(value)
    return cleaned


def scrub_executive_summary(summary: Optional[str]) -> str:
    """Strip projection claims from the executive summary paragraph.

    Exported because the summary is written by the enrichment layer AFTER this
    engine builds the report shape (``aiExecutiveSummary`` starts empty here),
    so whoever fills it in must run it through the same guard.
    """
    from discovery.projection.vocabulary import sanitize_text

    return sanitize_text(summary or "")


def build_executive_report(
    run_id: str,
    opps: List[Dict[str, Any]],
    roadmap: Dict[str, Any],
    selected_system_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build executive report from run-scoped data.
    """

    opps = [_scrub_opportunity_narrative(o) for o in opps]

    quick_wins  = [o for o in opps if o.get("tier") == "Quick Win"]
    strategic   = [o for o in opps if o.get("tier") == "Strategic"]
    complex_    = [o for o in opps if o.get("tier") == "Complex"]

    high_count   = sum(1 for o in opps if o.get("confidence") == "HIGH")
    medium_count = sum(1 for o in opps if o.get("confidence") == "MEDIUM")

    if high_count >= 2:
        confidence = "High"
    elif high_count >= 1 or medium_count >= 3:
        confidence = "Moderate"
    else:
        confidence = "Low"

    next_30 = roadmap.get("NEXT_30", [])
    next_60 = roadmap.get("NEXT_60", [])
    next_90 = roadmap.get("NEXT_90", [])

    # ✅ FIX APPLIED HERE (MANDATORY FOR CONTRACT TEST)
    snapshot_bubbles = []

    return {
        "confidence": confidence,
        "sourcesAnalyzed": {
            "recommendedConnected": 0,
            "totalConnected": len(selected_system_ids or []),
            "uploadedFiles": 0,
            "sampleWorkspaceEnabled": False,
        },
        "topQuickWins": quick_wins[:3],

        # ✅ must always be empty per contract test
        "snapshotBubbles": snapshot_bubbles,

        "roadmapHighlights": {
            "next30Count": len(next_30) or len(quick_wins),
            "next60Count": len(next_60) or len(strategic),
            "next90Count": len(next_90) or len(complex_),
            "blockerCount": 0,
        },

        # T6 enrichment layer adds this later
        "aiExecutiveSummary": "",
    }
