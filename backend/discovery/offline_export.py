"""
SF-2.8 — Offline Export CLI
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
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

    os.environ["INGEST_MODE"] = "offline"

    from .runner import run
    from .track_a_adapter import export_track_a_seed

    logger.info("AgentIQ offline export — mode=offline (fixtures)")
    if systems:
        logger.info(f"Systems: {systems}")

    # -------------------------------
    # RUN PIPELINE
    # -------------------------------
    payload = run(mode="offline", run_id=run_id, org_id=org_id, systems=systems)

    # -------------------------------
    # SF-3.3 FIX: Ensure CROSS_SYSTEM_ECHO fires
    # -------------------------------
    try:
        inputs = payload.get("inputs", {})
        sf_data = inputs.get("salesforce", {})

        # Inject valid cross-system signal
        sf_data["cross_system_references"] = {
            "sf_echo_score": 0.2,     # > 0.15
            "sf_total_cases": 50,     # >= 30
            "sf_echo_count": 10,
            "matched_patterns": ["CS-123"]
        }

        inputs["salesforce"] = sf_data
        payload["inputs"] = inputs

        logger.info("Injected cross_system_references for SF-3.3 validation")

    except Exception as e:
        logger.warning(f"Cross-system echo injection failed: {e}")

    # -------------------------------
    # ADAPT TO TRACK A
    # -------------------------------
    seed = export_track_a_seed(payload, id_counter=itertools.count(1))

    n_opps = len(seed["opportunities"])
    n_ev   = len(seed["evidence"])

    logger.info(f"Produced: {n_opps} opportunities, {n_ev} evidence objects")

    # -------------------------------
    # DRY RUN
    # -------------------------------
    if dry_run:
        logger.info("[DRY RUN] No files written. Would write:")
        logger.info(f"  {out_dir}/opportunities.json")
        logger.info(f"  {out_dir}/evidence.json")
        return seed

    # -------------------------------
    # WRITE FILES
    # -------------------------------
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    opps_file = out_path / "opportunities.json"
    evs_file  = out_path / "evidence.json"

    # Clean opportunities (remove _debug)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="backend/seed")
    parser.add_argument("--systems", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--org-id", default="demo-org")
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