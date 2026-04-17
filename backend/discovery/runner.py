"""
SF-2.8 — Runner CLI

Full pipeline: ingest → detect → score → build_evidence → OpportunityCandidate[]

Usage:
    python -m backend.discovery.runner --mode offline
    python -m backend.discovery.runner --mode offline --output runs/run_001.json
    python -m backend.discovery.runner --mode live   --run-id my-run-001

Output: seed_loader-compatible OpportunityCandidate[] JSON.

OpportunityCandidate shape (Track A contract):
    runId             str   — unique run identifier
    detector_id       str   — which detector fired
    signal_source     str   — salesforce | servicenow | jira
    metric_value      float — measured value
    threshold         float — threshold that was exceeded
    impact            int   — 1-10
    effort            int   — 1-10
    confidence        str   — HIGH | MEDIUM | LOW
    tier              str   — Quick Win | Strategic | Complex
    roadmap_stage     str   — NEXT_30 | NEXT_60 | NEXT_90
    evidenceIds       list  — list of evidence object IDs
    evidence          list  — full evidence objects (for seed_loader)
    raw_evidence      dict  — raw detector output (for calibration)
    score_debug       dict  — scorer intermediate values (for SF-3.2)
    inputs            dict  — run metadata (sources, counts)
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Track A adapter (Option A — internal artifact preserved, Track A export added)
from .track_a_adapter import export_track_a_seed

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# Detector names for logging
_DETECTOR_LABELS = {
    "repetition":               "repetition",
    "handoff_friction":         "handoff_friction",
    "approval_delay":           "approval_delay",
    "knowledge_gap":            "knowledge_gap",
    "integration_concentration":"integration_concentration",
    "permission_bottleneck":    "permission_bottleneck",
    "cross_system_echo":        "cross_system_echo",
}


# ─────────────────────────────────────────────────────────────────────────────
# org_context builder stub  (per SF-2.6 feedback — explicit field sourcing)
# ─────────────────────────────────────────────────────────────────────────────

def build_org_context(sf_data: Dict, sn_data: Dict, jira_data: Dict) -> Dict[str, Any]:
    """
    Build a summary of the org context for run metadata.
    Explicitly maps ingested data to known fields so scorer inputs are traceable.

    This is the stub flagged in SF-2.6 feedback. In SF-3.2 this expands to
    include weekly_volume, latency_days, handoff_per_record etc. for calibration.
    """
    cm = sf_data.get("case_metrics") or {}
    fi = sf_data.get("flow_inventory") or {}
    aps = sf_data.get("approval_processes") or []
    ncs = sf_data.get("named_credentials") or []
    csr_sf = sf_data.get("cross_system_references") or {}
    csr_sn = (sn_data or {}).get("cross_system_references") or {}
    jira_im = (jira_data or {}).get("issue_metrics") or {}

    return {
        # Volume signals
        "sf_total_cases_90d":    cm.get("total_cases_90d", 0),
        "sf_closed_cases_90d":   cm.get("closed_cases_90d", 0),
        "sf_owner_changes_90d":  cm.get("owner_changes_90d", 0),
        "sf_handoff_score":      cm.get("handoff_score", 0.0),
        # Flow signals
        "sf_active_flows":       fi.get("active_flow_count_on_object", 0),
        "sf_flow_activity_score":fi.get("flow_activity_score", 0.0),
        # Approval signals
        "sf_pending_approvals":  sum(a.get("pending_count", 0) for a in aps),
        "sf_approval_processes": len(aps),
        # Integration signals
        "sf_named_credentials":  len(ncs),
        # Cross-system signals
        "sf_echo_score":         csr_sf.get("sf_echo_score", 0.0),
        "sn_echo_score":         csr_sn.get("sn_echo_score", 0.0),
        "jira_echo_score":       jira_im.get("jira_echo_score", 0.0),
        # Source availability
        "sources_connected": {
            "salesforce":  bool(sf_data),
            "servicenow":  bool(sn_data),
            "jira":        bool(jira_data),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run(
    mode: str = "offline",
    run_id: Optional[str] = None,
    org_id: str = "demo-org",
    systems: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Execute the full AgentIQ discovery pipeline.

    Parameters
    ----------
    mode     : "offline" (fixtures) or "live" (real APIs)
    run_id   : explicit run identifier; auto-generated if None
    org_id   : org label for this run
    systems  : which systems to ingest from — ["salesforce", "servicenow", "jira"]
               Default None = all available. Use to make demo runs deterministic.
    """
    _systems = set(systems) if systems else {"salesforce", "servicenow", "jira"}
    """
    Execute the full AgentIQ discovery pipeline.

    Returns a run payload dict with:
        runId, orgId, mode, startedAt, completedAt,
        inputs (org context), opportunities (OpportunityCandidate[])
    """
    os.environ["INGEST_MODE"] = mode
    if run_id is None:
        run_id = f"run_{uuid.uuid4().hex[:8]}"

    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"AgentIQ discovery runner — mode={mode} run_id={run_id}")

    # ── 1. Ingest ─────────────────────────────────────────────────────────────
    from .ingest import salesforce, servicenow, jira as jira_mod
    from .ingest.salesforce import IngestError as SFError
    from .ingest.servicenow import ServiceNowIngestError as SNError
    from .ingest.jira import JiraIngestError

    sf_data: Dict = {}
    sn_data: Dict = {}
    jira_data: Dict = {}

    logger.info(f"Systems: {sorted(_systems)}")

    try:
        if "salesforce" in _systems:
            sf_data = salesforce.ingest()
            logger.info("Salesforce ingestion: OK")
        else:
            logger.info("Salesforce: skipped (not in --systems)")
    except SFError as e:
        logger.error(f"Salesforce ingestion FAILED: {e}")

    try:
        if "servicenow" in _systems:
            sn_data = servicenow.ingest()
            if sn_data:
                logger.info("ServiceNow ingestion: OK")
        else:
            logger.info("ServiceNow: skipped (not in --systems)")
    except SNError as e:
        logger.error(f"ServiceNow ingestion FAILED: {e}")

    try:
        if "jira" in _systems:
            jira_data = jira_mod.ingest()
            if jira_data:
                logger.info("Jira ingestion: OK")
        else:
            logger.info("Jira: skipped (not in --systems)")
    except JiraIngestError as e:
        logger.error(f"Jira ingestion FAILED: {e}")

    logger.info(
        f"Ingested: sf={bool(sf_data)}, sn={bool(sn_data)}, jira={bool(jira_data)}"
    )

    if not sf_data:
        logger.error("Salesforce data unavailable — cannot run detectors. Aborting.")
        return _empty_run(run_id, org_id, mode, started_at)

    # ── 2. Build org context ──────────────────────────────────────────────────
    org_ctx = build_org_context(sf_data, sn_data, jira_data)

    # ── 3. Detect ─────────────────────────────────────────────────────────────
    from .detectors import (
        repetition, handoff_friction, approval_delay,
        knowledge_gap, integration_concentration,
        permission_bottleneck, cross_system_echo,
    )

    all_detectors = [
        repetition, handoff_friction, approval_delay,
        knowledge_gap, integration_concentration,
        permission_bottleneck, cross_system_echo,
    ]

    detector_results = []
    for det in all_detectors:
        name = det.__name__.split(".")[-1]
        try:
            fired = det.detect(sf_data, sn_data, jira_data)
            detector_results.extend(fired)
            status = f"FIRED ({len(fired)})" if fired else "not fired"
        except Exception as e:
            fired = []
            status = f"ERROR: {e}"
        logger.info(f"  {name}: {status}")

    fired_count = len(detector_results)
    logger.info(f"Total detectors fired: {fired_count}")

    # ── 4. Score + Evidence ───────────────────────────────────────────────────
    from .scorer import score
    from .evidence_builder import build_evidence

    # Deterministic IDs within a run for seed_loader reproducibility
    id_counter = itertools.count(1)
    def id_factory() -> str:
        return f"{run_id[-6:]}_{next(id_counter):04d}"

    opportunities: List[Dict] = []

    for dr in detector_results:
        # Score
        scored = score(dr)

        # Evidence
        evidence_list = build_evidence(dr, scored, id_factory=id_factory)

        # Confidence downgrade if evidence build failed (SF-1.5 permissive mode)
        if not evidence_list and scored["confidence"] != "LOW":
            logger.warning(
                f"{dr.detector_id}: evidence build failed — downgrading "
                f"confidence from {scored['confidence']} to LOW"
            )
            scored = {**scored, "confidence": "LOW", "tier": _downgrade_tier(scored["tier"])}

        opp: Dict[str, Any] = {
            "runId":        run_id,
            "orgId":        org_id,
            "detector_id":  dr.detector_id,
            "signal_source":dr.signal_source,
            "metric_value": dr.metric_value,
            "threshold":    dr.threshold,
            # Scored fields
            "impact":       scored["impact"],
            "effort":       scored["effort"],
            "confidence":   scored["confidence"],
            "tier":         scored["tier"],
            "roadmap_stage":scored["roadmap_stage"],
            # Evidence
            "evidenceIds":  [e["id"] for e in evidence_list],
            "evidence":     evidence_list,
            # Debug / calibration
            "raw_evidence": dr.raw_evidence,
            "score_debug":  scored["score_debug"],
        }
        opportunities.append(opp)

    completed_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"Opportunities produced: {len(opportunities)}")

    return {
        "runId":       run_id,
        "orgId":       org_id,
        "mode":        mode,
        "startedAt":   started_at,
        "completedAt": completed_at,
        "inputs":      org_ctx,
        "opportunities": opportunities,
    }


