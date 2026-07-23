#!/usr/bin/env python3
"""LIC-1 (AT-342 / T1) — CloudFulcrum-internal license issuing CLI.

Local-signing variant (matches the LIC-1 design §3): this tool loads the
CloudFulcrum **private** Ed25519 key, assembles the signed payload, and prints
the license key string the customer pastes into AgentIQ. No network call is made
and no online activation exists — issuing is fully offline.

This directory (``backend/license/``) is CloudFulcrum-internal and is excluded
from the customer build/Docker image (AC10): it sits inside the backend build
context but ``backend/.dockerignore`` excludes ``license/``, so ``COPY . .``
never copies it into the image.

Key custody (AC10 / threat model): the private key is the single root secret of
the whole scheme. It lives only on the secured CloudFulcrum signing host /
secrets manager and is git-ignored here (``*.pem``). Generate it with
``generate_keypair.py`` (T2). If it leaks, every issued key is forgeable — rotate
per backend/license/README.md.

Usage (run from the repo root):
  python backend/license/generate_license.py \
    --customer 'City National Bank' \
    --license-id cnb-2026-001 \
    --term-months 12 \
    --grace-days 14 \
    --private-key backend/license/cloudfulcrum_private.pem

The signed key string is the ONLY thing printed to stdout, so it can be piped or
copied cleanly; everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ALLOWED_TERMS = (3, 6, 12)
DEFAULT_PRIVATE_KEY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cloudfulcrum_private.pem"
)

# R-1.9.1-L1 / T1 (AT-687): payload schema version. v2 adds org binding
# (``org_id``), the signing-key selector (``kid``), ``deployment_type``, and the
# opaque per-installation ``report_key``. It is stamped into every issued payload
# so the verifier (T2–T4) can tell a v2 key (has org_id + kid) from a v1-shaped
# one and reject the latter as ``unsupported_payload_version``. The window to do
# this cleanly is now — before any real customer key is in the field.
PAYLOAD_VERSION = 2

# The two recognised deployment topologies (payload v2). ``saas`` is the
# CloudFulcrum-hosted multi-tenant offering; ``customer_hosted`` is an on-prem /
# customer-cloud install. Parsed and surfaced in license status (AC5) and, in L2,
# stamped into run/telemetry context.
DEPLOYMENT_TYPES = ("saas", "customer_hosted")
DEFAULT_DEPLOYMENT_TYPE = "saas"

# The default signing-key identifier. The trusted public keys become a keyed set
# in T3 (kid → public key); this is the id of the current signing key. Overriding
# it per issue lets key rotation be a config change, not a binary release.
DEFAULT_KID = "cf-2026-1"


def build_payload(
    customer: str,
    license_id: str,
    term_months: int,
    grace_days: int = 14,
    *,
    max_systems: int | None = None,
    org_name: str | None = None,
    org_id: str | None = None,
    kid: str = DEFAULT_KID,
    deployment_type: str = DEFAULT_DEPLOYMENT_TYPE,
    report_key: str | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Assemble the full signed payload (schema v2 — R-1.9.1-L1 / T1).

    ``issued_at`` is today and ``expires_at`` is today + term_months*30 days, so
    the term boundary is baked into the signed payload (the customer cannot move
    it without breaking the signature).

    ``limits`` were reserved (null) in v1 — present from day one so seat/pack
    limits could be enforced later without changing the key format. R17-D4
    Addendum A (Scoped Activation) activates ``limits.max_systems``: the number
    of systems (connected Integration-Hub entities) the deployment may connect.
    It stays ``None`` by default so keys issued before the addendum remain valid
    and unlimited (AC13) — the enforcement is opt-in per key, and no key-format
    change is involved.

    ``org_name`` (R17-D4 Addendum A §2) is the customer-facing display name shown
    across the UI once the key is installed (header, workspace labels, reports,
    License page — resolved in ONE place server-side, see
    ``app.org_display_name``). It defaults to ``customer`` when not given, so the
    display name is always populated and sensible.

    Payload v2 (R-1.9.1-L1 / T1) adds four fields, purely additively — every
    existing field above is unchanged:

      * ``payload_version`` — ``2``. Lets the verifier reject v1-shaped payloads
        (no ``org_id`` / ``kid``) as ``unsupported_payload_version`` (T4).
      * ``org_id`` — the installation org this license is bound to. A key whose
        ``org_id`` does not match the installation org validates as
        ``org_mismatch`` (T2). Defaults to ``customer`` when not given so the
        field is always populated for a v2 key.
      * ``kid`` — key identifier selecting which trusted public key verifies this
        license, so signing-key rotation is a config change, not a binary
        update (T3).
      * ``deployment_type`` — ``saas`` | ``customer_hosted``. Parsed and exposed
        in license status, and (in L2) stamped into run/telemetry context (AC5).
      * ``report_key`` — the per-installation usage-report signing key, consumed
        by R-1.9.1-L2. Opaque to this scheme (carried, never interpreted here).
    """
    issued = today or datetime.date.today()
    expires = issued + datetime.timedelta(days=term_months * 30)
    if deployment_type not in DEPLOYMENT_TYPES:
        raise ValueError(
            f"deployment_type must be one of {DEPLOYMENT_TYPES}, got {deployment_type!r}"
        )
    return {
        "payload_version": PAYLOAD_VERSION,
        "customer": customer,
        "org_name": org_name or customer,
        "org_id": org_id or customer,
        "kid": kid,
        "deployment_type": deployment_type,
        "report_key": report_key,
        "license_id": license_id,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {
            "max_systems": max_systems,
            "max_workspaces": None,
            "enabled_packs": None,
        },
    }


