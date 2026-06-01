from __future__ import annotations

import pytest

from app import trend_engine
from app.trend_engine import TEMPORAL_CONFIG, calculate_trend


SIGNAL_KEY = "pack_trend::DET_TREND::metric_value"


def _latest_first(values: list[float]) -> list[dict[str, float]]:
    return [{"metric_value": value} for value in reversed(values)]


def _linear_values(mean: float, slope: float, count: int) -> list[float]:
    center = (count - 1) / 2
    return [mean + slope * (index - center) for index in range(count)]


def _stub_history(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> list[dict]:
    calls = []

    def fake_get_signal_history(org_id, detector_id, signal_key, limit=100):
        calls.append(
            {
                "org_id": org_id,
                "detector_id": detector_id,
                "signal_key": signal_key,
                "limit": limit,
            }
        )
        return _latest_first(values[:limit])

    monkeypatch.setattr(trend_engine, "get_signal_history", fake_get_signal_history)
    return calls


def test_calculate_trend_returns_rising_for_positive_slope(monkeypatch):
    _stub_history(monkeypatch, [10.0, 20.0, 30.0, 40.0, 50.0])

    result = calculate_trend("org_trend", SIGNAL_KEY)

    assert result.trend_direction == "rising"
    assert result.slope == 10.0
    assert result.slope_pct == 0.3333
    assert result.r_squared == 1.0
    assert result.run_count == 5
    assert result.signal_key == SIGNAL_KEY


def test_calculate_trend_returns_falling_for_negative_slope(monkeypatch):
    _stub_history(monkeypatch, [50.0, 40.0, 30.0, 20.0, 10.0])

    result = calculate_trend("org_trend", SIGNAL_KEY)

    assert result.trend_direction == "falling"
    assert result.slope == -10.0
    assert result.slope_pct == -0.3333
    assert result.r_squared == 1.0


def test_calculate_trend_returns_stable_within_configured_band(monkeypatch):
    stable_band = float(TEMPORAL_CONFIG["TREND_STABLE_BAND"])
    mean = 100.0
    slope = mean * stable_band * 0.5
    values = _linear_values(mean=mean, slope=slope, count=5)
    _stub_history(monkeypatch, values)

    result = calculate_trend("org_trend", SIGNAL_KEY)

    assert result.trend_direction == "stable"
    assert abs(result.slope_pct) <= stable_band


def test_calculate_trend_returns_insufficient_data_with_fewer_than_two_runs(
    monkeypatch,
):
    _stub_history(monkeypatch, [42.0])

    result = calculate_trend("org_trend", SIGNAL_KEY)

    assert result.trend_direction == "insufficient_data"
    assert result.slope is None
    assert result.slope_pct is None
    assert result.r_squared is None
    assert result.run_count == 1
    assert result.signal_key == SIGNAL_KEY


def test_calculate_trend_uses_configured_window_and_temporal_helper(monkeypatch):
    configured_window = 3
    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_WINDOW_RUNS", configured_window)
    calls = _stub_history(monkeypatch, [10.0, 20.0, 30.0, 40.0, 50.0])

    result = calculate_trend("org_window", SIGNAL_KEY)

    assert result.run_count == configured_window
    assert calls == [
        {
            "org_id": "org_window",
            "detector_id": "DET_TREND",
            "signal_key": SIGNAL_KEY,
            "limit": configured_window,
        }
    ]


def test_calculate_trend_classification_responds_to_configured_stable_band(
    monkeypatch,
):
    original_band = float(TEMPORAL_CONFIG["TREND_STABLE_BAND"])
    mean = 100.0
    slope_pct_between_bands = original_band * 1.5
    values = _linear_values(
        mean=mean,
        slope=mean * slope_pct_between_bands,
        count=5,
    )
    _stub_history(monkeypatch, values)

    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_STABLE_BAND", original_band)
    narrow_result = calculate_trend("org_config", SIGNAL_KEY)

    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_STABLE_BAND", original_band * 2)
    wider_result = calculate_trend("org_config", SIGNAL_KEY)

    assert narrow_result.trend_direction == "rising"
    assert wider_result.trend_direction == "stable"
