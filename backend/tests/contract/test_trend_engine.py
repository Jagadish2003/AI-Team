from __future__ import annotations

import pytest

from app import trend_engine
from app.trend_engine import TEMPORAL_CONFIG, AnomalyResult, TrendResult, calculate_anomaly, calculate_trend
from app.temporal_enrichment import build_baseline_context


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


def _stub_baseline(monkeypatch: pytest.MonkeyPatch, baseline) -> list[dict]:
    calls = []

    def fake_get_baseline(org_id, detector_id):
        calls.append({"org_id": org_id, "detector_id": detector_id})
        return baseline

    monkeypatch.setattr(trend_engine, "get_baseline", fake_get_baseline)
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


def test_calculate_anomaly_marks_value_above_configured_threshold(monkeypatch):
    threshold = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"])
    calls = _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": 100.0,
            "baseline_stddev": 10.0,
            "insufficient_data": False,
        },
    )

    result = calculate_anomaly(
        "org_anomaly",
        SIGNAL_KEY,
        current_value=100.0 + ((threshold + 0.25) * 10.0),
    )

    assert result.is_anomalous is True
    assert result.anomaly_score == round(threshold + 0.25, 2)
    assert result.anomaly_direction == "above"
    assert result.baseline_mean == 100.0
    assert result.baseline_stddev == 10.0
    assert result.insufficient_data is False
    assert result.first_deviation is False
    assert result.signal_key == SIGNAL_KEY
    assert calls == [{"org_id": "org_anomaly", "detector_id": "DET_TREND"}]


def test_calculate_anomaly_does_not_mark_value_within_threshold(monkeypatch):
    threshold = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"])
    _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": 100.0,
            "baseline_stddev": 10.0,
            "insufficient_data": False,
        },
    )

    result = calculate_anomaly(
        "org_anomaly",
        SIGNAL_KEY,
        current_value=100.0 + ((threshold - 0.25) * 10.0),
    )

    assert result.is_anomalous is False
    assert result.anomaly_score == round(threshold - 0.25, 2)
    assert result.anomaly_direction is None
    assert result.insufficient_data is False
    assert result.first_deviation is False


def test_calculate_anomaly_returns_insufficient_data_without_raising(monkeypatch):
    _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": None,
            "baseline_stddev": None,
            "insufficient_data": True,
        },
    )

    result = calculate_anomaly("org_anomaly", SIGNAL_KEY, current_value=125.0)

    assert result.is_anomalous is False
    assert result.anomaly_score is None
    assert result.anomaly_direction is None
    assert result.baseline_mean is None
    assert result.baseline_stddev is None
    assert result.insufficient_data is True
    assert result.first_deviation is False
    assert result.signal_key == SIGNAL_KEY


def test_calculate_anomaly_treats_missing_baseline_as_insufficient_data(
    monkeypatch,
):
    _stub_baseline(monkeypatch, None)

    result = calculate_anomaly("org_anomaly", SIGNAL_KEY, current_value=125.0)

    assert result.is_anomalous is False
    assert result.insufficient_data is True
    assert result.first_deviation is False


def test_calculate_anomaly_marks_first_deviation_for_zero_stddev(monkeypatch):
    _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": 100.0,
            "baseline_stddev": 0.0,
            "insufficient_data": False,
        },
    )

    result = calculate_anomaly("org_anomaly", SIGNAL_KEY, current_value=90.0)

    assert result.is_anomalous is False
    assert result.anomaly_score is None
    assert result.anomaly_direction == "below"
    assert result.baseline_mean == 100.0
    assert result.baseline_stddev == 0.0
    assert result.insufficient_data is False
    assert result.first_deviation is True


def test_calculate_anomaly_does_not_mark_first_deviation_when_value_unchanged(
    monkeypatch,
):
    _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": 100.0,
            "baseline_stddev": 0.0,
            "insufficient_data": False,
        },
    )

    result = calculate_anomaly("org_anomaly", SIGNAL_KEY, current_value=100.0)

    assert result.is_anomalous is False
    assert result.anomaly_score is None
    assert result.anomaly_direction is None
    assert result.first_deviation is False


def test_calculate_anomaly_responds_to_configured_threshold(monkeypatch):
    original_threshold = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"])
    score_between_thresholds = original_threshold + 0.25
    _stub_baseline(
        monkeypatch,
        {
            "baseline_mean": 100.0,
            "baseline_stddev": 10.0,
            "insufficient_data": False,
        },
    )

    monkeypatch.setitem(
        TEMPORAL_CONFIG,
        "ANOMALY_THRESHOLD_STDDEV",
        original_threshold,
    )
    lower_threshold_result = calculate_anomaly(
        "org_config",
        SIGNAL_KEY,
        current_value=100.0 + (score_between_thresholds * 10.0),
    )

    monkeypatch.setitem(
        TEMPORAL_CONFIG,
        "ANOMALY_THRESHOLD_STDDEV",
        score_between_thresholds + 0.25,
    )
    higher_threshold_result = calculate_anomaly(
        "org_config",
        SIGNAL_KEY,
        current_value=100.0 + (score_between_thresholds * 10.0),
    )

    assert lower_threshold_result.is_anomalous is True
    assert higher_threshold_result.is_anomalous is False


# ---------------------------------------------------------------------------
# build_baseline_context — Section 4 locked copy pattern (T3-S11-A)
# ---------------------------------------------------------------------------

