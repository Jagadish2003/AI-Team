"""
SF-2.8 stub — Runner CLI.
Orchestrates: ingest → detect → score → build_evidence → output JSON.

Usage:
    python -m backend.discovery.runner --mode offline
    python -m backend.discovery.runner --mode live
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def run(mode: str = "offline") -> list:
    os.environ["INGEST_MODE"] = mode
    logger.info(f"AgentIQ discovery runner — mode={mode}")

    # 1. Ingest
    from .ingest import salesforce, servicenow, jira
    sf_data = salesforce.ingest()
    sn_data = servicenow.ingest()
    jira_data = jira.ingest()
    logger.info(f"Ingested: sf={bool(sf_data)}, sn={bool(sn_data)}, jira={bool(jira_data)}")

    # 2. Detect
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
    results = []
    for det in all_detectors:
        fired = det.detect(sf_data, sn_data, jira_data)
        results.extend(fired)
        logger.info(f"  {det.__name__.split('.')[-1]}: {'FIRED' if fired else 'not fired'}")

    logger.info(f"Total detectors fired: {len(results)}")

    # 3. Score + evidence — stubs in SF-2.1, implemented in SF-2.6 / SF-2.7
    from .scorer import score
    from .evidence_builder import build_evidence

    opportunities = []
    for r in results:
        scored = score(r)
        evidence = build_evidence(r, scored)
        opp = {
            "detector_id": r.detector_id,
            "signal_source": r.signal_source,
            "metric_value": r.metric_value,
            "threshold": r.threshold,
            **scored,
            "evidenceIds": [e["id"] for e in evidence],
            "raw_evidence": r.raw_evidence,
        }
        opportunities.append(opp)

    logger.info(f"Opportunities produced: {len(opportunities)}")
    return opportunities


def main():
    parser = argparse.ArgumentParser(description="AgentIQ discovery runner")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    args = parser.parse_args()

    opps = run(args.mode)
    out = json.dumps(opps, indent=2)

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        logger.info(f"Output written to {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