def encode_payload(payload: dict) -> str:
    """Canonical payload encoding: base64(json with sorted keys).

    Must match the verifier's expectation exactly (``app.licensing`` decodes this
    same string), so the canonical JSON is produced with ``sort_keys=True``.
    """
    return base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()


def sign_payload(payload: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign a payload and return the ``base64(payload).base64(signature)`` key.

    The signature is over the ``base64(payload)`` bytes — the exact bytes the
    app verifies in ``verify_license_signature``.
    """
    payload_b64 = encode_payload(payload)
    sig_b64 = base64.b64encode(private_key.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Load the CloudFulcrum private signing key from a PEM file."""
    with open(path, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError(f"{path} is not an Ed25519 private key")
    return key


def generate(
    customer: str,
    license_id: str,
    term_months: int,
    private_key_pem_path: str,
    grace_days: int = 14,
    max_systems: int | None = None,
    org_name: str | None = None,
    org_id: str | None = None,
    kid: str = DEFAULT_KID,
    deployment_type: str = DEFAULT_DEPLOYMENT_TYPE,
    report_key: str | None = None,
) -> str:
    """End-to-end: build payload, load the private key, return the signed key."""
    payload = build_payload(
        customer,
        license_id,
        term_months,
        grace_days,
        max_systems=max_systems,
        org_name=org_name,
        org_id=org_id,
        kid=kid,
        deployment_type=deployment_type,
        report_key=report_key,
    )
    private_key = load_private_key(private_key_pem_path)
    return sign_payload(payload, private_key)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Issue a signed AgentIQ license key with the CloudFulcrum private key. "
            "R-1.9.1-L3: issuance is gated and logged — --contract-ref, --org-id and "
            "--issued-by are required, and every issue writes the license registry + "
            "append-only audit ledger (see backend/license/registry.py)."
        )
    )
    parser.add_argument("--customer", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--term-months", type=int, required=True, choices=ALLOWED_TERMS)
    parser.add_argument("--grace-days", type=int, default=14)
    parser.add_argument(
        "--contract-ref",
        required=True,
        help=(
            "R-1.9.1-L3 (AC1): the contract this license is issued under. Required — "
            "issuance is refused without it."
        ),
    )
    parser.add_argument(
        "--issued-by",
        required=True,
        help="R-1.9.1-L3: the operator issuing this license (recorded in the audit ledger).",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="R-1.9.1-L3: optional free-text note recorded on the registry row + audit entry.",
    )
    parser.add_argument(
        "--max-systems",
        type=int,
        default=None,
        help=(
            "R17-D4 Addendum A: number of systems (connected Integration-Hub "
            "entities) the deployment may connect. Omit for an unlimited license."
        ),
    )
    parser.add_argument(
        "--org-name",
        default=None,
        help=(
            "R17-D4 Addendum A §2: customer-facing organisation display name shown "
            "across the UI once the key is installed. Defaults to --customer when "
            "omitted."
        ),
    )
    parser.add_argument(
        "--org-id",
        required=True,
        help=(
            "R-1.9.1-L1 (payload v2) / R-1.9.1-L3 (AC1): the installation org this "
            "license is bound to. A key whose org_id does not match the installation "
            "org is rejected as org_mismatch. Required — issuance is refused without it."
        ),
    )
    parser.add_argument(
        "--kid",
        default=DEFAULT_KID,
        help=(
            "R-1.9.1-L1 (payload v2): key identifier selecting which trusted public "
            f"key verifies this license. Defaults to {DEFAULT_KID!r}."
        ),
    )
    parser.add_argument(
        "--deployment-type",
        default=DEFAULT_DEPLOYMENT_TYPE,
        choices=DEPLOYMENT_TYPES,
        help=(
            "R-1.9.1-L1 (payload v2): deployment topology (saas | customer_hosted). "
            "Parsed and exposed in license status. Defaults to "
            f"{DEFAULT_DEPLOYMENT_TYPE!r}."
        ),
    )
    parser.add_argument(
        "--report-key",
        default=None,
        help=(
            "R-1.9.1-L1 (payload v2): per-installation usage-report signing key, "
            "consumed by R-1.9.1-L2. Opaque here; carried in the signed payload."
        ),
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help=(
            "Path to the CloudFulcrum private key PEM. Defaults to "
            "LICENSE_SIGNING_KEY_PATH (managed secrets store), then the git-ignored "
            "dev key. Key material is never read from an env var (AC5)."
        ),
    )
    args = parser.parse_args(argv)

    # Route through the issuance service so this historical entrypoint is gated
    # (contract_ref/org_id/issued_by) and every issue writes the registry +
    # append-only audit ledger (R-1.9.1-L3). Imported lazily to avoid a module
    # import cycle (issuance imports the signer helpers from this module).
    import issuance  # noqa: E402  (path set up on import)

    try:
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
            org_name=args.org_name,
            report_key=args.report_key,
            notes=args.notes,
            private_key_path=args.private_key,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Only the signed key on stdout (pipe/copy clean); the audit id to stderr.
    print(f"issued license {args.license_id} (audit {result['audit_id']})", file=sys.stderr)
    print(result["key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
