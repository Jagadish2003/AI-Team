"""Issue / verify pack certification signatures — 2.0-C2 T1 (AT-831).

Release tooling for whoever holds the CloudFulcrum pack-certification signing key.
It is deliberately NOT part of the running application: the platform only ever
*verifies* (public key), it never *signs* (private key).

    # Verify every shipped pack's certification against the trusted anchors.
    python scripts/sign_pack_certifications.py --check

    # Print the canonical payload a signature would cover (for review/audit).
    python scripts/sign_pack_certifications.py --show cloud_ops

    # Re-issue signatures after editing certification metadata. The private key
    # comes from secrets management, never from this repository.
    PACK_CERTIFICATION_SIGNING_KEY=<base64 32-byte ed25519 seed> \
        python scripts/sign_pack_certifications.py --sign

    # Mint a fresh signing key (key rotation / a new deployment's own key).
    python scripts/sign_pack_certifications.py --generate-key

``--sign`` prints ``packId -> signature``; paste each value into that pack's
``certification.signature.value`` in ``discovery/packs/pack_config.py``. It does not
rewrite source: a signature landing in the tree should be a reviewed diff, not a
side effect of running a script.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.packs.pack_certification import (  # noqa: E402
    canonical_payload_bytes,
    certification_payload,
    get_pack_certification,
    sign_certification,
)
from discovery.packs.pack_config import (  # noqa: E402
    PACK_REGISTRY,
    get_pack_certification_declaration,
)

#: Base64-encoded 32-byte Ed25519 seed. Supplied from secrets management at issuance
#: time only — this is signing key material and must never be committed or logged.
SIGNING_KEY_ENV_VAR = "PACK_CERTIFICATION_SIGNING_KEY"
DEFAULT_KEY_ID = "cloudfulcrum-pack-signing-2026"


def _generate_key() -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print("PRIVATE SEED (base64) — store in secrets management, never commit:")
    print(f"  {base64.b64encode(seed).decode('ascii')}")
    print("PUBLIC KEY (base64) — add to CLOUDFULCRUM_SIGNING_KEYS:")
    print(f"  {base64.b64encode(public).decode('ascii')}")
    return 0


def _show(pack_id: str) -> int:
    if pack_id not in PACK_REGISTRY:
        print(f"unknown pack '{pack_id}'", file=sys.stderr)
        return 2
    declaration = get_pack_certification_declaration(pack_id)
    payload = certification_payload(pack_id, declaration)
    print(json.dumps(payload, indent=2, sort_keys=True))
    print("\ncanonical bytes:")
    print(canonical_payload_bytes(payload).decode("utf-8"))
    return 0


def _check() -> int:
    failures = 0
    for pack_id in PACK_REGISTRY:
        certification = get_pack_certification(pack_id)
        status = "ok" if not certification.downgraded else "FAILED"
        if certification.downgraded:
            failures += 1
        print(
            f"{status:>6}  {pack_id:<20} declared={certification.declared_level:<10} "
            f"effective={certification.effective_level:<10} "
            f"reviewDue={certification.review_due}"
            + (f"  due={certification.review_due_on}" if certification.review_due_on else "")
        )
        if certification.downgraded:
            print(f"          {certification.downgrade_detail}")
        # 2.0-C2 T5: review-due is NOT a failure — the badge is still valid, it just
        # needs re-reviewing. Reported, never exit-code-failing, so a certification
        # ageing out cannot turn into a red CI build on an unrelated change.
        if certification.review_due:
            print(f"          {certification.review_due_detail}")
    return 1 if failures else 0


def _sign(key_id: str) -> int:
    raw = os.environ.get(SIGNING_KEY_ENV_VAR, "").strip()
    if not raw:
        print(
            f"{SIGNING_KEY_ENV_VAR} is not set — signing requires the release "
            f"private key from secrets management.",
            file=sys.stderr,
        )
        return 2
    seed = base64.b64decode(raw, validate=True)
    for pack_id in PACK_REGISTRY:
        declaration = get_pack_certification_declaration(pack_id)
        if declaration["level"] == "community":
            print(f"{pack_id}: community — no signature required")
            continue
        signature = sign_certification(
            pack_id, declaration, seed, key_id=key_id
        )
        print(f"{pack_id}: {signature}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="verify shipped signatures")
    group.add_argument("--sign", action="store_true", help="re-issue signatures")
    group.add_argument("--generate-key", action="store_true", help="mint a signing key")
    group.add_argument("--show", metavar="PACK_ID", help="print the canonical payload")
    parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    args = parser.parse_args(argv)

    if args.generate_key:
        return _generate_key()
    if args.show:
        return _show(args.show)
    if args.check:
        return _check()
    return _sign(args.key_id)


if __name__ == "__main__":
    raise SystemExit(main())
