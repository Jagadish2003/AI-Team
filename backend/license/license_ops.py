#!/usr/bin/env python3
"""R-1.9.1-L3 — CloudFulcrum vendor-side license ops CLI.

The operator-facing wrapper around the issuance service (``issuance.py``) and the
registry (``registry.py``). Every write goes through the service, so the registry
and its append-only audit ledger are always written. CloudFulcrum-internal; lives
under ``backend/license/`` (excluded from the customer image).

Subcommands:

  issue        Issue a new payload-v2 license (gated on contract-ref/org-id/issued-by).
  renew        Renew an existing license, linking the new key via supersedes.
  regenerate   Re-emit the key for an existing license (same terms), audited.
  expiring     List active licenses expiring within N days (proactive-renewal list).
  list         List licenses for a customer or org.
  lineage      Show a license's renewal lineage (the supersedes chain).
  fee          Record the deployment-fee status for a license.

Connection: the standard ``DATABASE_URL`` from ``backend/.env`` (the ops registry
database). Signing key: a path only — ``--private-key`` or ``LICENSE_SIGNING_KEY_PATH``
(the managed secrets store), never key material from the environment (AC5).

Usage (run from the repo root):
  python backend/license/license_ops.py issue --customer 'City National Bank' \
      --license-id cnb-2026-001 --org-id cnb --contract-ref CTR-4471 \
      --issued-by ganesh --term-months 12
  python backend/license/license_ops.py renew --supersedes cnb-2026-001 \
      --license-id cnb-2027-001 --issued-by ganesh --term-months 12
  python backend/license/license_ops.py expiring --days 30
  python backend/license/license_ops.py lineage --license-id cnb-2027-001
  python backend/license/license_ops.py fee --license-id cnb-2026-001
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
sys.path.insert(0, _HERE)

import issuance  # noqa: E402
import registry  # noqa: E402
from generate_license import ALLOWED_TERMS, DEFAULT_DEPLOYMENT_TYPE, DEFAULT_KID, DEPLOYMENT_TYPES  # noqa: E402


def _print_row(row: dict) -> None:
    """Print a registry row as a one-line, secret-free summary (never the key)."""
    fee = "fee:yes" if row.get("deployment_fee_collected") else "fee:no"
    print(
        f"{row['license_id']}  {row['customer']} (org={row['org_id']})  "
        f"{row['status']}  expires={row['expires_at']}  kid={row['kid']}  "
        f"contract={row['contract_ref']}  supersedes={row.get('supersedes')}  {fee}"
    )


def _cmd_issue(args) -> int:
    result = issuance.issue_license(
        customer=args.customer,
        license_id=args.license_id,
        org_id=args.org_id,
        contract_ref=args.contract_ref,
        issued_by=args.issued_by,
        term_months=args.term_months,
        kid=args.kid,
        deployment_type=args.deployment_type,
        grace_days=args.grace_days,
        max_systems=args.max_systems,
        notes=args.notes,
        private_key_path=args.private_key,
    )
    print(f"issued {args.license_id} (audit {result['audit_id']})", file=sys.stderr)
    print(result["key"])
    return 0


def _cmd_renew(args) -> int:
    result = issuance.renew_license(
        supersedes_license_id=args.supersedes,
        license_id=args.license_id,
        issued_by=args.issued_by,
        term_months=args.term_months,
        contract_ref=args.contract_ref,
        kid=args.kid,
        deployment_type=args.deployment_type,
        grace_days=args.grace_days,
        max_systems=args.max_systems,
        notes=args.notes,
        private_key_path=args.private_key,
    )
    if result["term_changes"]:
        print(
            "TERM CHANGES FOR REVIEW: " + json.dumps(result["term_changes"]),
            file=sys.stderr,
        )
    print(
        f"renewed {args.supersedes} -> {args.license_id} (audit {result['audit_id']})",
        file=sys.stderr,
    )
    print(result["key"])
    return 0


def _cmd_regenerate(args) -> int:
    result = issuance.regenerate_license(
        license_id=args.license_id,
        issued_by=args.issued_by,
        kid=args.kid,
        private_key_path=args.private_key,
        notes=args.notes,
    )
    print(f"regenerated {args.license_id} (audit {result['audit_id']})", file=sys.stderr)
    print(result["key"])
    return 0


def _cmd_expiring(args) -> int:
    rows = registry.expiring_within(args.days)
    print(f"{len(rows)} active license(s) expiring within {args.days} day(s):", file=sys.stderr)
    for row in rows:
        _print_row(row)
    return 0


def _cmd_list(args) -> int:
    if args.customer:
        rows = registry.list_by_customer(args.customer)
    elif args.org:
        rows = registry.list_by_org(args.org)
    else:
        print("ERROR: pass --customer or --org", file=sys.stderr)
        return 1
    for row in rows:
        _print_row(row)
    return 0


def _cmd_lineage(args) -> int:
    chain = registry.license_lineage(args.license_id)
    print(f"lineage for {args.license_id} ({len(chain)} link(s), oldest first):", file=sys.stderr)
    for row in chain:
        _print_row(row)
    return 0


def _cmd_fee(args) -> int:
    issuance.record_deployment_fee(args.license_id, collected=not args.uncollected)
    state = "not collected" if args.uncollected else "collected"
    print(f"deployment fee for {args.license_id} marked {state}", file=sys.stderr)
    return 0


def _add_signing_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--private-key",
        default=None,
        help=(
            "Path to the CloudFulcrum private signing key. Defaults to "
            "LICENSE_SIGNING_KEY_PATH (managed secrets store), then the git-ignored "
            "dev key. Key material is never read from the environment (AC5)."
        ),
    )
    p.add_argument("--kid", default=DEFAULT_KID, help=f"Signing key id (default {DEFAULT_KID!r}).")
    p.add_argument(
        "--deployment-type",
        default=DEFAULT_DEPLOYMENT_TYPE,
        choices=DEPLOYMENT_TYPES,
    )
    p.add_argument("--grace-days", type=int, default=14)
    p.add_argument("--max-systems", type=int, default=None)
    p.add_argument("--notes", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CloudFulcrum vendor-side license operations.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new license (gated + logged).")
    p_issue.add_argument("--customer", required=True)
    p_issue.add_argument("--license-id", required=True)
    p_issue.add_argument("--org-id", required=True)
    p_issue.add_argument("--contract-ref", required=True)
    p_issue.add_argument("--issued-by", required=True)
    p_issue.add_argument("--term-months", type=int, required=True, choices=ALLOWED_TERMS)
    _add_signing_args(p_issue)
    p_issue.set_defaults(func=_cmd_issue)

    p_renew = sub.add_parser("renew", help="Renew an existing license via supersedes.")
    p_renew.add_argument("--supersedes", required=True, help="license_id being renewed.")
    p_renew.add_argument("--license-id", required=True, help="new license_id.")
    p_renew.add_argument("--issued-by", required=True)
    p_renew.add_argument("--term-months", type=int, required=True, choices=ALLOWED_TERMS)
    p_renew.add_argument("--contract-ref", default=None, help="Override; inherited if omitted.")
    _add_signing_args(p_renew)
    p_renew.set_defaults(func=_cmd_renew)

    p_regen = sub.add_parser("regenerate", help="Re-emit a key for an existing license.")
    p_regen.add_argument("--license-id", required=True)
    p_regen.add_argument("--issued-by", required=True)
    p_regen.add_argument("--private-key", default=None)
    p_regen.add_argument("--kid", default=None, help="Override the stored kid (rotation).")
    p_regen.add_argument("--notes", default=None)
    p_regen.set_defaults(func=_cmd_regenerate)

    p_exp = sub.add_parser("expiring", help="List active licenses expiring within N days.")
    p_exp.add_argument("--days", type=int, required=True)
    p_exp.set_defaults(func=_cmd_expiring)

    p_list = sub.add_parser("list", help="List licenses for a customer or org.")
    p_list.add_argument("--customer", default=None)
    p_list.add_argument("--org", default=None)
    p_list.set_defaults(func=_cmd_list)

    p_lin = sub.add_parser("lineage", help="Show a license's renewal lineage.")
    p_lin.add_argument("--license-id", required=True)
    p_lin.set_defaults(func=_cmd_lineage)

    p_fee = sub.add_parser("fee", help="Record deployment-fee status for a license.")
    p_fee.add_argument("--license-id", required=True)
    p_fee.add_argument(
        "--uncollected",
        action="store_true",
        help="Mark the fee NOT collected (clears the flag/date). Default marks it collected.",
    )
    p_fee.set_defaults(func=_cmd_fee)

    return parser


def main(argv=None) -> int:
    registry.load_ops_env()  # pick up DATABASE_URL (+ LICENSE_* vars) from backend/.env
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (issuance.IssuanceError, registry.RegistryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
