"""R-1.9.1-L2 / T3 (AT-695) — signed usage report generator (AC3).

A LOCAL, OFFLINE generator: it reads the immutable billing telemetry for a
requested period and the installation's license, assembles a deterministically
serialised report, and SIGNS it (HMAC-SHA256) with the per-installation
``report_key`` delivered in the license payload (L1). CloudFulcrum verifies the
signature on receipt with the same ``report_key``. The customer generates and
sends the report — nothing here initiates outbound contact (the federal
no-phone-home posture; AC5). Exposed both as an Owner-only route
(``routes_usage_report.py``) and a CLI (``scripts/generate_usage_report.py``).

The report body carries, for the period:
  * run counts by AI mode + the total,
  * per-run system counts (connected systems at each run),
  * the system connect/disconnect ledger,
  * the license ``kid`` and org,
  * the report period and generation time,
  * an ``event_count`` (how many billing events were included).

Signature contract (AC3): ``sign_report_body`` HMACs the canonical bytes of the
body; ``verify_report`` recomputes and constant-time compares. Any altered byte
of the body — or the wrong ``report_key`` — fails verification. The full
event-level hash chain (detecting locally deleted events) is R191-L2 / T4.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from . import billing_chain
from .license_runtime import get_current_license_status
from .telemetry import get_telemetry_range

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
SIGNATURE_ALGORITHM = "HMAC-SHA256"

# The billing event types the report aggregates. run_completed is R191-L2 / T1;
# the system connect/disconnect ledger is T2 — queried unconditionally so the
# ledger section is correct whether or not any ledger events exist yet.
BILLING_RUN_COMPLETED = "billing.run_completed"
BILLING_SYSTEM_CONNECTED = "billing.system_connected"
BILLING_SYSTEM_DISCONNECTED = "billing.system_disconnected"

_AI_MODES = ("hosted", "in_boundary", "customer_tenant")
# A month of runs/ledger entries is comfortably under this; get_telemetry_range
# clamps to its own maximum regardless.
_MAX_EVENTS = 10_000


class UsageReportError(Exception):
    """Raised when a signed usage report cannot be produced (e.g. the installed
    license carries no report_key, or the period is malformed)."""


def _parse_period(period_from: str, period_to: str) -> tuple[_dt.datetime, _dt.datetime]:
    """Turn an inclusive ``YYYY-MM-DD`` [from, to] day range into the half-open
    [from 00:00, to+1day 00:00) UTC window get_telemetry_range expects
    (``timestamp >= from_dt AND timestamp < to_dt``)."""
    try:
        d_from = _dt.date.fromisoformat(period_from)
        d_to = _dt.date.fromisoformat(period_to)
    except (ValueError, TypeError) as exc:
        raise UsageReportError(
            f"period must be two ISO dates (YYYY-MM-DD); got from={period_from!r} to={period_to!r}"
        ) from exc
    if d_to < d_from:
        raise UsageReportError("period 'to' is before 'from'")
    from_dt = _dt.datetime.combine(d_from, _dt.time.min, tzinfo=_dt.timezone.utc)
    to_dt = _dt.datetime.combine(d_to + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc)
    return from_dt, to_dt


def _payload(event: Any) -> dict:
    """The event's JSON payload as a dict (TelemetryEvent.payload is a JSON str)."""
    raw = getattr(event, "payload", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def build_usage_report_body(
    org_id: str,
    period_from: str,
    period_to: str,
    *,
    kid: Optional[str],
    license_org_id: Optional[str],
    generated_at: str,
) -> dict:
    """Assemble the (unsigned) usage report body from the org's billing telemetry.

    Deterministic: aggregates are order-independent and both event lists are
    sorted by a stable key, so the same events + ``generated_at`` always serialise
    to identical bytes (and therefore the same signature)."""
    from_dt, to_dt = _parse_period(period_from, period_to)

    runs = get_telemetry_range(org_id, BILLING_RUN_COMPLETED, from_dt, to_dt, limit=_MAX_EVENTS)
    connected = get_telemetry_range(org_id, BILLING_SYSTEM_CONNECTED, from_dt, to_dt, limit=_MAX_EVENTS)
    disconnected = get_telemetry_range(org_id, BILLING_SYSTEM_DISCONNECTED, from_dt, to_dt, limit=_MAX_EVENTS)

    # chain_events accumulates {seq, event_type, core} for EVERY covered billing
    # event, feeding the tamper-evidence hash chain (T4). ``core`` is the same
    # identity/content the report already lists, so the chain binds what's shown.
    chain_events: list[dict] = []

    by_ai_mode: dict[str, int] = {mode: 0 for mode in _AI_MODES}
    per_run: list[dict] = []
    for ev in runs:
        p = _payload(ev)
        mode = p.get("ai_mode") or "unknown"
        by_ai_mode[mode] = by_ai_mode.get(mode, 0) + 1
        entry = {
            "run_id": p.get("run_id"),
            "ai_mode": p.get("ai_mode"),
            "connected_system_count": p.get("connected_system_count"),
            "pack_ids": p.get("pack_ids") or [],
            "completed_at": p.get("completed_at"),
            "seq": p.get("seq"),
        }
        per_run.append(entry)
        chain_events.append(
            {"seq": p.get("seq"), "event_type": BILLING_RUN_COMPLETED, "core": {
                "run_id": entry["run_id"],
                "ai_mode": entry["ai_mode"],
                "connected_system_count": entry["connected_system_count"],
                "pack_ids": entry["pack_ids"],
                "completed_at": entry["completed_at"],
            }}
        )
    per_run.sort(key=lambda r: (str(r.get("completed_at") or ""), str(r.get("run_id") or "")))

    ledger: list[dict] = []
    for ev, kind, etype in (
        [(e, "connected", BILLING_SYSTEM_CONNECTED) for e in connected]
        + [(e, "disconnected", BILLING_SYSTEM_DISCONNECTED) for e in disconnected]
    ):
        p = _payload(ev)
        entry = {
            "event": kind,
            "connector": p.get("connector"),
            "system_identity": p.get("system_identity"),
            "occurred_at": p.get("occurred_at"),
            "seq": p.get("seq"),
        }
        ledger.append(entry)
        chain_events.append(
            {"seq": p.get("seq"), "event_type": etype, "core": {
                "event": kind,
                "connector": entry["connector"],
                "system_identity": entry["system_identity"],
                "occurred_at": entry["occurred_at"],
            }}
        )
    ledger.sort(
        key=lambda x: (
            str(x.get("occurred_at") or ""),
            x["event"],
            str(x.get("connector") or ""),
            str(x.get("system_identity") or ""),
        )
    )

    event_count = len(runs) + len(connected) + len(disconnected)
    tamper_evidence = billing_chain.build_tamper_evidence(chain_events, total_event_count=event_count)

    return {
        "report_version": REPORT_VERSION,
        "org_id": org_id,
        "kid": kid,
        "license_org_id": license_org_id,
        "period": {"from": period_from, "to": period_to},
        "generated_at": generated_at,
        "runs": {
            "total": len(runs),
            "by_ai_mode": by_ai_mode,
            "per_run": per_run,
        },
        "system_ledger": ledger,
        "event_count": event_count,
        "tamper_evidence": tamper_evidence,
    }


def canonical_bytes(body: dict) -> bytes:
    """Deterministic serialisation of a report body — the exact bytes that are
    signed and verified. Sorted keys + compact separators so signing and
    verification (here and CloudFulcrum-side) agree byte-for-byte."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_report_body(body: dict, report_key: str) -> str:
    """HMAC-SHA256 of the canonical body bytes, keyed by the license report_key."""
    return hmac.new(report_key.encode("utf-8"), canonical_bytes(body), hashlib.sha256).hexdigest()


def verify_report(body: dict, signature: str, report_key: str) -> bool:
    """True iff ``signature`` is the HMAC of ``body`` under ``report_key``.

    Constant-time compare. Any altered byte of the body, or the wrong report_key,
    makes this False (AC3)."""
    try:
        expected = sign_report_body(body, report_key)
        return hmac.compare_digest(expected, str(signature))
    except Exception:
        return False


def _resolve_license_signing(org_id: str) -> tuple[str, Optional[str], Optional[str]]:
    """Return ``(report_key, kid, license_org_id)`` from the installation's
    live-validated license payload. Raises :class:`UsageReportError` when the
    license carries no usable ``report_key`` (no license / pre-v2 / issued without
    one) — a signed report cannot be produced without it."""
    result = get_current_license_status(org_id=org_id)
    payload = result.get("payload") or {}
    report_key = payload.get("report_key")
    if not report_key or not isinstance(report_key, str):
        raise UsageReportError(
            "the installed license carries no report_key — a signed usage report "
            "cannot be produced. Reissue the license with a report key (see L1/L3)."
        )
    return report_key, payload.get("kid"), payload.get("org_id")


def generate_signed_report(
    org_id: str,
    period_from: str,
    period_to: str,
    *,
    generated_at: Optional[str] = None,
) -> dict:
    """Produce the signed usage-report envelope for ``org_id`` over the period.

    Returns ``{"report": <body>, "signature": <hex>, "algorithm": "HMAC-SHA256"}``.
    The ``report_key``/``kid``/org come from the installed license (L1). Fully
    local — no network call is made."""
    report_key, kid, license_org_id = _resolve_license_signing(org_id)
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).isoformat()
    body = build_usage_report_body(
        org_id,
        period_from,
        period_to,
        kid=kid,
        license_org_id=license_org_id,
        generated_at=generated_at,
    )
    return {
        "report": body,
        "signature": sign_report_body(body, report_key),
        "algorithm": SIGNATURE_ALGORITHM,
    }
