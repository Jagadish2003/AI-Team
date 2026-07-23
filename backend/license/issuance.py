#!/usr/bin/env python3
"""R-1.9.1-L3 — CloudFulcrum vendor-side license issuance service.

The single path every license write goes through (AT-718 / T4). Whether a key is
minted by the CLI (``generate_license.py``) or a future internal caller, it comes
through ``issue_license`` / ``renew_license`` / ``regenerate_license`` here, so:

* **Contract gate (AT-719 / T5, AC1):** issuance REQUIRES ``contract_ref`` and
  ``org_id`` (and ``issued_by`` — the "who") and refuses without them. No key is
  signed and no registry row is written on a refused request.
* **Payload-v2 signing (AT-720 / T6):** the signed key is produced by the L1
  payload-v2 signer (``generate_license.build_payload`` + ``sign_payload``) — the
  only key format this service emits.
* **Registry + append-only audit, together (AT-721 / T7):** every write inserts a
  ``license_registry`` row AND an ``issuance_audit`` entry in ONE transaction, so
  a minted key can never exist without its ledger record (AC1, AC2).
* **Renewal (AT-750 / T8, AC3):** ``renew_license`` links the new row to the
  original via ``supersedes``, inherits customer/org, and flags term changes for
  review.
* **Deployment-fee tracking (AT-752 / T10):** the row carries ``deployment_type``
  and a ``deployment_fee_collected`` flag/date (set via ``registry``).
* **Parallel kids (AT-753 / T11, AC6):** ``kid`` is a per-issue parameter, so two
  active signing keys can issue side by side during a rotation window; the
  registry records which ``kid`` signed each license.

**Key custody (AT-716 / T2, AC5):** the private signing key is read ONLY from a
filesystem path — an explicit argument, else ``LICENSE_SIGNING_KEY_PATH`` (which
points at the managed secrets store mounted into the ops environment), else the
git-ignored dev default under ``backend/license/``. Key MATERIAL is never read
from an environment variable and never lives in the repository (enforced by the
AC5 repo scan). This module only ever opens a path; it cannot be handed raw key
bytes.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from typing import Any, Dict, List, Optional

# backend/ (for app.db) and this dir (for sibling imports) on the path — same
# pattern as verify_license.py / dev_mint_test_keys.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))
sys.path.insert(0, _HERE)

from app import db  # noqa: E402
import registry  # noqa: E402
from generate_license import (  # noqa: E402
    DEFAULT_DEPLOYMENT_TYPE,
    DEFAULT_KID,
    DEFAULT_PRIVATE_KEY,
    DEPLOYMENT_TYPES,
    build_payload,
    load_private_key,
    sign_payload,
)

# The env var that names WHERE the signing key lives (a path into the managed
# secrets store) — NOT the key material itself. Reading key bytes from the
# environment is deliberately impossible here (AC5).
SIGNING_KEY_PATH_ENV = "LICENSE_SIGNING_KEY_PATH"


class IssuanceError(RuntimeError):
    """Raised when an issuance request is refused (missing gate fields, unknown
    license to renew, or a custody/signing problem)."""


# ---------------------------------------------------------------------------
# Key custody (AT-716 / T2, AC5).
# ---------------------------------------------------------------------------
def resolve_signing_key_path(explicit: Optional[str] = None) -> str:
    """Resolve the private signing key PATH — never key material.

    Precedence: explicit argument -> ``LICENSE_SIGNING_KEY_PATH`` (the managed
    secrets-store path) -> the git-ignored dev default under ``backend/license/``.
    The returned value is a filesystem path; the caller opens it. There is no code
    path that accepts the key bytes from an environment variable (AC5).
    """
    path = explicit or os.getenv(SIGNING_KEY_PATH_ENV) or DEFAULT_PRIVATE_KEY
    if not os.path.isfile(path):
        raise IssuanceError(
            f"signing key not found at {path!r}. Point {SIGNING_KEY_PATH_ENV} at the "
            "key in the managed secrets store, or pass an explicit path. The private "
            "key must never live in the repo or an env var (see backend/license/README.md)."
        )
    return path


# ---------------------------------------------------------------------------
# Issuance (AT-718 / T4, AT-719 / T5, AT-720 / T6, AT-721 / T7).
# ---------------------------------------------------------------------------
def _require(field: str, value: Optional[str]) -> str:
    if value is None or str(value).strip() == "":
        raise IssuanceError(
            f"issuance refused: {field} is required. Every issued license must be "
            "tied to a contract and an installation org, with a named issuer."
        )
    return str(value).strip()


def issue_license(
    *,
    customer: str,
    license_id: str,
    org_id: str,
    contract_ref: str,
    issued_by: str,
    term_months: int,
    kid: str = DEFAULT_KID,
    deployment_type: str = DEFAULT_DEPLOYMENT_TYPE,
    grace_days: int = 14,
    max_systems: Optional[int] = None,
    org_name: Optional[str] = None,
    report_key: Optional[str] = None,
    notes: Optional[str] = None,
    supersedes: Optional[str] = None,
    audit_action: str = registry.ACTION_ISSUE,
    private_key_path: Optional[str] = None,
    conn=None,
) -> Dict[str, Any]:
    """Issue a payload-v2 license key, writing a registry row + audit entry atomically.

    Refuses (``IssuanceError``) unless ``customer``, ``license_id``, ``org_id``,
    ``contract_ref`` and ``issued_by`` are all present (AC1). Returns
    ``{key, license_id, payload, audit_id}``.
    """
    customer = _require("customer", customer)
    license_id = _require("license_id", license_id)
    org_id = _require("org_id", org_id)
    contract_ref = _require("contract_ref", contract_ref)
    issued_by = _require("issued_by", issued_by)
    if deployment_type not in DEPLOYMENT_TYPES:
        raise IssuanceError(
            f"deployment_type must be one of {DEPLOYMENT_TYPES}, got {deployment_type!r}"
        )

    # Sign with the L1 payload-v2 signer. Build the payload first so the exact
    # signed terms (expires_at, kid, …) are what we record in the registry.
    key_path = resolve_signing_key_path(private_key_path)
    payload = build_payload(
        customer,
        license_id,
        term_months,
        grace_days,
        max_systems=max_systems,
        org_name=org_name or customer,
        org_id=org_id,
        kid=kid,
        deployment_type=deployment_type,
        report_key=report_key,
    )
    priv = load_private_key(key_path)
    key_string = sign_payload(payload, priv)

    # Registry row + audit entry commit together (AC1/AC2): a minted key never
    # exists without its ledger record.
    with closing(db.connect()) as c:
        try:
            registry.insert_registry_row(
                license_id=license_id,
                customer=customer,
                org_id=org_id,
                contract_ref=contract_ref,
                deployment_type=deployment_type,
                expires_at=payload["expires_at"],
                kid=kid,
                issued_by=issued_by,
                license_key=key_string,
                max_systems=max_systems,
                grace_days=grace_days,
                status=registry.STATUS_ACTIVE,
                supersedes=supersedes,
                notes=notes,
                payload_version=payload["payload_version"],
                conn=c,
            )
            audit_id = registry.append_audit(
                license_id=license_id,
                action=audit_action,
                actor=issued_by,
                customer=customer,
                org_id=org_id,
                contract_ref=contract_ref,
                kid=kid,
                deployment_type=deployment_type,
                max_systems=max_systems,
                expires_at=payload["expires_at"],
                grace_days=grace_days,
                supersedes=supersedes,
                notes=notes,
                conn=c,
            )
            c.commit()
        except Exception:
            c.rollback()
            raise

    return {
        "key": key_string,
        "license_id": license_id,
        "payload": payload,
        "audit_id": audit_id,
    }


# ---------------------------------------------------------------------------
# Renewal (AT-750 / T8, AC3).
# ---------------------------------------------------------------------------
def _diff_terms(original: Dict[str, Any], *, max_systems, grace_days, deployment_type) -> Dict[str, Any]:
    """Return a {field: [old, new]} map of terms that changed on renewal."""
    changes: Dict[str, Any] = {}
    if original.get("max_systems") != max_systems:
        changes["max_systems"] = [original.get("max_systems"), max_systems]
    if original.get("grace_days") != grace_days:
        changes["grace_days"] = [original.get("grace_days"), grace_days]
    if original.get("deployment_type") != deployment_type:
        changes["deployment_type"] = [original.get("deployment_type"), deployment_type]
    return changes


def renew_license(
    *,
    supersedes_license_id: str,
    license_id: str,
    issued_by: str,
    term_months: int,
    contract_ref: Optional[str] = None,
    kid: Optional[str] = None,
    deployment_type: Optional[str] = None,
    grace_days: Optional[int] = None,
    max_systems: Optional[int] = None,
    report_key: Optional[str] = None,
    notes: Optional[str] = None,
    private_key_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Renew an existing license: mint a new key linked to the original via
    ``supersedes``, inheriting customer/org, and flag term changes for review (AC3).

    Any term not supplied is inherited from the original row. On success the
    original row is marked ``superseded`` and the new row is ``active``. The
    returned dict includes ``term_changes`` (a {field: [old, new]} map) for the
    operator to review.
    """
    original = registry.get_license(supersedes_license_id)
    if original is None:
        raise IssuanceError(
            f"cannot renew: no registry row for license_id {supersedes_license_id!r}."
        )

    # Inherit customer/org from the original; other terms inherit unless overridden.
    customer = original["customer"]
    org_id = original["org_id"]
    contract_ref = contract_ref or original["contract_ref"]
    kid = kid or original["kid"]
    deployment_type = deployment_type or original["deployment_type"]
    grace_days = original["grace_days"] if grace_days is None else grace_days
    max_systems = original["max_systems"] if max_systems is None else max_systems

    term_changes = _diff_terms(
        original, max_systems=max_systems, grace_days=grace_days, deployment_type=deployment_type
    )
    renewal_note = notes or ""
    if term_changes:
        flag = "TERM CHANGES FOR REVIEW: " + "; ".join(
            f"{k}: {v[0]} -> {v[1]}" for k, v in term_changes.items()
        )
        renewal_note = (renewal_note + " | " + flag).strip(" |")

    # issue_license writes the new row (status active, supersedes set) + a renew
    # audit entry atomically; then mark the original superseded.
    result = issue_license(
        customer=customer,
        license_id=license_id,
        org_id=org_id,
        contract_ref=contract_ref,
        issued_by=issued_by,
        term_months=term_months,
        kid=kid,
        deployment_type=deployment_type,
        grace_days=grace_days,
        max_systems=max_systems,
        org_name=original.get("customer"),
        report_key=report_key,
        notes=renewal_note or None,
        supersedes=supersedes_license_id,
        audit_action=registry.ACTION_RENEW,
        private_key_path=private_key_path,
    )
    registry.mark_superseded(supersedes_license_id)

    result["term_changes"] = term_changes
    result["supersedes"] = supersedes_license_id
    return result


