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

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

def build_org_context(sf_data: Dict, sn_data: Dict, jira_data: Dict) -> Dict[str, Any]:
    cm = sf_data.get("case_metrics") or {}
    fi = sf_data.get("flow_inventory") or {}
    aps = sf_data.get("approval_processes") or []
    ncs = sf_data.get("named_credentials") or []
    csr_sf = sf_data.get("cross_system_references") or {}
    csr_sn = (sn_data or {}).get("cross_system_references") or {}
    jira_im = (jira_data or {}).get("issue_metrics") or {}

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
        "jira_echo_score":       jira_im.get("jira_echo_score", 0.0),
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

    # 2. Context
    org_ctx = build_org_context(sf_data, sn_data, jira_data)

    # 3. Detect
    from .detectors import (
        repetition, handoff_friction, approval_delay,
        knowledge_gap, integration_concentration,
        permission_bottleneck, cross_system_echo,
    )
    all_detectors = [repetition, handoff_friction, approval_delay, knowledge_gap,
                     integration_concentration, permission_bottleneck, cross_system_echo]

    detector_results = []
    for det in all_detectors:
        name = det.__name__.split(".")[-1]
        try:
            fired = det.detect(sf_data, sn_data, jira_data)
            detector_results.extend(fired)
            status = f"FIRED ({len(fired)})" if fired else "not fired"
        except Exception as e:
            fired, status = [], f"ERROR: {e}"
        logger.info(f"  {name}: {status}")

    # 4. Score + Evidence
    from .scorer import score
    from .evidence_builder import build_evidence
    id_counter = itertools.count(1)
    def id_factory() -> str: return f"{run_id[-6:]}_{next(id_counter):04d}"

    opportunities = []
    for dr in detector_results:
        scored = score(dr)
        evidence_list = build_evidence(dr, scored, id_factory=id_factory)
        opp = {
            "runId": run_id, "orgId": org_id, "detector_id": dr.detector_id,
            "signal_source": dr.signal_source, "metric_value": dr.metric_value,
            "threshold": dr.threshold, "impact": scored["impact"], "effort": scored["effort"],
            "confidence": scored["confidence"], "tier": scored["tier"],
            "roadmap_stage": scored["roadmap_stage"], "evidenceIds": [e["id"] for e in evidence_list],
            "evidence": evidence_list, "raw_evidence": dr.raw_evidence, "score_debug": scored["score_debug"],
        }
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
 
