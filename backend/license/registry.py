#!/usr/bin/env python3
"""R-1.9.1-L3 (AT-715 / T1 + AT-717 / T3) — CloudFulcrum vendor-side license registry.

CloudFulcrum-INTERNAL. Like the rest of ``backend/license/`` this module is
excluded from the customer image (``backend/.dockerignore`` drops ``license/``)
and runs only in CloudFulcrum's ops environment. It is the authoritative,
upstream record of every license key CloudFulcrum has *minted* — NOT the
customer-side installed-key store (``app/license_runtime.py`` -> ``org_licenses``),
which is one downstream copy of a single installed key at one customer.

Storage: the ops PostgreSQL database. The schema (``license_registry`` +
``issuance_audit``) is defined once in ``database/models/license_registry.py`` and
provisioned by the ``0026`` alembic migration (CI/Path A) and by
``database/provision/provision.sql`` (the psql-only Path B the ops team runs in
prod). Connections use the standard ``DATABASE_URL`` via the app's pooled
``app.db.connect()`` — the same connection string configured in ``backend/.env``.

Every public function accepts an optional ``conn`` so a caller can run several
writes in one transaction (the issuance service does this so a registry row and
its audit entry commit together); when omitted, each call borrows and releases a
pooled connection itself.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import closing, contextmanager
from typing import Any, Dict, List, Optional

# Make ``backend/`` importable so ``app.db`` resolves when this runs as a script
# (mirrors verify_license.py). When imported inside the app/test process the path
# is already present and this is a no-op.
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND_DIR)

from app import db  # noqa: E402


def load_ops_env() -> None:
    """Load backend/.env so the ops CLIs pick up DATABASE_URL (and any LICENSE_*
    vars) from the standard config file without the operator exporting them —
    mirroring database/provision/provision_schema.py.

    Called from the CLI entry points ONLY (not at import), so importing this
    module inside the app / contract-test process never mutates a hermetically
    controlled environment. ``override=False`` so an already-exported value always
    wins.
    """
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)
from database.models.license_registry import (  # noqa: E402
    ALL_LICENSE_REGISTRY_DDL,
    AUDIT_ACTIONS,
    ACTION_ISSUE,
    ACTION_REGENERATE,
    ACTION_RENEW,
    REGISTRY_STATUSES,
    STATUS_ACTIVE,
    STATUS_REVOKED_AT_NEXT_ROTATION,
    STATUS_SUPERSEDED,
)

__all__ = [
    "RegistryError",
    "ensure_registry_schema",
    "insert_registry_row",
    "append_audit",
    "mark_superseded",
    "set_deployment_fee_collected",
    "get_license",
    "list_by_customer",
    "list_by_org",
    "license_lineage",
    "expiring_within",
    "get_audit_for_license",
    "ACTION_ISSUE",
    "ACTION_RENEW",
    "ACTION_REGENERATE",
    "STATUS_ACTIVE",
    "STATUS_SUPERSEDED",
    "STATUS_REVOKED_AT_NEXT_ROTATION",
]


class RegistryError(RuntimeError):
    """Raised for a registry configuration or integrity error."""


@contextmanager
def _conn_ctx(conn=None):
    """Yield a DB connection.

    If ``conn`` is supplied it is yielded as-is and neither committed nor closed
    here (the caller owns the transaction). Otherwise a pooled connection is
    borrowed via ``db.connect()``, committed on clean exit, and released.
    """
    if conn is not None:
        yield conn
        return
    with closing(db.connect()) as own:
        try:
            yield own
            own.commit()
        except Exception:
            own.rollback()
            raise


def ensure_registry_schema(conn=None) -> None:
    """Create the registry + audit tables, indexes, and append-only rules.

    Idempotent (``IF NOT EXISTS`` / ``CREATE OR REPLACE RULE``). Normally the
    schema already exists (migration / provision.sql); this is here so the ops
    tooling can self-provision against a bare database if needed.
    """
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            for ddl in ALL_LICENSE_REGISTRY_DDL:
                cur.execute(ddl)


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------
def _terms_json(*, max_systems, expires_at, grace_days) -> str:
    return json.dumps(
        {"max_systems": max_systems, "expires_at": expires_at, "grace_days": grace_days},
        sort_keys=True,
    )


def insert_registry_row(
    *,
    license_id: str,
    customer: str,
    org_id: str,
    contract_ref: str,
    deployment_type: str,
    expires_at: str,
    kid: str,
    issued_by: str,
    license_key: str,
    max_systems: Optional[int] = None,
    grace_days: int = 14,
    status: str = STATUS_ACTIVE,
    supersedes: Optional[str] = None,
    notes: Optional[str] = None,
    payload_version: int = 2,
    conn=None,
) -> None:
    """Insert one issued/renewed license row. Fails loudly on a duplicate id."""
    if status not in REGISTRY_STATUSES:
        raise RegistryError(f"invalid status {status!r}; must be one of {REGISTRY_STATUSES}")
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO license_registry (
                    license_id, customer, org_id, contract_ref, deployment_type,
                    max_systems, expires_at, grace_days, kid, issued_by, status,
                    supersedes, notes, payload_version, license_key
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    license_id, customer, org_id, contract_ref, deployment_type,
                    max_systems, expires_at, grace_days, kid, issued_by, status,
                    supersedes, notes, payload_version, license_key,
                ),
            )


def append_audit(
    *,
    license_id: str,
    action: str,
    actor: str,
    customer: Optional[str] = None,
    org_id: Optional[str] = None,
    contract_ref: Optional[str] = None,
    kid: Optional[str] = None,
    deployment_type: Optional[str] = None,
    max_systems: Optional[int] = None,
    expires_at: Optional[str] = None,
    grace_days: Optional[int] = None,
    supersedes: Optional[str] = None,
    notes: Optional[str] = None,
    conn=None,
) -> str:
    """Append one audit entry (who, when, what terms, which contract). Returns its id.

    Append-only: the table's schema-level rules make any later UPDATE/DELETE a
    no-op, so this INSERT is the only mutation an audit entry ever sees (AC2).
    """
    if action not in AUDIT_ACTIONS:
        raise RegistryError(f"invalid audit action {action!r}; must be one of {AUDIT_ACTIONS}")
    audit_id = uuid.uuid4().hex
    terms = _terms_json(max_systems=max_systems, expires_at=expires_at, grace_days=grace_days)
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO issuance_audit (
                    audit_id, license_id, action, actor, customer, org_id,
                    contract_ref, kid, deployment_type, terms, supersedes, notes
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    audit_id, license_id, action, actor, customer, org_id,
                    contract_ref, kid, deployment_type, terms, supersedes, notes,
                ),
            )
    return audit_id


def mark_superseded(license_id: str, conn=None) -> None:
    """Flip a license row to ``superseded`` (used by the renewal flow)."""
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE license_registry SET status = %s WHERE license_id = %s",
                (STATUS_SUPERSEDED, license_id),
            )


def set_deployment_fee_collected(
    license_id: str, *, collected: bool = True, conn=None
) -> None:
    """Record the deployment-fee status for an issued license (finding #10).

    Setting ``collected=True`` stamps ``deployment_fee_collected_at`` with the
    server clock; clearing it nulls the date again.
    """
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                """
                UPDATE license_registry
                   SET deployment_fee_collected = %s,
                       deployment_fee_collected_at = CASE WHEN %s THEN now() ELSE NULL END
                 WHERE license_id = %s
                """,
                (collected, collected, license_id),
            )


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------
def get_license(license_id: str, conn=None) -> Optional[Dict[str, Any]]:
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM license_registry WHERE license_id = %s", (license_id,))
            row = cur.fetchone()
            return dict(row) if row is not None else None


def list_by_customer(customer: str, conn=None) -> List[Dict[str, Any]]:
    """All license rows for a customer, newest first — the lineage view (AC3)."""
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM license_registry WHERE customer = %s ORDER BY issued_at DESC",
                (customer,),
            )
            return [dict(r) for r in cur.fetchall()]


def list_by_org(org_id: str, conn=None) -> List[Dict[str, Any]]:
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM license_registry WHERE org_id = %s ORDER BY issued_at DESC",
                (org_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def license_lineage(license_id: str, conn=None) -> List[Dict[str, Any]]:
    """Return the supersedes-linked chain a license belongs to, oldest first.

    Walks ``supersedes`` back to the original, then forward through every row
    that supersedes a member — so querying any license in a renewal chain shows
    the whole lineage (AC3).
    """
    with _conn_ctx(conn) as c:
        chain: List[Dict[str, Any]] = []
        seen = set()
        # Walk backwards to the root.
        cur_id: Optional[str] = license_id
        while cur_id and cur_id not in seen:
            row = get_license(cur_id, conn=c)
            if row is None:
                break
            seen.add(cur_id)
            chain.append(row)
            cur_id = row.get("supersedes")
        chain.reverse()  # oldest (root) first
        # Walk forwards: append any row that supersedes the current tail.
        tail = chain[-1]["license_id"] if chain else license_id
        with c.cursor() as cur:
            while True:
                cur.execute(
                    "SELECT * FROM license_registry WHERE supersedes = %s ORDER BY issued_at ASC",
                    (tail,),
                )
                nxt = cur.fetchone()
                if nxt is None or nxt["license_id"] in seen:
                    break
                row = dict(nxt)
                chain.append(row)
                seen.add(row["license_id"])
                tail = row["license_id"]
        return chain


def expiring_within(days: int, *, conn=None) -> List[Dict[str, Any]]:
    """Active licenses whose expiry falls within the next ``days`` days.

    The proactive-renewal list (AC4): ``status='active'`` AND ``expires_at``
    between today and today+``days`` (inclusive). Already-expired and further-out
    licenses are excluded, so the result is exactly the set expiring in the
    window. Ordered soonest-expiry first.
    """
    if days < 0:
        raise RegistryError("days must be >= 0")
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM license_registry
                 WHERE status = %s
                   AND expires_at >= CURRENT_DATE
                   AND expires_at <= CURRENT_DATE + %s
                 ORDER BY expires_at ASC
                """,
                (STATUS_ACTIVE, days),
            )
            return [dict(r) for r in cur.fetchall()]


def get_audit_for_license(license_id: str, conn=None) -> List[Dict[str, Any]]:
    with _conn_ctx(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT * FROM issuance_audit WHERE license_id = %s ORDER BY occurred_at ASC",
                (license_id,),
            )
            return [dict(r) for r in cur.fetchall()]
