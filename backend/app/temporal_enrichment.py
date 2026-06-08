"""Temporal enrichment — baseline context copy builder.

The seven baseline_context string templates (Section 4, T3-S11-A) are locked
product decisions.  Only the threshold values in TEMPORAL_CONFIG are tunable.
Do not modify the string templates without explicit product sign-off.

Called from the enrichment integration layer (Section 5, T3-S11-A):
    context = build_baseline_context(trend, anomaly, float(current_value))
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recent_metric_values(
    *,
    org_id: str,
    detector_id: str,
    signal_key: str,
    current_run_id: str,
    current_value: float,
    limit: int,
) -> list[float]:
    """Return recent values oldest-to-newest for UI display."""
    try:
        from app.temporal import get_signal_history
    except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
        from backend.app.temporal import get_signal_history

    try:
        rows = get_signal_history(org_id, detector_id, signal_key, limit=limit)
    except Exception:
        return [current_value]
    values: list[float] = []
    includes_current_run = False
    for row in reversed(rows):
        if isinstance(row, dict):
            value = _safe_float(row.get("metric_value", row.get("value")))
            includes_current_run = includes_current_run or row.get("run_id") == current_run_id
        else:
            value = _safe_float(
                getattr(row, "metric_value", getattr(row, "value", None))
            )
            includes_current_run = includes_current_run or (
                getattr(row, "run_id", None) == current_run_id
            )
        if value is not None:
            values.append(value)

    if not values or not includes_current_run:
        values.append(current_value)
    return values[-limit:]


def _baseline_window_days(org_id: str, detector_id: str) -> Optional[int]:
    try:
        from app.temporal import get_baseline
    except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
        from backend.app.temporal import get_baseline

    try:
        baseline = get_baseline(org_id, detector_id)
    except Exception:
        return None
    if not isinstance(baseline, dict):
        return None
    value = baseline.get("baseline_window_days")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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

            current_value_float = float(current_value)
            signal_key = f"{pack_id}::{detector_id}::metric_value"
            trend = calculate_trend(org_id, signal_key)
            anomaly = calculate_anomaly(org_id, signal_key, current_value_float)
            window_days = _baseline_window_days(org_id, detector_id)
            context = build_baseline_context(
                trend,
                anomaly,
                current_value_float,
                window_days=window_days or _BASELINE_WINDOW_DAYS,
            )
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
            opp["baseline_stddev"] = anomaly.baseline_stddev
            opp["baseline_window_days"] = window_days
            opp["run_count"] = trend.run_count
            opp["current_value"] = current_value_float
            opp["recent_values"] = _recent_metric_values(
                org_id=org_id,
                detector_id=detector_id,
                signal_key=signal_key,
                current_run_id=run_id,
                current_value=current_value_float,
                limit=max(trend.run_count, 5),
            )
            opp["signal_key"] = signal_key
            opp["pack_id"] = pack_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temporal enrichment failed (non-blocking): %s", exc)
    return opps


__all__ = ["build_baseline_context", "enrich_opportunities_with_temporal_context"]
