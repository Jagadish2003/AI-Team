"""Temporal enrichment — baseline context copy builder.

The seven baseline_context string templates (Section 4, T3-S11-A) are locked
product decisions.  Only the threshold values in TEMPORAL_CONFIG are tunable.
Do not modify the string templates without explicit product sign-off.

Called from the enrichment integration layer (Section 5, T3-S11-A):
    context = build_baseline_context(trend, anomaly, float(current_value))
"""

from __future__ import annotations

import logging
from typing import Optional

try:
    from app.trend_engine import (
        AnomalyResult,
        TrendResult,
        calculate_anomaly,
        calculate_trend,
    )
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.trend_engine import (
        AnomalyResult,
        TrendResult,
        calculate_anomaly,
        calculate_trend,
    )

logger = logging.getLogger(__name__)

# Default baseline window used in copy strings.
# Matches the canonical example ("90-day baseline") from T3-S11-A Section 4.
# Override per-call with the window_days keyword argument when a signal's
# actual baseline_window_days is known.
_BASELINE_WINDOW_DAYS = 90


def build_baseline_context(
    trend: TrendResult,
    anomaly: AnomalyResult,
    current_value: float,
    *,
    window_days: int = _BASELINE_WINDOW_DAYS,
    unit: str = "",
) -> Optional[str]:
    """Return the locked baseline context copy string for an opportunity.

    Returns None when insufficient signal history exists (baseline_context
    field is absent from the API response in that case).

    State priority:
    1. insufficient_data      → None
    2. first_deviation        → stable-baseline deviation notice
    3. anomalous + rising     → Up N% from baseline of mean
    4. anomalous + falling    → Down N% from baseline of mean
    5. rising (not anomalous) → Trending up notice
    6. falling (not anomalous)→ Trending down notice
    7. stable                 → Within normal range notice
    """
    if anomaly.insufficient_data or trend.trend_direction == "insufficient_data":
        return None

    # stddev == 0: signal was perfectly stable; any deviation is notable but
    # cannot be statistically scored as anomalous (no variance in baseline).
    if anomaly.first_deviation:
        return "First deviation from a previously stable baseline"

    direction = trend.trend_direction
    mean = anomaly.baseline_mean  # non-None guaranteed when !insufficient_data
    unit_suffix = f" {unit}" if unit else ""

    pct = round(abs(current_value - mean) / mean * 100) if mean else 0

    if anomaly.is_anomalous:
        if direction == "rising":
            return (
                f"Up {pct}% from your {window_days}-day baseline"
                f" of {mean:.1f}{unit_suffix}"
            )
        if direction == "falling":
            return (
                f"Down {pct}% from your {window_days}-day baseline"
                f" of {mean:.1f}{unit_suffix}"
            )

    if direction == "rising":
        return f"Trending up — currently {pct}% above your {window_days}-day baseline"
    if direction == "falling":
        return f"Trending down — currently {pct}% below your {window_days}-day baseline"

    # direction == "stable" (or any unrecognised direction defaults to stable copy)
    return f"Stable — within normal range of your {window_days}-day baseline"


def enrich_opportunities_with_temporal_context(
    run_id: str,
    org_id: str,
    pack_id: str,
    opps: list,
) -> list:
    """Attach temporal fields to each opportunity dict. Non-blocking.

    Opportunities that lack detector metadata or a metric_value are skipped
    and returned unchanged — missing temporal data must not cause an
    opportunity to disappear or fail.

    Returns opps unchanged if any unexpected error occurs.
    """
    try:
        for opp in opps:
            debug = opp.get("_debug", {}) or {}
            detector_id = debug.get("detector_id")
            current_value = opp.get("metric_value")
            if current_value is None:
                current_value = debug.get("metric_value")
            if not detector_id or current_value is None:
                continue

            signal_key = f"{pack_id}::{detector_id}::metric_value"
            trend = calculate_trend(org_id, signal_key)
            anomaly = calculate_anomaly(org_id, signal_key, float(current_value))
            context = build_baseline_context(trend, anomaly, float(current_value))
            trend_direction = (
                "insufficient_data"
                if anomaly.insufficient_data
                else trend.trend_direction
            )

            opp["baseline_context"] = context
            opp["trend_direction"] = trend_direction
            opp["anomaly_score"] = anomaly.anomaly_score
            opp["is_anomalous"] = anomaly.is_anomalous
            opp["first_deviation"] = anomaly.first_deviation
            opp["baseline_mean"] = anomaly.baseline_mean
            opp["run_count"] = trend.run_count
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temporal enrichment failed (non-blocking): %s", exc)
    return opps


__all__ = ["build_baseline_context", "enrich_opportunities_with_temporal_context"]
