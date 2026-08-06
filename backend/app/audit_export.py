"""
audit_export.py — 2.0-D4 T2: the signed, period-scoped audit export.

This turns the audit trail from something the platform HAS into something a
customer can hand to an auditor, which is a different requirement.

The only audit read surface before this was ``GET /api/runs/{run_id}/audit`` —
Owner-gated and scoped to a single run. That is useful for reviewing one discovery
run and useless for the question an enterprise security review actually asks:
*"show me every state-changing action in this org between these two dates, and
prove the file you gave me is the file the system produced."*

So the export has three properties the run-scoped route does not:

1. **A period, not a run.** ``from``/``to`` bound the disclosure, because "every
   action ever" is neither what an auditor asks for nor something a customer wants
   to hand over.
2. **Org scoping enforced IN THE QUERY.** ``WHERE org_id = %s`` in the SQL, the
   pattern the 2.0-A2 / A3 stores established — isolation asserted after retrieval
   is not isolation, because the rows have already been read and one bug between
   the read and the filter discloses another tenant's audit trail.
3. **A signature over the exported bytes**, via the shared
   :mod:`app.export_signing` scheme, so verification does not depend on trusting
   the transport or the vendor.

Exporting is a disclosure, and a disclosure is auditable
-------------------------------------------------------
Generating an export mutates nothing, and it is still a state-changing action from
a compliance standpoint: someone took a copy of the organisation's audit trail out
of the system. So the export ITSELF emits an audit event
(``AUDIT_EXPORT_GENERATED``), which is genuinely recursive — the export records
that it happened, and a later export over an overlapping period will contain that
record. That is the intended behaviour, not an accident: an auditor should be able
to see who has previously read the trail.

A consequence worth stating plainly: because the audit row is written BEFORE the
export payload is assembled, an export never contains its own generation record —
only those of previous exports. Ordering it the other way would let a disclosure go
unrecorded if serialisation failed after the read.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .export_signing import sign_export

logger = logging.getLogger(__name__)

#: Export-format version, carried in the payload. An auditor's verifier keys off
#: this, so it must change whenever the payload SHAPE changes.
EXPORT_SCHEMA_VERSION = "1"

#: Hard cap on rows in one export. A bounded disclosure is a deliberate choice: an
#: unbounded export is a memory and signing hazard, and — more importantly — an
#: export that silently stopped at some internal limit would be a truncated audit
#: trail presented as a complete one. When the cap is hit the export is still
#: produced, but ``complete`` is False and ``truncated`` says so, so the reader
#: narrows the period rather than trusting a partial file (the same
#: loud-degradation rule MSP-B7 applies to event volume).
MAX_EXPORT_ROWS = 50_000


class AuditExportError(ValueError):
    """Raised for an invalid export request (bad period, missing org)."""


def _parse_boundary(value: Any, *, field: str) -> str:
    """Normalise a period boundary to a comparable ISO-8601 UTC string.

    ``timestamp`` is stored as TEXT (the table is SQLite-compatible), and the
    existing rows are ``datetime.now(timezone.utc).isoformat()``. Comparing ISO-8601
    UTC strings is correct for ordering, so the boundary is normalised to the same
    shape rather than cast in SQL — a cast would silently exclude any row whose
    text does not parse, which is the wrong direction for an audit export.
    """
    if value is None or str(value).strip() == "":
        raise AuditExportError(f"{field} is required (ISO-8601 date or timestamp)")
    text = str(value).strip()
    try:
        # Accept a plain date as well as a full timestamp.
        if len(text) == 10:
            parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            if field == "to":
                # An inclusive end-of-day, so `to=2026-07-20` means all of the 20th
                # rather than only its first instant — the reading every auditor
                # assumes and the one a naive implementation gets wrong.
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        raise AuditExportError(
            f"{field} is not a valid ISO-8601 date or timestamp: {text!r}"
        ) from None
    return parsed.astimezone(timezone.utc).isoformat()


def fetch_audit_rows(
    org_id: str,
    period_from: str,
    period_to: str,
    *,
    limit: int = MAX_EXPORT_ROWS,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Rows for ONE org within a period. Returns ``(rows, complete)``.

    The org predicate is in the SQL, never applied to a wider result afterwards.
    ``limit + 1`` is requested so hitting the cap is detectable: if the extra row
    comes back the export is incomplete, and it says so rather than presenting a
    truncated trail as a whole one.
    """
    org = str(org_id or "").strip()
    if not org:
        raise AuditExportError("org_id is required")

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, org_id, event_type, user_id, run_id, connector_id,
                   payload, timestamp
            FROM audit_log
            WHERE org_id = %s
              AND timestamp >= %s
              AND timestamp <= %s
            ORDER BY timestamp ASC, id ASC
            LIMIT %s
            """,
            (org, period_from, period_to, int(limit) + 1),
        )
        columns = [d[0] for d in cur.description]
        raw = [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        con.close()

    complete = len(raw) <= limit
    rows = raw[:limit]

    out: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, str) and payload:
            try:
                payload = json.loads(payload)
            except ValueError:
                # Keep the raw text rather than dropping it: an unparseable payload
                # is still evidence, and silently omitting it would alter the trail.
                payload = {"_unparsed": payload}
        out.append({
            "id": str(row.get("id") or ""),
            "event_type": row.get("event_type"),
            "actor": row.get("user_id"),
            "run_id": row.get("run_id"),
            "connector_id": row.get("connector_id"),
            "timestamp": row.get("timestamp"),
            "detail": payload or {},
        })
    return out, complete


def build_export(
    org_id: str,
    period_from: Any,
    period_to: Any,
    *,
    generated_by: Optional[str] = None,
    generated_at: Optional[str] = None,
    limit: int = MAX_EXPORT_ROWS,
) -> Dict[str, Any]:
    """Build the UNSIGNED export payload for one org and period."""
    start = _parse_boundary(period_from, field="from")
    end = _parse_boundary(period_to, field="to")
    if end < start:
        raise AuditExportError("`to` must not be earlier than `from`")

    rows, complete = fetch_audit_rows(org_id, start, end, limit=limit)

    payload: Dict[str, Any] = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "org_id": str(org_id),
        "period": {"from": start, "to": end},
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "record_count": len(rows),
        # Loud degradation: a capped export is reported as incomplete rather than
        # handed over as if it were the whole period.
        "complete": complete,
        "records": rows,
    }
    if not complete:
        payload["truncated"] = {
            "limit": int(limit),
            "reason": "export row cap reached — narrow the period and export again",
        }
    return payload


def build_signed_export(
    org_id: str,
    period_from: Any,
    period_to: Any,
    *,
    generated_by: Optional[str] = None,
    generated_at: Optional[str] = None,
    limit: int = MAX_EXPORT_ROWS,
    private_key: Any = None,
) -> Dict[str, Any]:
    """The customer-facing artifact: the payload wrapped in a signed envelope."""
    payload = build_export(
        org_id,
        period_from,
        period_to,
        generated_by=generated_by,
        generated_at=generated_at,
        limit=limit,
    )
    return sign_export(payload, private_key=private_key, key_id="deployment")
