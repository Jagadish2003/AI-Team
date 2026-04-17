"""
SF-2.8 — Offline Export CLI

The "one command" workflow for Track A integration:

    python -m backend.discovery.offline_export

This always:
  1. Runs the full pipeline in offline mode (fixtures — no credentials needed)
  2. Exports two JSON files that Track A's seed_loader.py expects:
       backend/seed/opportunities.json  — OpportunityCandidate[] (Track A shape)
       backend/seed/evidence.json       — EvidenceReview[] (Track A shape)

Usage examples:

    # Standard export to backend/seed/
    python -m backend.discovery.offline_export

    # Export to a custom directory
    python -m backend.discovery.offline_export --out-dir runs/demo_2026_04_16

    # Export Salesforce-only (skip SN/Jira fixtures)
    python -m backend.discovery.offline_export --systems salesforce

    # Preview what would be exported without writing files
    python -m backend.discovery.offline_export --dry-run

Why this exists:
    The runner.py --output-format track_a_seed flag is correct but requires
    knowing the right combination of flags. This script is the zero-config
    path that always does the right thing for demos and integration testing.
    It never needs credentials. It always produces Track A compatible output.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def export(
    out_dir: str = "backend/seed",
    systems: list = None,
    dry_run: bool = False,
    run_id: str = None,
    org_id: str = "demo-org",
) -> dict:
    """
    Run offline pipeline and write Track A seed files.
    Returns the exported seed dict for testing/inspection.
    """
    os.environ["INGEST_MODE"] = "offline"

    from .runner import run
    from .track_a_adapter import export_track_a_seed

    logger.info("AgentIQ offline export — mode=offline (fixtures)")
    if systems:
        logger.info(f"Systems: {systems}")

    # Run the pipeline
    payload = run(mode="offline", run_id=run_id, org_id=org_id, systems=systems)
    seed = export_track_a_seed(payload, id_counter=itertools.count(1))

    n_opps = len(seed["opportunities"])
    n_ev   = len(seed["evidence"])
    logger.info(f"Produced: {n_opps} opportunities, {n_ev} evidence objects")

    if dry_run:
        logger.info("[DRY RUN] No files written. Would write:")
        logger.info(f"  {out_dir}/opportunities.json")
        logger.info(f"  {out_dir}/evidence.json")
        for opp in seed["opportunities"]:
            logger.info(
                f"  {opp['id']} | {opp['_debug']['detector_id']:30s} | "
                f"{opp['tier']:12s} | {opp['confidence']}"
            )
        return seed

    # Write files
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    opps_file = out_path / "opportunities.json"
    evs_file  = out_path / "evidence.json"

    # opportunities.json: Track A shape (omit _debug for clean seed)
    clean_opps = []
    for opp in seed["opportunities"]:
        clean = {k: v for k, v in opp.items() if k != "_debug"}
        clean_opps.append(clean)

    opps_file.write_text(
        json.dumps(clean_opps, indent=2), encoding="utf-8"
    )
    evs_file.write_text(
        json.dumps(seed["evidence"], indent=2), encoding="utf-8"
    )

    logger.info(f"Written: {opps_file} ({n_opps} opportunities)")
    logger.info(f"Written: {evs_file} ({n_ev} evidence objects)")
    logger.info("")
    logger.info("Next step: python backend/seed_loader.py")

    return seed


def main():
    parser = argparse.ArgumentParser(
        description=(
            "AgentIQ offline export — produces Track A seed files without credentials.\n\n"
            "Quick start:\n"
            "  python -m backend.discovery.offline_export\n"
            "  python backend/seed_loader.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out-dir", default="backend/seed",
        help="Directory to write opportunities.json and evidence.json (default: backend/seed)",
    )
    parser.add_argument(
        "--systems", nargs="+",
        choices=["salesforce", "servicenow", "jira"],
        default=None,
        metavar="SYSTEM",
        help=(
            "Which systems to include. Default: all. "
            "Example: --systems salesforce servicenow"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be exported without writing files",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="Explicit run ID (default: auto-generated)",
    )
    parser.add_argument(
        "--org-id", default="demo-org",
        help="Org identifier (default: demo-org)",
    )
    args = parser.parse_args()

    export(
        out_dir=args.out_dir,
        systems=args.systems,
        dry_run=args.dry_run,
        run_id=args.run_id,
        org_id=args.org_id,
    )


if __name__ == "__main__":
    main()