def _make_trend(direction: str, run_count: int = 5) -> TrendResult:
    return TrendResult(
        trend_direction=direction,
        slope=1.0 if direction not in ("stable", "insufficient_data") else 0.0,
        slope_pct=0.1 if direction == "rising" else (-0.1 if direction == "falling" else 0.0),
        r_squared=1.0,
        run_count=run_count,
        signal_key=SIGNAL_KEY,
    )


def _make_anomaly(
    *,
    is_anomalous: bool = False,
    anomaly_score: float | None = None,
    anomaly_direction=None,
    baseline_mean: float | None = 100.0,
    baseline_stddev: float | None = 10.0,
    insufficient_data: bool = False,
    first_deviation: bool = False,
) -> AnomalyResult:
    return AnomalyResult(
        is_anomalous=is_anomalous,
        anomaly_score=anomaly_score,
        anomaly_direction=anomaly_direction,
        baseline_mean=baseline_mean,
        baseline_stddev=baseline_stddev,
        insufficient_data=insufficient_data,
        first_deviation=first_deviation,
        signal_key=SIGNAL_KEY,
    )


# AC10 — insufficient data returns None
def test_build_baseline_context_returns_none_when_anomaly_insufficient_data():
    trend = _make_trend("rising")
    anomaly = _make_anomaly(insufficient_data=True, baseline_mean=None, baseline_stddev=None)
    assert build_baseline_context(trend, anomaly, 120.0) is None


def test_build_baseline_context_returns_none_when_trend_insufficient_data():
    trend = _make_trend("insufficient_data", run_count=1)
    anomaly = _make_anomaly(insufficient_data=True, baseline_mean=None, baseline_stddev=None)
    assert build_baseline_context(trend, anomaly, 120.0) is None


# AC9 — first deviation returns exact locked string
def test_build_baseline_context_returns_first_deviation_string(monkeypatch):
    trend = _make_trend("stable")
    anomaly = _make_anomaly(
        baseline_stddev=0.0,
        first_deviation=True,
        anomaly_direction="above",
    )
    result = build_baseline_context(trend, anomaly, 105.0)
    assert result == "First deviation from a previously stable baseline"


def test_build_baseline_context_first_deviation_takes_priority_over_trend_direction():
    """first_deviation copy is returned regardless of trend direction."""
    for direction in ("rising", "falling", "stable"):
        trend = _make_trend(direction)
        anomaly = _make_anomaly(baseline_stddev=0.0, first_deviation=True)
        assert build_baseline_context(trend, anomaly, 105.0) == (
            "First deviation from a previously stable baseline"
        )


# AC8 — rising anomalous exact locked copy
def test_build_baseline_context_rising_anomalous_matches_locked_copy():
    mean = 12.0
    current = mean * 1.40  # 40% above
    trend = _make_trend("rising")
    anomaly = _make_anomaly(
        is_anomalous=True,
        anomaly_score=2.5,
        anomaly_direction="above",
        baseline_mean=mean,
        baseline_stddev=2.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90, unit="applications")
    assert result == "Up 40% from your 90-day baseline of 12.0 applications"


# Falling anomalous locked copy
def test_build_baseline_context_falling_anomalous_matches_locked_copy():
    mean = 23.0
    current = mean * (1 - 0.31)  # 31% below
    trend = _make_trend("falling")
    anomaly = _make_anomaly(
        is_anomalous=True,
        anomaly_score=3.1,
        anomaly_direction="below",
        baseline_mean=mean,
        baseline_stddev=3.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90, unit="tickets")
    assert result == "Down 31% from your 90-day baseline of 23.0 tickets"


# Rising non-anomalous locked copy
def test_build_baseline_context_rising_non_anomalous_matches_locked_copy():
    mean = 100.0
    current = mean * 1.18  # 18% above
    trend = _make_trend("rising")
    anomaly = _make_anomaly(
        is_anomalous=False,
        baseline_mean=mean,
        baseline_stddev=5.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90)
    assert result == "Trending up — currently 18% above your 90-day baseline"


# Falling non-anomalous locked copy
def test_build_baseline_context_falling_non_anomalous_matches_locked_copy():
    mean = 100.0
    current = mean * (1 - 0.12)  # 12% below
    trend = _make_trend("falling")
    anomaly = _make_anomaly(
        is_anomalous=False,
        baseline_mean=mean,
        baseline_stddev=5.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90)
    assert result == "Trending down — currently 12% below your 90-day baseline"


# Stable locked copy
def test_build_baseline_context_stable_matches_locked_copy():
    trend = _make_trend("stable")
    anomaly = _make_anomaly(is_anomalous=False, baseline_mean=100.0, baseline_stddev=5.0)
    result = build_baseline_context(trend, anomaly, 102.0, window_days=90)
    assert result == "Stable — within normal range of your 90-day baseline"


# Unit omission — no trailing space when unit is empty
def test_build_baseline_context_anomalous_no_trailing_space_without_unit():
    mean = 12.0
    current = mean * 1.40
    trend = _make_trend("rising")
    anomaly = _make_anomaly(
        is_anomalous=True,
        anomaly_direction="above",
        baseline_mean=mean,
        baseline_stddev=2.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90)
    assert result is not None
    assert not result.endswith(" ")
    assert result == "Up 40% from your 90-day baseline of 12.0"


# window_days default is 90
def test_build_baseline_context_default_window_is_90():
    trend = _make_trend("stable")
    anomaly = _make_anomaly()
    result = build_baseline_context(trend, anomaly, 100.0)
    assert "90-day" in result
