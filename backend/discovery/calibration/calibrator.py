"""
SF-3.2 — Threshold & Score Calibrator

Purpose: compare algorithm output against an independent architect assessment,
identify systematic bias, and produce documented adjustment recommendations.

Calibration gate (from Sprint 3 doc):
  - At least 3 of top 5 algorithm opportunities overlap with architect's top 5
  - None of the algorithm's top 5 are rated "not real" by the architect
  - Impact/Effort direction correct for all agreed opportunities

Allowed adjustments (from Sprint 3 doc):
  - Detector thresholds (detectors/*.py)
  - Scorer factor weights and pts bands (scorer.py)
  - Confidence mapping rules (scorer.py)

NOT allowed without scope change:
  - New detectors
  - Ingestion logic rewrites
  - Track A contract changes

Usage:
    python -m backend.discovery.calibration.calibrator \\
        --run-output   runs/sf31_live_output.json \\
        --architect    runs/architect_assessment.json \\
        --report-path  runs/sf32_calibration_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Architect assessment schema
#
# The architect fills in a JSON file (see runs/architect_assessment_template.json)
# with their independent view of the org's top automation opportunities.
# ─────────────────────────────────────────────────────────────────────────────

ARCHITECT_TEMPLATE = {
    "_instructions": (
        "Fill this file with your independent assessment of the org's "
        "automation opportunities BEFORE reading the algorithm output. "
        "List your top 5 in priority order. "
        "For each: write a short label, your effort/impact estimate (1-10), "
        "and whether you consider it real and worth doing."
    ),
    "assessor": "name of architect",
    "org_id": "demo-org",
    "assessment_date": "YYYY-MM-DD",
    "top_5": [
        {
            "rank": 1,
            "label": "Short description of opportunity (e.g. 'Case routing is manual')",
            "detector_match": "HANDOFF_FRICTION",  # best-match detector ID or null
            "architect_impact": 8,                 # 1-10
            "architect_effort": 3,                 # 1-10
            "is_real": True,                       # would you actually pilot this?
            "notes": "optional free text"
        },
    ]
}


# ─────────────────────────────────────────────────────────────────────────────
# Calibration gate evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_overlap(
    algo_top5: List[Dict],
    arch_top5: List[Dict],
) -> Tuple[int, List[str], List[str]]:
    """
    Compare algorithm top 5 vs architect top 5.

    Returns:
        overlap_count   int    — how many detector_match IDs appear in both
        matched_ids     list   — detector IDs that matched
        unmatched_ids   list   — algorithm top 5 not in architect list
    """
    algo_ids = [o.get("detector_id", "") for o in algo_top5]
    arch_ids = [a.get("detector_match", "") for a in arch_top5 if a.get("detector_match")]

    matched   = [aid for aid in algo_ids if aid in arch_ids]
    unmatched = [aid for aid in algo_ids if aid not in arch_ids]
    return len(matched), matched, unmatched


def check_false_positives(
    algo_top5: List[Dict],
    arch_top5: List[Dict],
    all_algo_opps: Optional[List[Dict]] = None,
) -> Tuple[List[str], List[str]]:
    """
    Return (gate_fps, review_later_fps).

    Scope rule (explicit):
        gate_fps      — FPs in the algo's ranked top 5 ONLY.
                        These BLOCK the calibration gate. If the architect
                        says a top-5 item is not real, the gate fails.
        review_later_fps — FPs in fired opportunities outside the top 5.
                        These are logged for future calibration but do NOT
                        block the sprint gate. A bad rank-6 item does not
                        invalidate an excellent top 5.

    This is Option A from the Sprint 3 review: FP gate applies to Top-5 only.
    Anything outside Top-5 is logged as "review later".
    """
    false_positive_detectors = {
        a["detector_match"]
        for a in arch_top5
        if not a.get("is_real", True) and a.get("detector_match")
    }
    top5_ids = {o.get("detector_id", "") for o in algo_top5}

    gate_fps        = [did for did in false_positive_detectors if did in top5_ids]
    review_later_fps = []
    if all_algo_opps:
        non_top5_ids = {o.get("detector_id", "") for o in all_algo_opps} - top5_ids
        review_later_fps = [did for did in false_positive_detectors if did in non_top5_ids]

    return gate_fps, review_later_fps


def check_direction(
    algo_top5: List[Dict],
    arch_top5: List[Dict],
) -> List[Dict]:
    """
    For opportunities both teams agree on, compare Impact/Effort direction.
    Returns list of discrepancies.
    """
    arch_by_detector = {
        a["detector_match"]: a
        for a in arch_top5
        if a.get("detector_match")
    }
    discrepancies = []
    for opp in algo_top5:
        did = opp["detector_id"]
        if did not in arch_by_detector:
            continue
        arch = arch_by_detector[did]
        algo_impact = opp.get("impact", 0)
        algo_effort = opp.get("effort", 0)
        arch_impact = arch.get("architect_impact", 0)
        arch_effort = arch.get("architect_effort", 0)

        # Direction check: is the algo's relative ordering consistent?
        # High-impact algo item should not be low-impact per architect (>3 pt gap)
        # Deterministic direction metric (Must-fix 3):
        #   abs(impact_algo - impact_arch) <= 3
        #   AND abs(effort_algo - effort_arch) <= 3
        # Both conditions must hold. Either violation is a discrepancy.
        # severity: HIGH if either gap > 4, else MEDIUM.
        impact_gap = abs(algo_impact - arch_impact)
        effort_gap = abs(algo_effort - arch_effort)

        if impact_gap > 3 or effort_gap > 3:
            discrepancies.append({
                "detector_id":        did,
                "algo_impact":        algo_impact,
                "arch_impact":        arch_impact,
                "impact_gap":         impact_gap,
                "impact_gap_ok":      impact_gap <= 3,
                "algo_effort":        algo_effort,
                "arch_effort":        arch_effort,
                "effort_gap":         effort_gap,
                "effort_gap_ok":      effort_gap <= 3,
                "direction_formula":  (
                    "abs(impact_algo - impact_arch) <= 3 "
                    "AND abs(effort_algo - effort_arch) <= 3"
                ),
                "severity":           "HIGH" if max(impact_gap, effort_gap) > 4 else "MEDIUM",
            })
    return discrepancies


# ─────────────────────────────────────────────────────────────────────────────
# Adjustment recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_impact_bias(algo_opps: List[Dict]) -> Optional[str]:
    """
    Detect systematic impact under/over-scoring.
    Returns a recommendation string or None.
    """
    impacts = [o.get("impact", 0) for o in algo_opps]
    if not impacts:
        return None
    avg_impact = sum(impacts) / len(impacts)

    if avg_impact < 4.0:
        return (
            f"Systematic low impact bias detected (avg={avg_impact:.1f}). "
            "All opportunities score ≤ 4. Review _volume_pts() bands — the "
            "current fixture org has ~300 cases/90d = ~23/week which maps to "
            "volume_pts=2.0 (the floor). Consider lowering the volume_pts "
            "threshold bands so that 20-50 weekly records scores 4-5 pts "
            "rather than 2.0. Also review _W_VOLUME weight (currently 0.30) — "
            "if volume dominates and most orgs have moderate volume, the "
            "resulting impact will always cluster low."
        )
    if avg_impact > 8.0:
        return (
            f"Systematic high impact bias detected (avg={avg_impact:.1f}). "
            "Review friction_pts and customer_pts bands for inflation."
        )
    return None


def _analyse_threshold_coverage(algo_opps: List[Dict]) -> List[str]:
    """
    Check which detectors have proxy_ratio very close to threshold (1.0-1.2).
    These are borderline fires — small threshold adjustment would drop them.
    """
    borderline = []
    for opp in algo_opps:
        ratio = opp.get("score_debug", {}).get("proxy_ratio", 0)
        if 1.0 <= ratio <= 1.2:
            borderline.append(
                f"{opp['detector_id']}: proxy_ratio={ratio:.3f} — "
                f"borderline fire. Validate this is a genuine signal in the live org "
                f"before SF-3.2 calibration session. May need threshold +10%."
            )
    return borderline


def _build_org_readiness_checklist(inputs: Dict[str, Any]) -> List[Dict]:
    """
    Good-to-have B: measurable org state with one-liner verification queries.
    Checks inputs dict (from runner org_context) for known signals.
    Each item has: check, current_value, minimum, soql, status.
    """
    total_cases   = inputs.get("sf_total_cases_90d", 0)
    owner_changes = inputs.get("sf_owner_changes_90d", 0)
    active_flows  = inputs.get("sf_active_flows", 0)
    pending_approvals = inputs.get("sf_pending_approvals", 0)
    named_creds   = inputs.get("sf_named_credentials", 0)

    def _status(current, minimum):
        if current == 0:     return "❌ missing"
        if current < minimum: return "⚠  low"
        return "✅ ok"

    return [
        {
            "check":         "Cases in last 90 days",
            "current_value": total_cases,
            "minimum":       50,
            "status":        _status(total_cases, 50),
            "soql":          "SELECT COUNT(Id) FROM Case WHERE CreatedDate = LAST_N_DAYS:90",
        },
        {
            "check":         "CaseHistory owner-change records",
            "current_value": owner_changes,
            "minimum":       20,
            "status":        _status(owner_changes, 20),
            "soql":          "SELECT COUNT(Id) FROM CaseHistory WHERE Field='Owner' AND CreatedDate = LAST_N_DAYS:90",
        },
        {
            "check":         "Active AutoLaunchedFlows on Case",
            "current_value": active_flows,
            "minimum":       2,
            "status":        _status(active_flows, 2),
            "soql":          "SELECT COUNT() FROM FlowVersionView WHERE Status='Active' AND ProcessType='AutoLaunchedFlow' LIMIT 200  [Tooling API]",
        },
        {
            "check":         "Pending approval records",
            "current_value": pending_approvals,
            "minimum":       5,
            "status":        _status(pending_approvals, 5),
            "soql":          "SELECT COUNT(Id) FROM ProcessInstance WHERE Status='Pending'",
        },
        {
            "check":         "Named Credentials",
            "current_value": named_creds,
            "minimum":       1,
            "status":        _status(named_creds, 1),
            "soql":          "SELECT DeveloperName FROM NamedCredential LIMIT 20  [Tooling API]",
        },
    ]


def _build_summary_table(
    algo_top5: List[Dict],
    arch_top5: List[Dict],
) -> List[Dict]:
    """
    Good-to-have A: one-glance comparison table for calibration meetings.
    Returns a list of rows with algo and architect data side by side.
    """
    arch_by_detector = {
        a.get("detector_match", ""): a for a in arch_top5
    }
    rows = []
    for i, opp in enumerate(algo_top5):
        did = opp.get("detector_id", "")
        arch = arch_by_detector.get(did, {})
        algo_net = opp.get("impact", 0) - opp.get("effort", 0)
        arch_impact = arch.get("architect_impact")
        arch_effort = arch.get("architect_effort")
        arch_net = (arch_impact - arch_effort) if (arch_impact and arch_effort) else None
        rows.append({
            "rank":           i + 1,
            "detector_id":    did,
            "algo_impact":    opp.get("impact"),
            "algo_effort":    opp.get("effort"),
            "algo_net":       algo_net,
            "algo_confidence":opp.get("confidence"),
            "algo_tier":      opp.get("tier"),
            "arch_label":     arch.get("label", ""),
            "arch_impact":    arch_impact,
            "arch_effort":    arch_effort,
            "arch_net":       arch_net,
            "arch_is_real":   arch.get("is_real"),
            "impact_delta":   abs(opp.get("impact", 0) - arch_impact) if arch_impact else None,
            "effort_delta":   abs(opp.get("effort", 0) - arch_effort) if arch_effort else None,
            "matched":        bool(arch),
        })
    return rows


def generate_recommendations(
    algo_opps: List[Dict],
    overlap_count: int,
    false_positives: List[str],
    direction_issues: List[Dict],
) -> List[Dict]:
    """
    Generate calibration adjustment recommendations with before/after values.
    Each recommendation is in the allowed scope from Sprint 3 doc.
    """
    recommendations = []

    # Impact bias
    bias_note = _analyse_impact_bias(algo_opps)
    if bias_note:
        recommendations.append({
            "type":        "scorer_weight",
            "severity":    "HIGH",
            "target":      "scorer.py _volume_pts() bands + _W_VOLUME",
            "observation": bias_note,
            "before":      {
                "_volume_pts_bands": {"<50/week": 2.0, "<200/week": 5.0,
                                      "<500/week": 7.0, ">=500/week": 9.0},
                "_W_VOLUME": 0.30,
            },
            "suggested_after": {
                "_volume_pts_bands": {"<20/week": 3.0, "<100/week": 5.5,
                                      "<300/week": 7.5, ">=300/week": 9.0},
                "_W_VOLUME": 0.35,
                "_W_FRICTION": 0.30,
            },
            "rationale": (
                "Lowering volume thresholds means a dev org with 20-30 cases/week "
                "scores 3.0 pts instead of 2.0. Most enterprise Salesforce orgs "
                "have 100-500 cases/week — these would score 5.5-7.5. "
                "This brings expected impact into the 4-7 range for typical orgs. "
                "OVERFIT GUARD: after adjusting, simulate a mid-size org "
                "(200 cases/week) through the fixture — confirm it scores 5-7 "
                "impact, not 9-10. Bands calibrated only for a dev org will "
                "inflate scores on real customer orgs and destroy credibility."
            ),
            "allowed": True,
        })

    # False positives
    # false_positives here is the gate_fps list (top-5 only)
    for fp in (false_positives or []):
        recommendations.append({
            "type":        "detector_threshold",
            "severity":    "HIGH",
            "target":      f"detectors/{fp.lower()}.py — threshold value",
            "observation": (
                f"{fp} fired but architect rated this as NOT real / not worth doing. "
                "This is a false positive. Raise the threshold so this org configuration "
                "does not trigger the detector."
            ),
            "before":      "current threshold (read from detectors file)",
            "suggested_after": "raise by 20-30% — document the specific live org values that prompted this",
            "rationale":   "Eliminating false positives is the highest-priority calibration action.",
            "allowed": True,
        })

    # Direction discrepancies
    for disc in direction_issues:
        did = disc["detector_id"]
        if disc["impact_gap"] > 3:
            recommendations.append({
                "type":        "scorer_factor",
                "severity":    disc["severity"],
                "target":      f"scorer.py _impact_factors() for {did}",
                "observation": (
                    f"{did}: algorithm impact={disc['algo_impact']}, "
                    f"architect says {disc['arch_impact']} (gap={disc['impact_gap']}). "
                    "Impact factor mapping does not match expert judgment."
                ),
                "before":      f"current _impact_factors({did}) mapping",
                "suggested_after": (
                    f"Review which of volume/friction/customer/revenue/external pts "
                    f"are under- or over-estimated for {did}. Adjust the relevant "
                    f"sub-function (e.g. _friction_pts_handoff) bands."
                ),
                "rationale": "Architect impact estimate is ground truth for calibration.",
                "allowed": True,
            })

    # Borderline thresholds
    borderline = _analyse_threshold_coverage(algo_opps)
    for note in borderline:
        recommendations.append({
            "type":        "detector_threshold",
            "severity":    "LOW",
            "target":      "threshold — see note",
            "observation": note,
            "before":      "current threshold",
            "suggested_after": "validate live org first; raise by 10% only if architect confirms not real",
            "rationale":   "Borderline fires should be validated before adjusting.",
            "allowed": True,
        })

    # PM-05 NC scan note (from SF-3.2 context)
    recommendations.append({
        "type":        "pm05_note",
        "severity":    "INFO",
        "target":      "INTEGRATION_CONCENTRATION detector + named_credential_flow_refs",
        "observation": (
            "PM-05 (Named Credential → Flow scan) may be empty even if Named "
            "Credentials exist. Flow metadata shape differs by Flow type and "
            "packaging. Treat D5 as 'may not fire in all orgs' until the exact "
            "metadata path(s) are confirmed in the target org. "
            "Do not adjust D5 threshold based on a single org observation."
        ),
        "before":      "no change",
        "suggested_after": "no change — document observed metadata path for SF-4.3",
        "rationale":   "SF-1.2 flagged this risk. Confirmed as known variance point.",
        "allowed": True,
    })

    return recommendations


# ─────────────────────────────────────────────────────────────────────────────
# Main calibration run
# ─────────────────────────────────────────────────────────────────────────────

def run_calibration(
    run_output: Dict[str, Any],
    architect_assessment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute the full SF-3.2 calibration analysis.

    Parameters
    ----------
    run_output          : dict from runner.run() or offline_export output
    architect_assessment: dict matching architect_assessment_template.json
                          If None, runs analysis-only mode (no overlap gate)

    Returns calibration report dict.
    """
    report_time = datetime.now(timezone.utc).isoformat()

    # Extract opportunities — handle both runner payload and track_a_seed format
    if "opportunities" in run_output and run_output["opportunities"]:
        first = run_output["opportunities"][0]
        if "detector_id" in first:
            # Internal runner format
            opps = run_output["opportunities"]
        elif "_debug" in first:
            # Track A seed format with _debug namespace
            opps = []
            for o in run_output["opportunities"]:
                d = dict(o)
                debug = d.pop("_debug", {})
                d.update(debug)
                opps.append(d)
        else:
            opps = run_output["opportunities"]
    else:
        opps = []

    # Production ranking — shared utility (SF-3.3). Single definition reused across
    # calibrator.py, track_a_adapter.py, and runner.py. See calibration/ranking.py.
    from .ranking import rank_opportunities
    algo_ranked = rank_opportunities(opps)
    algo_top5 = algo_ranked[:5]

    # Score debug summary
    score_debug_summary = []
    for opp in algo_ranked:
        sd = opp.get("score_debug", {})
        score_debug_summary.append({
            "detector_id":    opp.get("detector_id", ""),
            "impact":         opp.get("impact", 0),
            "effort":         opp.get("effort", 0),
            "confidence":     opp.get("confidence", ""),
            "tier":           opp.get("tier", ""),
            "proxy_ratio":    sd.get("proxy_ratio", 0),
            "impact_factors": sd.get("impact_factors", {}),
            "effort_factors": sd.get("effort_factors", {}),
        })

    # Impact bias analysis (always run)
    impact_bias = _analyse_impact_bias(algo_ranked)
    threshold_coverage = _analyse_threshold_coverage(algo_ranked)

    # Architect comparison (only if assessment provided)
    overlap_count   = 0
    matched_ids     = []
    unmatched_ids   = []
    false_positives = []
    direction_issues= []
    gate_passed     = None
    arch_top5       = []

    if architect_assessment:
        arch_top5 = architect_assessment.get("top_5", [])
        overlap_count, matched_ids, unmatched_ids = evaluate_overlap(algo_top5, arch_top5)
        false_positives, review_later_fps = check_false_positives(
            algo_top5, arch_top5, algo_ranked
        )
        direction_issues = check_direction(algo_top5, arch_top5)

        # Calibration gate:
        #   overlap >= 3 AND zero FALSE POSITIVES IN TOP 5 (gate_fps only)
        #   review_later_fps are logged but do not block the gate
        gate_passed = (overlap_count >= 3) and (len(false_positives) == 0)

    recommendations = generate_recommendations(
        algo_ranked, overlap_count, false_positives, direction_issues
    )

    # Good-to-have A: one-glance calibration summary table
    summary_table = _build_summary_table(algo_top5, arch_top5 if architect_assessment else [])

    # Good-to-have B: minimum org state checklist with verification queries
    org_readiness_checklist = _build_org_readiness_checklist(run_output.get("inputs", {}))

    return {
        "sf32_gate_passed":     gate_passed,    # None if no architect assessment
        "report_time":          report_time,
        "org_id":               run_output.get("orgId", run_output.get("org_id", "")),
        "run_id":               run_output.get("runId", run_output.get("run_id", "")),
        "mode":                 run_output.get("mode", ""),
        "calibration_summary_table": summary_table,
        "org_readiness_checklist":   org_readiness_checklist,
        "algo_top5": [
            {
                "rank":       i + 1,
                "detector_id":o.get("detector_id", ""),
                "impact":     o.get("impact", 0),
                "effort":     o.get("effort", 0),
                "confidence": o.get("confidence", ""),
                "tier":       o.get("tier", ""),
            }
            for i, o in enumerate(algo_top5)
        ],
        "score_debug_summary":  score_debug_summary,
        "calibration_gate": {
            "overlap_count":         overlap_count,
            "matched_ids":           matched_ids,
            "unmatched_ids":         unmatched_ids,
            "false_positives_top5":  false_positives,     # gate-blocking
            "false_positives_other": review_later_fps if architect_assessment else [],
            "direction_issues":      direction_issues,
            "gate_passed":           gate_passed,
            "gate_rule": (
                "3 of top 5 overlap "
                "AND zero FPs in algo top 5 (FPs outside top 5 are logged, not blocking). "
                "Direction: abs(impact_delta) <= 3 AND abs(effort_delta) <= 3."
            ),
            "ranking_function": (
                "tier_order (Quick Win=1, Strategic=2, Complex=3), "
                "then (impact - effort) desc, then effort asc"
            ),
        },
        "impact_bias_note":     impact_bias,
        "borderline_thresholds":threshold_coverage,
        "recommendations":      recommendations,
        "adjustments_log":      [],   # filled by team during calibration session
        "summary":              _build_summary(
            gate_passed, overlap_count, false_positives,
            direction_issues, recommendations, architect_assessment is not None
        ),
    }


