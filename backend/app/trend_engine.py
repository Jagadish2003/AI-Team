"""Trend detection engine for temporal signal analysis.

This module owns TEMPORAL_CONFIG — the single source of truth for all
tunable thresholds used by trend direction classification and anomaly
detection.  Downstream functions must read from TEMPORAL_CONFIG (or the
convenience aliases below) rather than using bare numeric literals so that
calibration changes propagate automatically.
"""

from __future__ import annotations

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
# Convenience aliases — read from TEMPORAL_CONFIG, not hardcoded.
# All algorithm code should use these names; changing the dict above changes
# behaviour everywhere without touching algorithm logic (AC22).
# ---------------------------------------------------------------------------

TREND_WINDOW_RUNS: int = TEMPORAL_CONFIG["TREND_WINDOW_RUNS"]
TREND_STABLE_BAND: float = TEMPORAL_CONFIG["TREND_STABLE_BAND"]
ANOMALY_THRESHOLD_STDDEV: float = TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"]

__all__ = [
    "TEMPORAL_CONFIG",
    "TREND_WINDOW_RUNS",
    "TREND_STABLE_BAND",
    "ANOMALY_THRESHOLD_STDDEV",
]
