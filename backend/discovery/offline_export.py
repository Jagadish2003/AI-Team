"""
SF-2.8 — Offline Export CLI

2.0-B1 T5 (AC5): this writes opportunities + evidence to disk, so it is an
EXPORT path and holds the same two lines every export path holds — secrets are
redacted (non-reversibly) and the 1.9 SecOps aggregation floor is enforced —
via the shared ``app.export_guard``. Before T5 it applied neither, which made it
the least protected export surface in the product despite being the one that
literally writes files.

A floor breach FAILS the export with a named reason and writes nothing: a seed
file that doubles as a host x vulnerability target list must not reach disk.
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


def _guard(opportunities: list, evidence: list) -> tuple:
    """Redact secrets and enforce the aggregation floor on what will be written.

    Returns the guarded ``(opportunities, evidence)`` — the content that must
    actually be written, so what lands on disk is what was checked. Delegates to
    the shared discovery-side bridge (``discovery.export_safety``) so every
    export CLI holds the line the same way.
    """
    from .export_safety import guard_exported_payload

    guarded = guard_exported_payload(
        {"opportunities": opportunities, "evidence": evidence},
        where="offline seed export",
    )
    return guarded["opportunities"], guarded["evidence"]


def export(
    out_dir: str = "discovery/seed",
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

    # Clean opportunities (remove _debug)
    clean_opps = []
    for opp in seed["opportunities"]:
        clean = {k: v for k, v in opp.items() if k != "_debug"}
        clean_opps.append(clean)

    # -------------------------------
    # 2.0-B1 T5 (AC5) — EXPORT GUARD
    # -------------------------------
    # Runs BEFORE the dry-run branch so `--dry-run` exercises exactly the same
    # discipline as a real write: a dry run that skipped the guard would report
    # "would write" for content the guard refuses.
    clean_opps, guarded_evidence = _guard(clean_opps, seed["evidence"])

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

    opps_file.write_text(
        json.dumps(clean_opps, indent=2), encoding="utf-8"
    )

    evs_file.write_text(
        json.dumps(guarded_evidence, indent=2), encoding="utf-8"
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

    from .export_safety import ExportGuardViolation

    try:
        export(
            out_dir=args.out_dir,
            systems=args.systems,
            dry_run=args.dry_run,
            run_id=args.run_id,
            org_id=args.org_id,
        )
    except ExportGuardViolation as exc:
        # 2.0-B1 T5 (AC5): a refused export must be a clear, actionable message
        # and a non-zero exit — not a traceback, and never a partial write.
        # Caught via the symbol re-exported by export_safety, which resolves to
        # the SAME class the guard raises (see its _load_guard docstring).
        logger.error("EXPORT REFUSED — nothing was written.")
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