def _build_summary(
    gate_passed, overlap_count, false_positives,
    direction_issues, recommendations, has_architect
) -> str:
    if not has_architect:
        return (
            "SF-3.2 analysis-only mode (no architect assessment provided). "
            f"{len(recommendations)} recommendation(s) generated from score_debug analysis. "
            "Provide architect_assessment.json to run the full calibration gate."
        )
    status = "PASSED" if gate_passed else "FAILED"
    return (
        f"SF-3.2 calibration gate {status} | "
        f"Overlap: {overlap_count}/5 | "
        f"False positives: {len(false_positives)} | "
        f"Direction issues: {len(direction_issues)} | "
        f"Recommendations: {len(recommendations)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="SF-3.2 Threshold Calibrator — AgentIQ discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Workflow:\n"
            "  1. Run SF-3.1 validator: python -m backend.discovery.ingest.live_validator\n"
            "  2. Run algorithm:        python -m backend.discovery.runner --mode live "
            "--output runs/live_output.json\n"
            "  3. Architect fills:      runs/architect_assessment.json\n"
            "  4. Run calibration:      python -m backend.discovery.calibration.calibrator "
            "--run-output runs/live_output.json --architect runs/architect_assessment.json\n"
            "  5. Review report and apply allowed adjustments to scorer.py / detectors"
        ),
    )
    parser.add_argument(
        "--run-output", required=True,
        help="Path to JSON output from runner.py (--output flag)"
    )
    parser.add_argument(
        "--architect", default=None,
        help="Path to architect_assessment.json (optional — if omitted runs analysis-only)"
    )
    parser.add_argument(
        "--report-path", default=None,
        help="Write calibration report JSON to this path"
    )
    parser.add_argument(
        "--write-template", default=None,
        help="Write architect_assessment_template.json to this path and exit"
    )
    args = parser.parse_args()

    if args.write_template:
        Path(args.write_template).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write_template).write_text(
            json.dumps(ARCHITECT_TEMPLATE, indent=2), encoding="utf-8"
        )
        print(f"Template written to {args.write_template}")
        print("Share with the architect BEFORE running the algorithm against their org.")
        return 0

    run_output_path = Path(args.run_output)
    if not run_output_path.exists():
        print(f"ERROR: run output file not found: {args.run_output}")
        return 1

    run_output = json.loads(run_output_path.read_text(encoding="utf-8"))

    architect_assessment = None
    if args.architect:
        arch_path = Path(args.architect)
        if not arch_path.exists():
            print(f"ERROR: architect assessment file not found: {args.architect}")
            return 1
        architect_assessment = json.loads(arch_path.read_text(encoding="utf-8"))

    report = run_calibration(run_output, architect_assessment)

    print()
    print("=" * 65)
    print(report["summary"])
    print("=" * 65)

    print()
    print("Algorithm top 5:")
    for item in report["algo_top5"]:
        print(
            f"  {item['rank']}. {item['detector_id']:30s} "
            f"impact={item['impact']} effort={item['effort']} "
            f"{item['confidence']:6s} {item['tier']}"
        )

    print()
    print("Score debug (impact raw_sum):")
    for item in report["score_debug_summary"]:
        raw = item.get("impact_factors", {}).get("raw_sum", 0)
        ratio = item.get("proxy_ratio", 0)
        print(
            f"  {item['detector_id']:30s} "
            f"raw_sum={raw:.2f} → impact={item['impact']} "
            f"proxy_ratio={ratio:.3f}"
        )

    if report["impact_bias_note"]:
        print()
        print("⚠  Impact bias detected:")
        print(f"   {report['impact_bias_note'][:120]}...")

    if report["recommendations"]:
        print()
        print(f"Recommendations ({len(report['recommendations'])}):")
        for i, rec in enumerate(report["recommendations"], 1):
            sev_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "ℹ"}.get(rec["severity"], "?")
            print(f"  {i}. {sev_icon} [{rec['type']}] {rec['target']}")

    if args.report_path:
        Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_path).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(f"\nCalibration report written to {args.report_path}")

    return 0 if (report["sf32_gate_passed"] is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
