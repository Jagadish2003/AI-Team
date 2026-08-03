"""
Sprint 4 T6 — Executive Report Engine

Builds the executive report from run-scoped opportunities and roadmap.
This module was imported by materialize_t2.py but never existed.
T6 creates it so the import succeeds and the fallback block is never hit.

The executive report shape matches what the frontend ExecutiveReportPage
expects — confirmed from frontend/src/pages/ExecutiveReportPage.tsx.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_executive_report(
    run_id: str,
    opps: List[Dict[str, Any]],
    roadmap: Dict[str, Any],
    selected_system_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build executive report from run-scoped data.
    """

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

        # 2.0-C2 T3 (AT-833 / AC2): which LEVEL of pack produced the claims in this
        # report. A board paper quoting a finding has to be able to say whether it
        # came from a CloudFulcrum-certified pack, a partner pack, or a community
        # one — so the export states it rather than leaving the reader to look it up.
        "packCertifications": pack_certifications(opps),

        # T6 enrichment layer adds this later
        "aiExecutiveSummary": "",
    }


def pack_certifications(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Certification badge for every pack that contributed a finding to this report.

    Order follows first appearance in the findings, so the pack behind the leading
    claim is listed first. Levels are the EFFECTIVE (signature-verified) ones — a
    pack whose claim cannot be proved is reported as Community here exactly as it is
    everywhere else (2.0-C2 AC1), because an export is the surface where an
    unverified badge would do the most damage.

    Frozen into the report artifact at generation time, which is the honest reading
    for an export: it states what was verifiable when the report was produced.

    Fail-soft — a report must still generate if certification cannot be resolved. It
    then carries no badges rather than unproved ones.
    """
    pack_ids: List[str] = []
    for opp in opps:
        pack_id = str((opp or {}).get("packId") or "").strip()
        if pack_id and pack_id not in pack_ids:
            pack_ids.append(pack_id)
    if not pack_ids:
        return []
    try:
        from discovery.packs.pack_certification import certification_badges

        badges = certification_badges(pack_ids)
    except Exception:  # noqa: BLE001
        return []
    return [badges[pack_id] for pack_id in pack_ids if pack_id in badges]
