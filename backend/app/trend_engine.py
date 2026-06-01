"""Trend detection engine for temporal signal analysis.

This module owns TEMPORAL_CONFIG — the single source of truth for all
tunable thresholds used by trend direction classification and anomaly
detection.  Downstream functions must read from TEMPORAL_CONFIG (or the
convenience aliases below) rather than using bare numeric literals so that
calibration changes propagate automatically.
"""

from __future__ import annotations

import inspect
import statistics
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

try:
    from app.temporal import get_signal_history
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.app.temporal import get_signal_history

# ---------------------------------------------------------------------------
# Temporal configuration — tunable defaults
# ---------------------------------------------------------------------------
# These are first-iteration engineering defaults, not locked product policy.
# They will be reviewed after 90 days of production signal data.
# Change values here; algorithm logic does NOT need to change.
# ---------------------------------------------------------------------------

TEMPORAL_CONFIG: dict = {
    # Number of historical runs used for linear regression.
    # Rationale: 5 provides a directional signal without over-fitting to
    # recent noise.  A window of 3 reacts quickly but is volatile; 7-10
    # dampens transient spikes.
    # Calibrate after 90 days: if trend_direction='rising' is too frequent,
    # increase to 7-10.  If real trends are detected too slowly, decrease to 3.
    "TREND_WINDOW_RUNS": 5,
    # Slope as a fraction of the signal mean within which the signal is
    # classified as 'stable' (i.e. 0.05 == 5%).
    # Rationale: 5% accounts for normal run-to-run measurement variation
    # without flagging routine noise as a directional trend.
    # Calibrate after 90 days: widen (e.g. 0.10) if too many false
    # rising/falling labels appear; narrow (e.g. 0.02) if genuine small
    # trends are being missed.
    "TREND_STABLE_BAND": 0.05,
    # Standard deviations from baseline_mean required to classify a current
    # value as anomalous.
    # Rationale: 2 standard deviations flags roughly the top 5% of
    # observations under a normal distribution — a meaningful but not
    # hypersensitive threshold.
    # Calibrate after 90 days: increase to 2.5-3.0 if anomaly alerts fire
    # too frequently; decrease to 1.5 if genuine anomalies are being missed.
    "ANOMALY_THRESHOLD_STDDEV": 2.0,
}

# ---------------------------------------------------------------------------
# Convenience aliases for existing imports. Algorithm code reads from
# TEMPORAL_CONFIG at call time so calibration changes take effect immediately.
# ---------------------------------------------------------------------------

TREND_WINDOW_RUNS: int = TEMPORAL_CONFIG["TREND_WINDOW_RUNS"]
TREND_STABLE_BAND: float = TEMPORAL_CONFIG["TREND_STABLE_BAND"]
ANOMALY_THRESHOLD_STDDEV: float = TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"]


@dataclass(frozen=True)
class TrendResult:
    trend_direction: Literal["rising", "falling", "stable", "insufficient_data"]
    slope: float | None
    slope_pct: float | None
    r_squared: float | None
    run_count: int
    signal_key: str


def _trend_window_runs() -> int:
    return int(TEMPORAL_CONFIG["TREND_WINDOW_RUNS"])


def _trend_stable_band() -> float:
    return float(TEMPORAL_CONFIG["TREND_STABLE_BAND"])


def _detector_id_from_signal_key(signal_key: str) -> str:
    parts = signal_key.split("::")
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    return signal_key


def _history_accepts_detector_id() -> bool:
    """Support the current Sprint 10 helper plus the Sprint 11 doc signature."""
    try:
        signature = inspect.signature(get_signal_history)
    except (TypeError, ValueError):
        return True

    params = list(signature.parameters.values())
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params):
        return True
    if "detector_id" in signature.parameters:
        return True

    positional = [
        param
        for param in params
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 4


def _recent_signal_history(org_id: str, signal_key: str, limit: int) -> Sequence[Any]:
    if _history_accepts_detector_id():
        detector_id = _detector_id_from_signal_key(signal_key)
        return get_signal_history(org_id, detector_id, signal_key, limit=limit)
    return get_signal_history(org_id, signal_key, limit=limit)


def _metric_value(row: Any) -> float:
    if isinstance(row, Mapping):
        raw_value = row.get("metric_value", row.get("value"))
    else:
        raw_value = getattr(row, "metric_value", getattr(row, "value", None))

    if raw_value is None:
        raise ValueError("signal history row is missing metric_value")
    return float(raw_value)


def calculate_trend(org_id: str, signal_key: str) -> TrendResult:
    """Calculate trend direction for a signal using recent temporal history."""
    history = _recent_signal_history(
        org_id=org_id,
        signal_key=signal_key,
        limit=_trend_window_runs(),
    )
    run_count = len(history)
    if run_count < 2:
        return TrendResult(
            trend_direction="insufficient_data",
            slope=None,
            slope_pct=None,
            r_squared=None,
            run_count=run_count,
            signal_key=signal_key,
        )

    values = [_metric_value(row) for row in reversed(history)]
    x_values = list(range(run_count))
    mean_x = statistics.mean(x_values)
    mean_y = statistics.mean(values)

    numerator = sum(
        (x_value - mean_x) * (y_value - mean_y)
        for x_value, y_value in zip(x_values, values)
    )
    denominator = sum((x_value - mean_x) ** 2 for x_value in x_values)
    slope = numerator / denominator if denominator else 0.0

    residual_sum = sum(
        (y_value - (mean_y + slope * (x_value - mean_x))) ** 2
        for x_value, y_value in zip(x_values, values)
    )
    total_sum = sum((y_value - mean_y) ** 2 for y_value in values)
    r_squared = 1 - (residual_sum / total_sum) if total_sum else 1.0
    slope_pct = slope / mean_y if mean_y else 0.0

    stable_band = _trend_stable_band()
    if abs(slope_pct) <= stable_band:
        trend_direction: Literal["rising", "falling", "stable"] = "stable"
    elif slope_pct > 0:
        trend_direction = "rising"
    else:
        trend_direction = "falling"

    return TrendResult(
        trend_direction=trend_direction,
        slope=round(slope, 4),
        slope_pct=round(slope_pct, 4),
        r_squared=round(r_squared, 4),
        run_count=run_count,
        signal_key=signal_key,
    )


__all__ = [
    "TrendResult",
    "TEMPORAL_CONFIG",
    "TREND_WINDOW_RUNS",
    "TREND_STABLE_BAND",
    "ANOMALY_THRESHOLD_STDDEV",
    "calculate_trend",
]