# ---------------------------------------------------------------------------
# Regeneration — re-emit a key for an existing registry row (audited).
# ---------------------------------------------------------------------------
def regenerate_license(
    *,
    license_id: str,
    issued_by: str,
    kid: Optional[str] = None,
    private_key_path: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-sign the key for an existing license (same terms), recording a
    ``regenerate`` audit entry. Used when a customer loses the key string or a
    signing key is rotated without changing terms. The stored ``license_key`` and
    ``kid`` are refreshed; a new audit entry is appended (the audit ledger is
    never rewritten — AC2).
    """
    original = registry.get_license(license_id)
    if original is None:
        raise IssuanceError(
            f"cannot regenerate: no registry row for license_id {license_id!r}."
        )
    kid = kid or original["kid"]
    key_path = resolve_signing_key_path(private_key_path)

    # Rebuild the payload with the original's terms, then overwrite the dates with
    # the stored ones so regeneration preserves the exact expiry (term_months here
    # is a placeholder — expires_at is set to the stored value immediately after).
    payload = build_payload(
        original["customer"],
        license_id,
        12,  # placeholder term; expires_at is overwritten to the stored value below
        original["grace_days"],
        max_systems=original["max_systems"],
        org_name=original["customer"],
        org_id=original["org_id"],
        kid=kid,
        deployment_type=original["deployment_type"],
    )
    payload["expires_at"] = (
        original["expires_at"].isoformat()
        if hasattr(original["expires_at"], "isoformat")
        else str(original["expires_at"])
    )
    priv = load_private_key(key_path)
    key_string = sign_payload(payload, priv)

    with closing(db.connect()) as c:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE license_registry SET license_key = %s, kid = %s WHERE license_id = %s",
                    (key_string, kid, license_id),
                )
            audit_id = registry.append_audit(
                license_id=license_id,
                action=registry.ACTION_REGENERATE,
                actor=issued_by,
                customer=original["customer"],
                org_id=original["org_id"],
                contract_ref=original["contract_ref"],
                kid=kid,
                deployment_type=original["deployment_type"],
                max_systems=original["max_systems"],
                expires_at=payload["expires_at"],
                grace_days=original["grace_days"],
                notes=notes,
                conn=c,
            )
            c.commit()
        except Exception:
            c.rollback()
            raise

    return {"key": key_string, "license_id": license_id, "payload": payload, "audit_id": audit_id}


# ---------------------------------------------------------------------------
# Deployment-fee tracking (AT-752 / T10) — thin pass-through to the registry.
# ---------------------------------------------------------------------------
def record_deployment_fee(license_id: str, *, collected: bool = True) -> None:
    registry.set_deployment_fee_collected(license_id, collected=collected)


# ---------------------------------------------------------------------------
# Read helpers surfaced for the CLI (AT-751 / T9 expiry, AC3 lineage).
# ---------------------------------------------------------------------------
def expiring_within(days: int) -> List[Dict[str, Any]]:
    return registry.expiring_within(days)


def license_lineage(license_id: str) -> List[Dict[str, Any]]:
    return registry.license_lineage(license_id)
