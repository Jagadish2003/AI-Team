#!/usr/bin/env python3
"""R-1.9.1-L2 / T3 (AT-695) — CLI to generate the signed usage report.

The offline counterpart to ``GET /api/usage/report``: it builds the SAME signed
report envelope for a period and prints it to stdout so a customer can pipe it to
a file and send it to CloudFulcrum. Fully local — no network call is ever made
(the federal no-phone-home posture); billability is derived by CloudFulcrum from
the report, never decided here.

Usage (from backend/, with the venv active and .env pointing at the DB):

    python scripts/generate_usage_report.py --org-id default \
        --from 2026-07-01 --to 2026-07-31 > usage-2026-07.json

The signed JSON envelope is the ONLY thing printed to stdout, so it pipes/copies
cleanly; diagnostics go to stderr. Exit code 0 on success, 1 on error (e.g. the
installed license carries no report_key, or a malformed period).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This script lives in backend/scripts/ but imports the `app` package under
# backend/. Ensure backend/ is on sys.path regardless of how it is launched.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.usage_report import UsageReportError, generate_signed_report  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the signed AgentIQ usage report for a period (offline)."
    )
    parser.add_argument(
        "--org-id",
        default="default",
        help="The installation org the report is for (default: 'default').",
    )
    parser.add_argument(
        "--from",
        dest="period_from",
        required=True,
        help="Period start, inclusive (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--to",
        dest="period_to",
        required=True,
        help="Period end, inclusive (YYYY-MM-DD).",
    )
    args = parser.parse_args(argv)

    try:
        envelope = generate_signed_report(args.org_id, args.period_from, args.period_to)
    except UsageReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure cleanly
        print(f"ERROR: could not generate usage report: {exc}", file=sys.stderr)
        return 1

    # The signed envelope is the only thing on stdout, so it can be redirected.
    print(json.dumps(envelope, sort_keys=True, indent=2))
    print(
        f"Usage report for org={args.org_id} period "
        f"{args.period_from}..{args.period_to} "
        f"({envelope['report']['event_count']} billing events) — "
        "send this file to CloudFulcrum.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
