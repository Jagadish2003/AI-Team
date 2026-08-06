#!/usr/bin/env python3
"""2.0-B1 / T4 (AC4) — CLI to VERIFY a signed AgentIQ evidence-export bundle.

The third-party counterpart to the export routes. An auditor, regulator, or
reviewer who has been handed a bundle file plus the installation's license
``report_key`` can confirm — offline, without any access to the deployment —
that the bundle is intact and was produced by that installation.

Deliberately DEPENDENCY-FREE and self-contained: it re-implements the canonical
serialisation and HMAC verification in ~30 lines using only the Python standard
library, so it can be handed out alongside a bundle and audited by eye. It does
NOT import the ``app`` package (that would require the whole backend, a database,
and a licence to be installed just to check a file). A test in
``tests/unit/test_r2_0_b1_t4_evidence_export.py`` pins this reimplementation
against the product's own signer so the two can never drift.

Usage:

    python scripts/verify_evidence_export.py bundle.json --report-key <key>
    REPORT_KEY=<key> python scripts/verify_evidence_export.py bundle.json

Exit code 0 when the bundle verifies, 1 when it does not (or cannot be read) —
so it composes in CI or a review script. Any altered byte of the bundle fails.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys

SIGNATURE_ALGORITHM = "HMAC-SHA256"


def canonical_bytes(body: dict) -> bytes:
    """The exact bytes that were signed: sorted keys, compact separators, UTF-8.
    Must match app/usage_report.py::canonical_bytes byte for byte."""
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def expected_signature(body: dict, report_key: str) -> str:
    return hmac.new(
        report_key.encode("utf-8"), canonical_bytes(body), hashlib.sha256
    ).hexdigest()


def fold(prev_root: str, this_hash: str) -> str:
    return hashlib.sha256(f"{prev_root}\n{this_hash}".encode("utf-8")).hexdigest()


def verify(envelope: dict, report_key: str) -> dict:
    """Return a verdict dict mirroring app/evidence_export.verify_export_envelope."""
    problems = []
    body = envelope.get("bundle")
    signature = envelope.get("signature")
    algorithm = envelope.get("algorithm")

    if not isinstance(body, dict):
        return {"verified": False, "problems": ["envelope carries no bundle object"]}
    if not isinstance(signature, str) or not signature:
        return {"verified": False, "problems": ["envelope carries no signature"]}
    if algorithm != SIGNATURE_ALGORITHM:
        return {
            "verified": False,
            "problems": [f"unexpected algorithm {algorithm!r} (expected {SIGNATURE_ALGORITHM})"],
        }

    signature_valid = hmac.compare_digest(expected_signature(body, report_key), signature)
    if not signature_valid:
        problems.append(
            "SIGNATURE MISMATCH - the bundle has been altered since it was exported, "
            "or the report key is wrong"
        )

    # Re-fold the integrity chain to localise WHICH record changed.
    integrity = body.get("integrity") if isinstance(body.get("integrity"), dict) else None
    integrity_ok = False
    if integrity is None:
        problems.append("bundle carries no integrity block")
    else:
        root = ""
        integrity_ok = True
        for entry in integrity.get("records") or []:
            if not isinstance(entry, dict):
                problems.append("integrity block contains a malformed record")
                integrity_ok = False
                break
            root = fold(root, str(entry.get("content_hash") or ""))
            if str(entry.get("chain_hash") or "") != root:
                problems.append(
                    "integrity chain breaks at record "
                    f"{entry.get('kind')}/{entry.get('record_id')}"
                )
                integrity_ok = False
                break
        if integrity_ok and root != str(integrity.get("content_root") or ""):
            problems.append("integrity block does not re-fold to its recorded content_root")
            integrity_ok = False

    return {
        "verified": bool(signature_valid and integrity_ok),
        "signature_valid": signature_valid,
        "integrity_consistent": integrity_ok,
        "problems": problems,
    }


def _summarise(body: dict) -> str:
    provenance = body.get("run_provenance") or {}
    integrity = body.get("integrity") or {}
    return (
        f"  scope           : {body.get('scope')}\n"
        f"  run             : {body.get('run_id')}\n"
        f"  opportunity     : {body.get('opportunity_id')}\n"
        f"  findings        : {body.get('finding_count')}\n"
        f"  pack            : {provenance.get('pack_id')} v{provenance.get('pack_version')}\n"
        f"  generated at    : {body.get('generated_at')}\n"
        f"  records covered : {integrity.get('record_count')}\n"
        f"  content root    : {integrity.get('content_root')}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a signed AgentIQ evidence-export bundle (offline)."
    )
    parser.add_argument("bundle", help="Path to the exported bundle JSON file.")
    parser.add_argument(
        "--report-key",
        default=os.getenv("REPORT_KEY", ""),
        help="The installation's license report_key (or set REPORT_KEY).",
    )
    args = parser.parse_args(argv)

    if not args.report_key:
        print(
            "ERROR: no report key supplied - pass --report-key or set REPORT_KEY.",
            file=sys.stderr,
        )
        return 1

    try:
        with open(args.bundle, "rb") as handle:
            envelope = json.loads(handle.read().decode("utf-8"))
    except FileNotFoundError:
        print(f"ERROR: no such file: {args.bundle}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — unreadable input is "not verified".
        print(f"ERROR: {args.bundle} is not valid UTF-8 JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(envelope, dict):
        print("ERROR: bundle is not a JSON object.", file=sys.stderr)
        return 1

    verdict = verify(envelope, args.report_key)
    body = envelope.get("bundle") if isinstance(envelope.get("bundle"), dict) else {}

    if verdict["verified"]:
        print("VERIFIED - the bundle is intact and signed by this installation.")
        print(_summarise(body))
        return 0

    print("NOT VERIFIED - do not rely on this bundle.", file=sys.stderr)
    for problem in verdict["problems"]:
        print(f"  - {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
