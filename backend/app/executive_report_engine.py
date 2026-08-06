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


from .projection_copy_guard import (  # noqa: F401  (re-exported)
    scrub_executive_summary,
    scrub_opportunity_narrative as _scrub_opportunity_narrative,
    scrub_opportunity_narratives,
)
from .outcome_surfaces import build_empty_outcome_report_section


def build_executive_report(
    run_id: str,
    opps: List[Dict[str, Any]],
    roadmap: Dict[str, Any],
    selected_system_ids: Optional[List[str]] = None,
    outcome_section: Optional[Dict[str, Any]] = None,
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
        "outcomeSection": outcome_section or build_empty_outcome_report_section(run_id),
    }
