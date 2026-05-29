"""
SF-2.8 — Runner CLI — ENG-SHARED-1 pack selector added

Full pipeline: ingest → detect → score → build_evidence → OpportunityCandidate[]

Usage:
    python -m backend.discovery.runner --mode offline
    python -m backend.discovery.runner --mode offline --pack ncino
    python -m backend.discovery.runner --mode live --systems salesforce,jira --pack ncino
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
from dotenv import load_dotenv

# Track A adapter
from .track_a_adapter import export_track_a_seed

try:
    from app.temporal import DetectorEvaluation, snapshot_signals
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.temporal import DetectorEvaluation, snapshot_signals

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def _run_detector_phase(
    all_detectors: List[Any],
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any],
    jira_data: Dict[str, Any],
) -> tuple[List[Any], List[DetectorEvaluation]]:
    detector_results = []
    all_evaluated: List[DetectorEvaluation] = []

    for det in all_detectors:
        name = det.__name__.split(".")[-1]
        try:
            evaluation = det.evaluate(sf_data, sn_data, jira_data)
            all_evaluated.append(evaluation)

            fired = det.detect(sf_data, sn_data, jira_data)
            detector_results.extend(fired)
            if evaluation.fired != bool(fired):
                logger.warning(
                    "  %s: evaluate/detect fired mismatch (evaluation=%s, results=%d)",
                    name,
                    evaluation.fired,
                    len(fired),
                )
            status = f"FIRED ({len(fired)})" if fired else "not fired"
        except Exception as e:
            status = f"ERROR: {e}"
        logger.info(f"  {name}: {status}")

    logger.info(
        "Temporal detector evaluations captured: %d/%d",
        len(all_evaluated),
        len(all_detectors),
    )
    return detector_results, all_evaluated


def _snapshot_detector_evaluations(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: List[Any],
    all_evaluated: List[DetectorEvaluation],
) -> datetime:
    run_completed_at = datetime.now(timezone.utc)
    try:
        snapshot_signals(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            detector_results=detector_results,
            all_evaluated=all_evaluated,
            run_completed_at=run_completed_at,
        )
    except Exception as e:
        logger.warning("Signal snapshot failed (non-blocking): %s", e)
    return run_completed_at


def build_org_context(sf_data: Dict, sn_data: Dict, jira_data: Dict) -> Dict[str, Any]:
    cm = sf_data.get("case_metrics") or {}
    fi = sf_data.get("flow_inventory") or {}
    aps = sf_data.get("approval_processes") or []
    ncs = sf_data.get("named_credentials") or []
    csr_sf = sf_data.get("cross_system_references") or {}
    sn_im = (sn_data or {}).get("incident_metrics") or {}
    csr_sn = (sn_data or {}).get("cross_system_references") or {}
    sn_lc = (sn_data or {}).get("lending_correlation") or {}
    jira_im = (jira_data or {}).get("issue_metrics") or {}
    jira_lc = (jira_data or {}).get("lending_correlation") or {}

    return {
        "sf_total_cases_90d":    cm.get("total_cases_90d", 0),
        "sf_closed_cases_90d":   cm.get("closed_cases_90d", 0),
        "sf_owner_changes_90d":  cm.get("owner_changes_90d", 0),
        "sf_handoff_score":      cm.get("handoff_score", 0.0),
        "sf_active_flows":       fi.get("flow_activity_score", 0.0) if fi else 0,
        "sf_flow_activity_score":fi.get("flow_activity_score", 0.0),
        "sf_pending_approvals":  sum(a.get("pending_count", 0) for a in aps),
        "sf_approval_processes": len(aps),
        "sf_named_credentials":  len(ncs),
        "sf_echo_score":         csr_sf.get("sf_echo_score", 0.0),
        "sn_echo_score":         csr_sn.get("sn_echo_score", 0.0),
        "sn_total_incidents_90d": sn_im.get("total_incidents_90d", csr_sn.get("sn_total_incidents", 0)),
        "sn_lending_signal_count": sn_lc.get("total_matched", 0),
        "jira_echo_score":       jira_im.get("jira_echo_score", 0.0),
        "jira_total_issues_90d":  jira_im.get("total_issues_90d", 0),
        "jira_lending_signal_count": jira_lc.get("total_matched", 0),
        "sources_connected": {
            "salesforce":  bool(sf_data),
            "servicenow":  bool(sn_data),
            "jira":        bool(jira_data),
        },
    }

def run(
    mode: Optional[str] = None,
    run_id: Optional[str] = None,
    org_id: str = "demo-org",
    systems: Optional[List[str]] = None,
    pack: Optional[str] = None,
) -> Dict[str, Any]:
    # ENG-SHARED-1: resolve pack config — replaces temporary is_ncino_pack conditional
    from .packs.pack_config import get_pack, get_pack_domain, is_ncino_pack
    pack_config = get_pack(pack)
    pack_id     = pack_config["packId"]
    pack_domain = pack_config["pack_domain"]

    # Default to all systems if None
    if mode is None:
        mode = os.environ.get("INGEST_MODE", "offline").strip().lower()
        if mode not in ("offline", "live"):
            mode = "offline"

    _systems = set(systems) if systems else {"salesforce", "servicenow", "jira"}
    os.environ["INGEST_MODE"] = mode
    if run_id is None:
        run_id = f"run_{uuid.uuid4().hex[:8]}"

    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"AgentIQ discovery runner — mode={mode} run_id={run_id} pack={pack_id}")

    # 1. Ingest
    from .ingest import salesforce, servicenow, jira as jira_mod
    from .ingest.salesforce import IngestError as SFError
    from .ingest.servicenow import ServiceNowIngestError as SNError
    from .ingest.jira import JiraIngestError

    sf_data, sn_data, jira_data = {}, {}, {}
    logger.info(f"Systems: {sorted(list(_systems))}")

    try:
        if "salesforce" in _systems:
            sf_data = salesforce.ingest()
            logger.info("Salesforce ingestion: OK")
    except SFError as e:
        logger.error(f"Salesforce ingestion FAILED: {e}")

    try:
        if "servicenow" in _systems:
            sn_data = servicenow.ingest()
            if sn_data: logger.info("ServiceNow ingestion: OK")
    except SNError as e:
        logger.error(f"ServiceNow ingestion FAILED: {e}")

    try:
        if "jira" in _systems:
            jira_data = jira_mod.ingest()
            if jira_data: logger.info("Jira ingestion: OK")
    except JiraIngestError as e:
        logger.error(f"Jira ingestion FAILED: {e}")

    if not sf_data and "salesforce" in _systems:
        logger.error("Salesforce data unavailable — cannot run detectors. Aborting.")
        return _empty_run(run_id, org_id, mode, started_at)

    # 2a. nCino ingest — if ncino pack, fetch lending signals from nCino objects
    from .packs.pack_config import is_ncino_pack as _is_ncino
    if _is_ncino(pack_id) and "salesforce" in _systems:
        try:
            from .ingest.ncino import ingest as ncino_ingest
            ncino_data = ncino_ingest()
            # Merge ncino data into sf_data so detectors can find it
            if sf_data is None:
                sf_data = {}
            sf_data["ncino"] = ncino_data
            logger.info("nCino ingestion: OK — %d lending metrics", len(ncino_data))
        except Exception as e:
            logger.warning("nCino ingestion failed (non-blocking): %s", e)

    # 2b. STRS Benefits ingest — if strs_benefits pack
    from .packs.pack_config import is_strs_benefits_pack as _is_strs
    if _is_strs(pack_id) and "salesforce" in _systems:
        try:
            from .ingest.strs_benefits import ingest as strs_ingest
            strs_data = strs_ingest()
            if sf_data is None:
                sf_data = {}
            sf_data["strs_benefits"] = strs_data
            logger.info("STRS Benefits ingestion: OK — %d benefit metrics", len(strs_data))
        except Exception as e:
            logger.warning("STRS Benefits ingestion failed (non-blocking): %s", e)

    # 2. Context
    org_ctx = build_org_context(sf_data, sn_data, jira_data)

    # 3. Detect — ENG-AIQ-NC-4: pack-driven detector selection
    # Replaces hardcoded Service Cloud detector list.
    # pack_config.py (ENG-SHARED-1) defines which detectors each pack activates.
    from .packs.pack_config import is_ncino_pack

    if is_ncino_pack(pack_id):
        # nCino lending detectors — confirmed objects from SF-NC-2
        from .detectors import (
            loan_origination_routing_friction,
            covenant_tracking_gap,
            checklist_bottleneck,
            spreading_bottleneck,
            approval_bottleneck,
        )
        all_detectors = [
            loan_origination_routing_friction,
            covenant_tracking_gap,
            checklist_bottleneck,
            spreading_bottleneck,
            approval_bottleneck,
        ]
        logger.info("Pack: ncino — 5 lending detectors active")
    elif _is_strs(pack_id):
        from .detectors import (
            application_stall,
            benefit_election_deadline,
            disbursement_overdue,
            disability_review_bottleneck,
        )
        all_detectors = [
            application_stall,
            benefit_election_deadline,
            disbursement_overdue,
            disability_review_bottleneck,
        ]
        logger.info("Pack: strs_benefits — 4 benefit detectors active")
    else:
        # Service Cloud detectors — default
        from .detectors import (
            repetition, handoff_friction, approval_delay,
            knowledge_gap, integration_concentration,
            permission_bottleneck, cross_system_echo,
        )
        all_detectors = [repetition, handoff_friction, approval_delay, knowledge_gap,
                         integration_concentration, permission_bottleneck, cross_system_echo]
        logger.info("Pack: service_cloud — 7 SC detectors active")

    # Capture fired and non-firing detector evaluations before scoring.
    detector_results, all_evaluated = _run_detector_phase(
        all_detectors,
        sf_data,
        sn_data,
        jira_data,
    )
    _snapshot_detector_evaluations(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        detector_results=detector_results,
        all_evaluated=all_evaluated,
    )

    # 4. Score + Evidence
    # ENG-AIQ-NC-4: use lending_scorer for ncino pack, SC scorer for service_cloud
    from .scorer import score as sc_score
    from .lending_scorer import score_lending, is_lending_detector
    from .strs_benefits_scorer import score_strs_benefits, is_strs_benefits_detector
    from .evidence_builder import build_evidence
    id_counter = itertools.count(1)
    def id_factory() -> str: return f"{run_id[-6:]}_{next(id_counter):04d}"

    # Issue 3 fix: collect Jira/SN lending correlation by detector for ncino pack.
    # Wave 2 (ENG-AIQ-NC-2/NC-3) built lending_correlation — wire it into evidence here.
    jira_by_detector: Dict[str, List[str]] = {}
    sn_by_detector:   Dict[str, List[str]] = {}
    if is_ncino_pack(pack_id):
        if jira_data:
            jira_by_detector = (
                jira_data.get("lending_correlation", {}).get("by_detector", {})
            )
        if sn_data:
            sn_by_detector = (
                sn_data.get("lending_correlation", {}).get("by_detector", {})
            )

    # ── STRS Benefits corroboration — ENG-STRS-CORR-1/2 (Fix Pack Sprint 7) ──
    # Same pattern as nCino above. strs_benefits.py ingest() now returns
    # jira_strs_correlation and sn_strs_correlation inside the metrics dict,
    # which is merged into sf_data["strs_benefits"]. Extract by_detector here.
    if _is_strs(pack_id):
        strs_metrics = sf_data.get("strs_benefits", {})
        jira_by_detector = (
            strs_metrics.get("jira_strs_correlation", {}).get("by_detector", {})
        )
        sn_by_detector = (
            strs_metrics.get("sn_strs_correlation", {}).get("by_detector", {})
        )
        if jira_by_detector:
            logger.info(
                "STRS Jira corroboration: %d detectors have Jira evidence",
                len(jira_by_detector),
            )
        if sn_by_detector:
            logger.info(
                "STRS ServiceNow corroboration: %d detectors have SN evidence",
                len(sn_by_detector),
            )

    opportunities = []
    for dr in detector_results:
        # Select scorer based on pack
        if is_ncino_pack(pack_id) and is_lending_detector(dr.detector_id):
            scored = score_lending(dr)
        elif _is_strs(pack_id) and is_strs_benefits_detector(dr.detector_id):
            scored = score_strs_benefits(dr)
        else:
            scored = sc_score(dr)
        # Pass packId so build_evidence uses nCino banking-language builders
        scored_with_pack = {**scored, "packId": pack_id}
        evidence_list = build_evidence(dr, scored_with_pack, id_factory=id_factory)

        # Issue 3 fix: attach Jira/SN corroboration evidence for ncino pack.
        # These appear as additional evidence items in S4 alongside nCino evidence.
        # Does not yet modulate confidence — deferred to post-Sprint 5.
        if is_ncino_pack(pack_id) or _is_strs(pack_id):
            corroboration_count = 0
            for snippet in jira_by_detector.get(dr.detector_id, []):
                ev_id = id_factory()
                evidence_list.append({
                    "id":          ev_id,
                    "tsLabel":     "",
                    "source":      "Jira",
                    "detectorId":  dr.detector_id,
                    "evidenceType":"Metric",
                    "title":       f"Jira corroboration: {dr.detector_id}",
                    "snippet":     snippet,
                    "entities":    [],
                    "confidence":  "MEDIUM",
                    "decision":    "UNREVIEWED",
                })
                corroboration_count += 1
            for snippet in sn_by_detector.get(dr.detector_id, []):
                ev_id = id_factory()
                evidence_list.append({
                    "id":          ev_id,
                    "tsLabel":     "",
                    "source":      "ServiceNow",
                    "detectorId":  dr.detector_id,
                    "evidenceType":"Metric",
                    "title":       f"ServiceNow corroboration: {dr.detector_id}",
                    "snippet":     snippet,
                    "entities":    [],
                    "confidence":  "MEDIUM",
                    "decision":    "UNREVIEWED",
                })
                corroboration_count += 1
            if corroboration_count > 0:
                logger.info("  %s: +%d corroborating evidence items (Jira/SN)",
                            dr.detector_id, corroboration_count)
        opp = {
            "runId": run_id, "orgId": org_id, "detector_id": dr.detector_id,
            "packId": pack_id,
            "signal_source": dr.signal_source, "metric_value": dr.metric_value,
            "threshold": dr.threshold, "impact": scored["impact"], "effort": scored["effort"],
            "confidence": scored["confidence"], "tier": scored["tier"],
            "roadmap_stage": scored["roadmap_stage"], "evidenceIds": [e["id"] for e in evidence_list],
            "evidence": evidence_list, "raw_evidence": dr.raw_evidence, "score_debug": scored["score_debug"],
        }
        # ENG-AIQ-NC-5 Issue 1: inject approved UI labels from pack UI label files.
        # Deterministic config text — not LLM generated:
        #   title      → s6_title   (S6 opportunity card heading)
        #   category   → s7_category (S7 detail panel category)
        #   description → s6_desc   (S6 one-line description)
        # LLM-generated narrative (from run_llm_enrichment):
        #   aiSummary / aiWhyBullets / aiRisks / aiSuggestedNextSteps → S4
        #   s9_roadmap label seeds the LLM blueprint prompt → S9
        #   s10_exec label seeds the LLM exec summary prompt → S10
        from .packs.pack_config import get_ui_labels
        ui_labels = get_ui_labels(pack_id) or {}
        if ui_labels:
            det_labels = ui_labels.get(dr.detector_id, {})
            opp["title"]       = det_labels.get("s6_title", dr.detector_id)
            opp["category"]    = det_labels.get("s7_category", "Automation Opportunity")
            opp["description"] = det_labels.get("s6_desc", "")
            opp["s9_roadmap"]  = det_labels.get("s9_roadmap", "")
            opp["s10_exec"]    = det_labels.get("s10_exec", "")
            opp["compliance_guardrail"] = det_labels.get("compliance_guardrail")

        opportunities.append(opp)

    return {
        "runId": run_id, "orgId": org_id, "mode": mode,
        "packId": pack_id,
        "startedAt": started_at, "completedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": org_ctx, "opportunities": opportunities,
    }

def _empty_run(run_id: str, org_id: str, mode: str, started_at: str) -> Dict:
    return {"runId": run_id, "orgId": org_id, "mode": mode, "startedAt": started_at,
            "completedAt": datetime.now(timezone.utc).isoformat(), "inputs": {}, "opportunities": []}

def main():
    parser = argparse.ArgumentParser(description="AgentIQ discovery runner")

    # Dynamically read INGEST_MODE from environment, fallback to "offline"
    default_mode = os.environ.get("INGEST_MODE", "offline").strip().lower()
    if default_mode not in ("offline", "live"):
        default_mode = "offline"

    parser.add_argument("--mode", choices=["offline", "live"], default=default_mode)
    parser.add_argument("--systems", help="Comma-separated list of systems (e.g. salesforce,jira)")
    parser.add_argument("--pack", default=None, help="Pack ID: service_cloud (default) or ncino")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--run-id", help="Explicit run ID")
    parser.add_argument("--org-id", default="demo-org")
    parser.add_argument("--output-format", choices=["internal", "track_a_seed"], default="internal")

    args = parser.parse_args()

    # Parse systems string into a list if provided
    systems_list = None
    if args.systems:
        systems_list =[s.strip().lower() for s in args.systems.split(",") if s.strip()]

    payload = run(
        mode=args.mode,
        run_id=args.run_id,
        org_id=args.org_id,
        systems=systems_list,
        pack=args.pack,
    )

    if args.output_format == "track_a_seed":
        payload = export_track_a_seed(payload)

    out = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        logger.info(f"Output written to {args.output}")
    else:
        print(out)

if __name__ == "__main__":
    main()
 