def _downgrade_tier(tier: str) -> str:
    """Downgrade tier one level (matches SF-1.4 LOW confidence rule)."""
    if tier == "Quick Win":  return "Strategic"
    if tier == "Strategic":  return "Complex"
    return "Complex"


def _empty_run(run_id: str, org_id: str, mode: str, started_at: str) -> Dict:
    return {
        "runId": run_id, "orgId": org_id, "mode": mode,
        "startedAt": started_at,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": {}, "opportunities": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AgentIQ discovery runner")
    parser.add_argument("--mode",   choices=["offline", "live"], default="offline")
    parser.add_argument("--output", default=None,
                        help="Output JSON file path (default: stdout)")
    parser.add_argument("--run-id", default=None,
                        help="Explicit run ID (default: auto-generated)")
    parser.add_argument("--org-id", default="demo-org",
                        help="Org identifier for this run")
    parser.add_argument(
        "--output-format",
        choices=["internal", "track_a_seed"],
        default="internal",
        help=(
            "Output format: 'internal' = detector-centric (default, for calibration). "
            "'track_a_seed' = Track A OpportunityCandidate[] shape ready for seed_loader.py."
        ),
    )
    args = parser.parse_args()

    payload = run(mode=args.mode, run_id=args.run_id, org_id=args.org_id)

    if args.output_format == "track_a_seed":
        export = export_track_a_seed(payload)
        out = json.dumps(export, indent=2)
        logger.info(
            f"Output format: track_a_seed — "
            f"{len(export['opportunities'])} opportunities, "
            f"{len(export['evidence'])} evidence objects"
        )
    else:
        out = json.dumps(payload, indent=2)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        logger.info(f"Output written to {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
