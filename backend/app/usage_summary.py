"""R-1.9.1-L2 / T5 (AT-697) — Owner-facing usage summary (AC6).

Pre-invoice VISIBILITY: an Owner-facing summary of what the signed usage report
(T3, ``usage_report.py``) will say for a period — counts per AI mode and the
systems-over-time picture — so the customer sees the numbers before sending the
report to CloudFulcrum. No surprises at invoice time.

**AC6 is guaranteed by construction, not by parallel maths.** The summary is a pure
PROJECTION of ``usage_report.build_usage_report_body`` — the exact same aggregation
that backs the signed report. It re-reads the same billing telemetry over the same
period and reshapes the already-computed numbers into a summary view, so the
summary's run counts, per-mode breakdown, system ledger, per-run system counts, and
event count are the SAME numbers the report carries for that period — they cannot
drift because there is only one aggregation.

Unlike the signed report, the summary needs NO ``report_key`` and no installed
license: it is a read-only preview, so an Owner can see their usage even before a
report key is provisioned. Fully local — a read of billing telemetry only, no
outbound network call (the federal no-phone-home posture; AC5).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from . import usage_report

logger = logging.getLogger(__name__)

SUMMARY_VERSION = 1

# hosted AI runs are the billable ones; in_boundary / customer_tenant are recorded
# for audit (the pricing rule the report itself derives billability from — kept
# here as a convenience count, still traceable to runs.by_ai_mode).
_BILLABLE_AI_MODE = "hosted"


def build_usage_summary(
    org_id: str,
    period_from: str,
    period_to: str,
    *,
    generated_at: Optional[str] = None,
) -> dict:
    """Assemble the Owner-facing usage summary for ``org_id`` over [from, to].

    A projection of :func:`usage_report.build_usage_report_body`, so every number
    here is exactly the number the signed report carries for the same period (AC6).
    Raises :class:`usage_report.UsageReportError` on a malformed period (reusing the
    report's own validation), so the route can surface one clear 400.
    """
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Reuse the report's aggregation verbatim — kid/license_org_id are signing-only
    # metadata the summary does not need, so they are passed as None. This is the
    # single source of the numbers, which is what makes AC6 hold by construction.
    body = usage_report.build_usage_report_body(
        org_id,
        period_from,
        period_to,
        kid=None,
        license_org_id=None,
        generated_at=generated_at,
    )

    runs = body["runs"]
    ledger = body["system_ledger"]
    connected = [e for e in ledger if e.get("event") == "connected"]
    disconnected = [e for e in ledger if e.get("event") == "disconnected"]

    # "Systems over time": the connected-system count observed at each run, in the
    # report's own per-run order (already sorted by completed_at). Paired with the
    # add/remove ledger below, this is the systems-over-time picture the Owner sees.
    systems_over_time = [
        {
            "run_id": r.get("run_id"),
            "completed_at": r.get("completed_at"),
            "connected_system_count": r.get("connected_system_count"),
        }
        for r in runs["per_run"]
    ]

    return {
        "summary_version": SUMMARY_VERSION,
        "org_id": org_id,
        "period": body["period"],
        "generated_at": generated_at,
        "runs": {
            "total": runs["total"],
            "by_ai_mode": runs["by_ai_mode"],
            # hosted = billable; surfaced so the Owner sees the billable-run count
            # directly. Still just runs.by_ai_mode["hosted"], so it matches the report.
            "billable": runs["by_ai_mode"].get(_BILLABLE_AI_MODE, 0),
        },
        "systems": {
            "connected": len(connected),
            "disconnected": len(disconnected),
            "net_change": len(connected) - len(disconnected),
            # The full timestamped connect/disconnect ledger (the report's
            # system_ledger verbatim) — the pro-ration evidence over the period.
            "ledger": ledger,
            # The connected-system count as billed at each run, over time.
            "over_time": systems_over_time,
        },
        "event_count": body["event_count"],
    }
