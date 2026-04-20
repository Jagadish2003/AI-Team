"""
SF-3.3 — Demo Integration Verifier

Scope: Export → Seed → Contract Verification.
This is NOT run-triggered integration (Sprint 4).
This verifies that Track B JSON output is schema-compatible with
Track A and renders correctly across all 10 screens.

Three-command path:
    1. python -m backend.discovery.offline_export
    2. python -m backend.discovery.integration_verifier --seed-dir backend/seed
    3. pytest backend/discovery/tests/test_sf33_integration_verifier.py -v

What this checks:
    File existence and parseability
    OpportunityCandidate schema (all required fields, correct types, enum casing)
    EvidenceReview schema (all required fields)
    Evidence linkage (every evidenceId resolves to an evidence object with entities)
    Confidence badge colours (HIGH/MEDIUM/LOW uppercase — not High/Medium/Low)
    Tier → roadmap stage mapping (Quick Win→NEXT_30, Strategic→NEXT_60, Complex→NEXT_90)
    Tier bucketing in S9 (Quick Wins land in NEXT_30, etc.)
    S10 executive report fields (sourcesAnalyzed, topQuickWins)
    decision=UNREVIEWED on all seeded opportunities
    override structure present on every opportunity

S1→S10 manual walkthrough checklist is in SMOKE_DEMO_SF33.md.

Notes carried from SF-3.2 review (applied here):
    Gate 3 direction: OR not AND — impact_gap > 3 OR effort_gap > 3 is a fail
    Top-3 exact match is a stretch KPI, not a blocking gate
    Ranking function is now a shared utility (calibration/ranking.py)
    Volume band adjustment blocked until org_readiness_checklist all green
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
# Track A contract — required fields per shape
# ─────────────────────────────────────────────────────────────────────────────

OPP_REQUIRED_FIELDS = [
    "id", "title", "category", "tier", "decision",
    "impact", "effort", "confidence", "aiRationale",
    "evidenceIds", "requiredPermissions", "override",
]
OPP_ENUM_CHECKS = {
    "tier":       {"Quick Win", "Strategic", "Complex"},
    "decision":   {"UNREVIEWED", "APPROVED", "REJECTED", "MANUAL_REVIEW"},
    "confidence": {"HIGH", "MEDIUM", "LOW"},
}
OPP_TYPE_CHECKS = {
    "id":                 str,
    "title":              str,
    "category":           str,
    "impact":             int,
    "effort":             int,
    "aiRationale":        str,
    "evidenceIds":        list,
    "requiredPermissions":list,
}
OVERRIDE_REQUIRED_KEYS = ["isLocked", "rationaleOverride", "overrideReason", "updatedAt"]

EV_REQUIRED_FIELDS = [
    "id", "tsLabel", "source", "evidenceType",
    "title", "snippet", "entities", "confidence", "decision",
]
EV_ENUM_CHECKS = {
    "confidence": {"HIGH", "MEDIUM", "LOW"},
    "decision":   {"UNREVIEWED", "APPROVED", "REJECTED"},
    "evidenceType":{"Metric", "Log", "Document", "Survey"},
}

TIER_TO_ROADMAP = {
    "Quick Win": "NEXT_30",
    "Strategic": "NEXT_60",
    "Complex":   "NEXT_90",
}

CONFIDENCE_COLOURS = {
    "HIGH":   "green",
    "MEDIUM": "amber",
    "LOW":    "red",
}


# ─────────────────────────────────────────────────────────────────────────────
# Check result accumulator
# ─────────────────────────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self, name: str):
        self.name   = name
        self.passed = True
        self.issues: List[str] = []
        self.warnings: List[str] = []

    def fail(self, msg: str):
        self.passed = False
        self.issues.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check":    self.name,
            "passed":   self.passed,
            "issues":   self.issues,
            "warnings": self.warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def check_file_existence(seed_dir: Path) -> CheckResult:
    r = CheckResult("file_existence")
    for fname in ("opportunities.json", "evidence.json"):
        fpath = seed_dir / fname
        if not fpath.exists():
            r.fail(f"{fname} not found in {seed_dir}")
        else:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    r.fail(f"{fname}: expected JSON array, got {type(data).__name__}")
                elif len(data) == 0:
                    r.warn(f"{fname}: empty array — run offline_export first")
            except json.JSONDecodeError as e:
                r.fail(f"{fname}: JSON parse error — {e}")
    return r


def check_opportunity_schema(opps: List[Dict]) -> CheckResult:
    r = CheckResult("opportunity_schema")
    for i, opp in enumerate(opps):
        oid = opp.get("id", f"[index {i}]")

        # Required fields
        for field in OPP_REQUIRED_FIELDS:
            if field not in opp:
                r.fail(f"{oid}: missing required field '{field}'")

        # Type checks
        for field, expected_type in OPP_TYPE_CHECKS.items():
            if field in opp and not isinstance(opp[field], expected_type):
                r.fail(f"{oid}: '{field}' expected {expected_type.__name__}, "
                       f"got {type(opp[field]).__name__}")

        # Enum checks
        for field, valid_values in OPP_ENUM_CHECKS.items():
            if field in opp and opp[field] not in valid_values:
                r.fail(f"{oid}: '{field}' = '{opp[field]}' not in {valid_values}. "
                       f"Check casing — must be uppercase (HIGH not High).")

        # Impact/effort range
        for score_field in ("impact", "effort"):
            if score_field in opp:
                val = opp[score_field]
                if isinstance(val, int) and not (1 <= val <= 10):
                    r.fail(f"{oid}: '{score_field}' = {val} out of range [1, 10]")

        # Override structure
        if "override" in opp:
            ov = opp["override"]
            if not isinstance(ov, dict):
                r.fail(f"{oid}: 'override' must be a dict")
            else:
                for key in OVERRIDE_REQUIRED_KEYS:
                    if key not in ov:
                        r.fail(f"{oid}: override missing key '{key}'")

        # decision must be UNREVIEWED from algorithm
        if opp.get("decision") != "UNREVIEWED":
            r.fail(f"{oid}: decision = '{opp.get('decision')}' — "
                   "algorithm output must always be UNREVIEWED")

        # aiRationale must be non-empty
        if not opp.get("aiRationale", "").strip():
            r.fail(f"{oid}: aiRationale is empty")

    return r


def check_evidence_schema(evs: List[Dict]) -> CheckResult:
    r = CheckResult("evidence_schema")
    for i, ev in enumerate(evs):
        eid = ev.get("id", f"[index {i}]")

        for field in EV_REQUIRED_FIELDS:
            if field not in ev:
                r.fail(f"{eid}: missing required field '{field}'")

        for field, valid_values in EV_ENUM_CHECKS.items():
            if field in ev and ev[field] not in valid_values:
                r.fail(f"{eid}: '{field}' = '{ev[field]}' not in {valid_values}")

        # entities must be a list
        if "entities" in ev and not isinstance(ev["entities"], list):
            r.fail(f"{eid}: 'entities' must be a list")

        # tsLabel must be non-empty
        if not ev.get("tsLabel", "").strip():
            r.warn(f"{eid}: tsLabel is empty — S4 card timestamp will be blank")

        # snippet must contain at least one number (from SF-1.5 evidence schema rule)
        snippet = ev.get("snippet", "")
        has_number = any(c.isdigit() for c in snippet)
        if not has_number:
            r.warn(f"{eid}: snippet contains no numeric value — "
                   "evidence should reference measured data")

    return r


def check_evidence_linkage(opps: List[Dict], evs: List[Dict]) -> CheckResult:
    """
    Critical check: every evidenceId in opportunities.json must resolve
    to a visible evidence object with entities populated.
    This is the most common integration break.
    """
    r = CheckResult("evidence_linkage")
    ev_map = {e["id"]: e for e in evs if "id" in e}

    for opp in opps:
        oid = opp.get("id", "?")
        evidence_ids = opp.get("evidenceIds", [])

        if not evidence_ids:
            r.warn(f"{oid}: evidenceIds is empty — S6 evidence panel will be blank")
            continue

        for eid in evidence_ids:
            if eid not in ev_map:
                r.fail(
                    f"{oid}: evidenceId '{eid}' not found in evidence.json. "
                    "S6 → S4 evidence linkage will break. This is the most common "
                    "integration failure."
                )
            else:
                ev = ev_map[eid]
                entities = ev.get("entities", [])
                if not entities:
                    r.warn(
                        f"{oid} → {eid}: entities list is empty. "
                        "S4 card will render but entity tags will be missing."
                    )

    return r


def check_confidence_badges(opps: List[Dict]) -> CheckResult:
    """
    Verify confidence enum values are uppercase.
    Track A renders HIGH=green, MEDIUM=amber, LOW=red.
    Lowercase or mixed-case breaks badge rendering.
    """
    r = CheckResult("confidence_badges")
    found_confidences = set()

    for opp in opps:
        conf = opp.get("confidence", "")
        found_confidences.add(conf)

        if conf not in CONFIDENCE_COLOURS:
            r.fail(
                f"{opp.get('id','?')}: confidence = '{conf}' is not a valid value. "
                f"Must be one of {set(CONFIDENCE_COLOURS.keys())} (uppercase). "
                "Badge colours: HIGH=green, MEDIUM=amber, LOW=red."
            )

    # Inform what colours will render
    for conf in found_confidences:
        if conf in CONFIDENCE_COLOURS:
            logger.info(
                f"Confidence badge: {conf} → {CONFIDENCE_COLOURS[conf]}"
            )

    return r


def check_tier_roadmap_mapping(opps: List[Dict]) -> CheckResult:
    """
    Verify tier → roadmap stage mapping for S9.
    Quick Win → NEXT_30, Strategic → NEXT_60, Complex → NEXT_90.
    """
    r = CheckResult("tier_roadmap_mapping")

    for opp in opps:
        oid = opp.get("id", "?")
        tier = opp.get("tier", "")

        if tier not in TIER_TO_ROADMAP:
            r.fail(f"{oid}: unknown tier '{tier}'")
            continue

        expected_stage = TIER_TO_ROADMAP[tier]
        # Check _debug.roadmap_stage if present (internal runner field)
        debug = opp.get("_debug", {})
        actual_stage = debug.get("roadmap_stage", "")
        if actual_stage and actual_stage != expected_stage:
            r.fail(
                f"{oid}: roadmap_stage='{actual_stage}' but tier='{tier}' "
                f"should map to '{expected_stage}'"
            )

    # Confirm tier coverage
    tiers_present = {o.get("tier") for o in opps}
    if "Quick Win" not in tiers_present:
        r.warn("No Quick Win opportunities — S9 NEXT_30 column will be empty")
    if "Strategic" not in tiers_present and "Complex" not in tiers_present:
        r.warn("No Strategic or Complex opportunities — S9 NEXT_60/NEXT_90 may be empty")

    return r


def check_s10_executive_report(opps: List[Dict], evs: List[Dict]) -> CheckResult:
    """
    Verify fields needed by S10 executive report:
    - sourcesAnalyzed: can be computed from evidence sources
    - topQuickWins: Quick Win opportunities for the report summary
    """
    r = CheckResult("s10_executive_report")

    sources = {e.get("source", "") for e in evs}
    sources.discard("")
    if not sources:
        r.fail("No source fields found in evidence — S10 sourcesAnalyzed will be empty")
    else:
        logger.info(f"S10 sourcesAnalyzed sources: {sources}")

    quick_wins = [o for o in opps if o.get("tier") == "Quick Win"]
    if not quick_wins:
        r.warn("No Quick Win opportunities — S10 topQuickWins will be empty")
    else:
        logger.info(
            f"S10 topQuickWins ({len(quick_wins)}): "
            f"{[qw.get('title','')[:30] for qw in quick_wins[:3]]}"
        )

    # All opportunities must have aiRationale for S10 readiness score
    no_rationale = [o.get("id") for o in opps if not o.get("aiRationale", "").strip()]
    if no_rationale:
        r.fail(f"Missing aiRationale on: {no_rationale} — S10 readiness panel incomplete")

    return r


def check_s7_quadrant_placement(opps: List[Dict]) -> CheckResult:
    """
    Verify every opportunity has numeric impact/effort for S7 quadrant placement.
    Check no items sit at origin (0,0) which would hide them from the map.
    """
    r = CheckResult("s7_quadrant_placement")

    for opp in opps:
        oid = opp.get("id", "?")
        impact = opp.get("impact", 0)
        effort = opp.get("effort", 0)

        if impact == 0 and effort == 0:
            r.fail(f"{oid}: impact=0, effort=0 — item will be invisible at quadrant origin")

        if not isinstance(impact, int) or not (1 <= impact <= 10):
            r.fail(f"{oid}: impact={impact} invalid for S7 quadrant (must be int 1-10)")

        if not isinstance(effort, int) or not (1 <= effort <= 10):
            r.fail(f"{oid}: effort={effort} invalid for S7 quadrant (must be int 1-10)")

    return r


def check_decision_field(opps: List[Dict]) -> CheckResult:
    """All seeded opportunities must be UNREVIEWED (Track A governs decisions)."""
    r = CheckResult("decision_unreviewed")
    non_unreviewed = [
        o.get("id") for o in opps
        if o.get("decision") != "UNREVIEWED"
    ]
    if non_unreviewed:
        r.fail(
            f"Opportunities with non-UNREVIEWED decision: {non_unreviewed}. "
            "Track B algorithm must always output UNREVIEWED. "
            "Decisions are Track A's responsibility."
        )
    return r


def check_ranking_consistency(opps: List[Dict]) -> CheckResult:
    """
    Verify that the seed output is ordered by the shared ranking function.
    Quick Wins should appear before Strategic/Complex.
    Top-3 exact order is a stretch KPI, not a blocking gate.
    """
    r = CheckResult("ranking_consistency")
    from .calibration.ranking import rank_opportunities, TIER_ORDER

    expected_order = [o["id"] for o in rank_opportunities(opps)]
    actual_order   = [o["id"] for o in opps]

    if expected_order != actual_order:
        r.warn(
            "Seed output is not in production ranking order. "
            "Run offline_export again — adapter now uses shared ranking. "
            f"Expected: {expected_order[:5]}, Got: {actual_order[:5]}"
        )

    # Quick Wins before Strategic/Complex (hard check)
    seen_non_qw = False
    for opp in opps:
        tier = opp.get("tier", "")
        if tier != "Quick Win":
            seen_non_qw = True
        if seen_non_qw and tier == "Quick Win":
            r.fail(
                f"{opp.get('id')}: Quick Win appears after Strategic/Complex in seed — "
                "ranking function not applied correctly"
            )
            break

    return r


# ─────────────────────────────────────────────────────────────────────────────
# Main verifier
# ─────────────────────────────────────────────────────────────────────────────

def run_verification(seed_dir: str = "backend/seed") -> Dict[str, Any]:
    """
    Run all SF-3.3 integration checks against seed files.

    Returns a report dict with per-check results, overall pass/fail,
    and the S1→S10 screen readiness summary.
    """
    report_time = datetime.now(timezone.utc).isoformat()
    seed_path   = Path(seed_dir)
    checks: List[CheckResult] = []

    # ── File existence ────────────────────────────────────────────────────────
    file_check = check_file_existence(seed_path)
    checks.append(file_check)

    if not file_check.passed:
        return _build_report(checks, report_time, seed_dir, [], [])

    # Load files
    opps = json.loads((seed_path / "opportunities.json").read_text(encoding="utf-8"))
    evs  = json.loads((seed_path / "evidence.json").read_text(encoding="utf-8"))

    # ── Schema checks ─────────────────────────────────────────────────────────
    checks.append(check_opportunity_schema(opps))
    checks.append(check_evidence_schema(evs))

    # ── Evidence linkage (most critical) ─────────────────────────────────────
    checks.append(check_evidence_linkage(opps, evs))

    # ── Screen-specific checks ────────────────────────────────────────────────
    checks.append(check_confidence_badges(opps))      # S7 badges
    checks.append(check_s7_quadrant_placement(opps))  # S7 quadrant
    checks.append(check_tier_roadmap_mapping(opps))   # S9 bucketing
    checks.append(check_s10_executive_report(opps, evs))  # S10 fields
    checks.append(check_decision_field(opps))         # S6 initial state
    checks.append(check_ranking_consistency(opps))    # Shared ranking

    return _build_report(checks, report_time, seed_dir, opps, evs)


def _build_report(
    checks: List[CheckResult],
    report_time: str,
    seed_dir: str,
    opps: List[Dict],
    evs: List[Dict],
) -> Dict[str, Any]:
    all_passed = all(c.passed for c in checks)
    all_issues = [issue for c in checks for issue in c.issues]
    all_warnings = [w for c in checks for w in c.warnings]

    screen_readiness = _build_screen_readiness(checks, opps)

    return {
        "sf33_passed":       all_passed,
        "report_time":       report_time,
        "seed_dir":          str(seed_dir),
        "opportunities_count": len(opps),
        "evidence_count":      len(evs),
        "checks":            [c.to_dict() for c in checks],
        "all_issues":        all_issues,
        "all_warnings":      all_warnings,
        "screen_readiness":  screen_readiness,
        "summary":           _build_summary(all_passed, checks, opps, evs),
    }


def _build_screen_readiness(
    checks: List[CheckResult],
    opps: List[Dict],
) -> List[Dict]:
    check_map = {c.name: c for c in checks}

    def _screen(screen, desc, *check_names):
        relevant = [check_map[n] for n in check_names if n in check_map]
        passed   = all(c.passed for c in relevant)
        issues   = [i for c in relevant for i in c.issues]
        return {"screen": screen, "description": desc,
                "ready": passed, "blocking_issues": issues[:3]}

    tiers = {o.get("tier") for o in opps}
    confs = {o.get("confidence") for o in opps}

    return [
        _screen("S4", "Partial Results — evidence cards",
                "evidence_schema", "evidence_linkage"),
        _screen("S4→S6", "Evidence linkage — card resolves from S6",
                "evidence_linkage"),
        _screen("S6", "Analyst Review — aiRationale, override, decision",
                "opportunity_schema", "decision_unreviewed"),
        _screen("S7", "Opportunity Map — quadrant placement + badges",
                "s7_quadrant_placement", "confidence_badges"),
        _screen("S9", "Pilot Roadmap — tier bucketing",
                "tier_roadmap_mapping", "ranking_consistency"),
        _screen("S10", "Executive Report — sources, top quick wins",
                "s10_executive_report"),
    ]


def _build_summary(
    all_passed: bool,
    checks: List[CheckResult],
    opps: List[Dict],
    evs: List[Dict],
) -> str:
    status = "PASSED" if all_passed else "FAILED"
    n_pass = sum(1 for c in checks if c.passed)
    n_fail = sum(1 for c in checks if not c.passed)
    n_warn = sum(len(c.warnings) for c in checks)
    tiers  = {o.get("tier","?") for o in opps}
    return (
        f"SF-3.3 {status} | "
        f"Checks: {n_pass} ✅ passed, {n_fail} ❌ failed, {n_warn} ⚠ warnings | "
        f"Opportunities: {len(opps)} | Evidence: {len(evs)} | "
        f"Tiers: {', '.join(sorted(tiers))}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    parser = argparse.ArgumentParser(
        description="SF-3.3 Integration Verifier — Export → Seed → Contract check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Three-command path:\n"
            "  1. python -m backend.discovery.offline_export\n"
            "  2. python -m backend.discovery.integration_verifier\n"
            "  3. pytest backend/discovery/tests/test_sf33_integration_verifier.py -v\n\n"
            "Then complete the S1→S10 manual walkthrough in SMOKE_DEMO_SF33.md\n"
            "and obtain Niranjan sign-off."
        ),
    )
    parser.add_argument(
        "--seed-dir", default="backend/seed",
        help="Directory containing opportunities.json and evidence.json"
    )
    parser.add_argument(
        "--report-path", default=None,
        help="Write JSON verification report to this path"
    )
    args = parser.parse_args()

    report = run_verification(args.seed_dir)

    print()
    print("=" * 65)
    print(report["summary"])
    print("=" * 65)

    print()
    print("Check results:")
    for check in report["checks"]:
        icon = "✅" if check["passed"] else "❌"
        print(f"  {icon} {check['check']}")
        for issue in check["issues"][:2]:
            print(f"      ❌ {issue[:90]}")
        for w in check["warnings"][:2]:
            print(f"      ⚠  {w[:90]}")

    print()
    print("Screen readiness:")
    for s in report["screen_readiness"]:
        icon = "✅" if s["ready"] else "❌"
        print(f"  {icon} {s['screen']:8s} {s['description']}")
        for issue in s["blocking_issues"][:1]:
            print(f"         ❌ {issue[:80]}")

    if args.report_path:
        Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_path).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(f"\nReport written to {args.report_path}")

    return 0 if report["sf33_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
