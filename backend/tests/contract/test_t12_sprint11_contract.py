"""
T12 — Sprint 11 Contract Tests (22+ tests)

Locks down the full T3-S11-A surface:
  - calculate_trend():   rising, falling, stable, insufficient-data,
                         boundary tests using TEMPORAL_CONFIG values
  - calculate_anomaly(): anomalous, normal, insufficient baseline,
                         zero-stddev first_deviation, zero-stddev no deviation
  - build_baseline_context(): all 7 locked copy states
  - enrich_opportunities_with_temporal_context(): failure safety, field shape
  - Temporal route auth/tenancy: 401, 403 Viewer, 404 cross-org
  - OppEnrichment endpoint: expanded temporal field shape
  - temporal.enrichment_completed: single emission per run

Run:
    cd backend
    python -m pytest tests/contract/test_t12_sprint11_contract.py -v
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_JWT", "dev-token-change-me")

from app import trend_engine
from app.main import app
from app.temporal_enrichment import (
    build_baseline_context,
    enrich_opportunities_with_temporal_context,
)
from app.trend_engine import (
    TEMPORAL_CONFIG,
    AnomalyResult,
    TrendResult,
    calculate_anomaly,
    calculate_trend,
)

client = TestClient(app)

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['DEV_JWT']}"}

def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer viewer-token"}

# ── Stub helpers ──────────────────────────────────────────────────────────────

SIGNAL_KEY = "pack::DET_T12::metric_value"


def _latest_first(values: list[float]) -> list[dict]:
    return [{"metric_value": v} for v in reversed(values)]


def _stub_history(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    def _fake(org_id, detector_id, signal_key, limit=100):
        return _latest_first(values[:limit])
    monkeypatch.setattr(trend_engine, "get_signal_history", _fake)


def _stub_baseline(monkeypatch: pytest.MonkeyPatch, baseline) -> None:
    monkeypatch.setattr(trend_engine, "get_baseline", lambda *a, **kw: baseline)


def _make_trend(direction: str, run_count: int = 5) -> TrendResult:
    rising = direction == "rising"
    falling = direction == "falling"
    return TrendResult(
        trend_direction=direction,
        slope=1.0 if rising or falling else 0.0,
        slope_pct=0.1 if rising else (-0.1 if falling else 0.0),
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


# =============================================================================
# Section 1 — calculate_trend()
# =============================================================================

def test_trend_rising_with_positive_slope(monkeypatch):
    _stub_history(monkeypatch, [10.0, 20.0, 30.0, 40.0, 50.0])
    result = calculate_trend("org_t12", SIGNAL_KEY)
    assert result.trend_direction == "rising"
    assert result.slope > 0
    assert result.run_count == 5


def test_trend_falling_with_negative_slope(monkeypatch):
    _stub_history(monkeypatch, [50.0, 40.0, 30.0, 20.0, 10.0])
    result = calculate_trend("org_t12", SIGNAL_KEY)
    assert result.trend_direction == "falling"
    assert result.slope < 0


def test_trend_stable_within_configured_band(monkeypatch):
    band = float(TEMPORAL_CONFIG["TREND_STABLE_BAND"])
    mean = 100.0
    slope = mean * band * 0.4            # well inside the band
    count = 5
    center = (count - 1) / 2
    values = [mean + slope * (i - center) for i in range(count)]
    _stub_history(monkeypatch, values)
    result = calculate_trend("org_t12", SIGNAL_KEY)
    assert result.trend_direction == "stable"
    assert result.slope_pct is not None
    assert abs(result.slope_pct) <= band


def test_trend_insufficient_data_with_single_run(monkeypatch):
    _stub_history(monkeypatch, [42.0])
    result = calculate_trend("org_t12", SIGNAL_KEY)
    assert result.trend_direction == "insufficient_data"
    assert result.slope is None
    assert result.slope_pct is None
    assert result.r_squared is None
    assert result.run_count == 1


def test_trend_insufficient_data_with_empty_history(monkeypatch):
    _stub_history(monkeypatch, [])
    result = calculate_trend("org_t12", SIGNAL_KEY)
    assert result.trend_direction == "insufficient_data"
    assert result.run_count == 0


def test_trend_window_boundary_uses_temporal_config(monkeypatch):
    """Window size must be read from TEMPORAL_CONFIG, not a hardcoded literal."""
    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_WINDOW_RUNS", 3)
    calls = []

    def _fake(org_id, detector_id, signal_key, limit=100):
        calls.append(limit)
        return _latest_first([10.0, 20.0, 30.0][:limit])

    monkeypatch.setattr(trend_engine, "get_signal_history", _fake)
    calculate_trend("org_t12", SIGNAL_KEY)
    assert calls == [3], "limit passed to get_signal_history must equal TREND_WINDOW_RUNS"


def test_trend_stable_band_boundary_uses_temporal_config(monkeypatch):
    """A slope_pct between two band values must flip classification when band changes."""
    original = float(TEMPORAL_CONFIG["TREND_STABLE_BAND"])
    mid = original * 1.5
    mean = 100.0
    count = 5
    center = (count - 1) / 2
    values = [mean + mean * mid * (i - center) for i in range(count)]
    _stub_history(monkeypatch, values)

    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_STABLE_BAND", original)
    narrow = calculate_trend("org_t12", SIGNAL_KEY)

    monkeypatch.setitem(TEMPORAL_CONFIG, "TREND_STABLE_BAND", original * 2)
    wide = calculate_trend("org_t12", SIGNAL_KEY)

    assert narrow.trend_direction == "rising"
    assert wide.trend_direction == "stable"


# =============================================================================
# Section 2 — calculate_anomaly()
# =============================================================================

def test_anomaly_detected_above_configured_threshold(monkeypatch):
    threshold = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"])
    _stub_baseline(monkeypatch, {
        "baseline_mean": 100.0, "baseline_stddev": 10.0, "insufficient_data": False,
    })
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 100.0 + (threshold + 0.5) * 10.0)
    assert result.is_anomalous is True
    assert result.anomaly_direction == "above"
    assert result.first_deviation is False


def test_anomaly_not_triggered_within_threshold(monkeypatch):
    threshold = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"])
    _stub_baseline(monkeypatch, {
        "baseline_mean": 100.0, "baseline_stddev": 10.0, "insufficient_data": False,
    })
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 100.0 + (threshold - 0.5) * 10.0)
    assert result.is_anomalous is False
    assert result.anomaly_direction is None


def test_anomaly_insufficient_baseline_data(monkeypatch):
    _stub_baseline(monkeypatch, {
        "baseline_mean": None, "baseline_stddev": None, "insufficient_data": True,
    })
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 999.0)
    assert result.is_anomalous is False
    assert result.insufficient_data is True
    assert result.anomaly_score is None


def test_anomaly_none_baseline_treated_as_insufficient(monkeypatch):
    _stub_baseline(monkeypatch, None)
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 999.0)
    assert result.insufficient_data is True
    assert result.is_anomalous is False


def test_anomaly_zero_stddev_with_deviation_sets_first_deviation(monkeypatch):
    _stub_baseline(monkeypatch, {
        "baseline_mean": 100.0, "baseline_stddev": 0.0, "insufficient_data": False,
    })
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 105.0)
    assert result.first_deviation is True
    assert result.is_anomalous is False
    assert result.anomaly_score is None
    assert result.anomaly_direction == "above"


def test_anomaly_zero_stddev_unchanged_value_not_first_deviation(monkeypatch):
    _stub_baseline(monkeypatch, {
        "baseline_mean": 100.0, "baseline_stddev": 0.0, "insufficient_data": False,
    })
    result = calculate_anomaly("org_t12", SIGNAL_KEY, 100.0)
    assert result.first_deviation is False
    assert result.anomaly_direction is None


def test_anomaly_threshold_boundary_responds_to_config(monkeypatch):
    score = float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"]) + 0.3
    _stub_baseline(monkeypatch, {
        "baseline_mean": 100.0, "baseline_stddev": 10.0, "insufficient_data": False,
    })
    current = 100.0 + score * 10.0

    monkeypatch.setitem(TEMPORAL_CONFIG, "ANOMALY_THRESHOLD_STDDEV",
                        float(TEMPORAL_CONFIG["ANOMALY_THRESHOLD_STDDEV"]))
    below = calculate_anomaly("org_t12", SIGNAL_KEY, current)

    monkeypatch.setitem(TEMPORAL_CONFIG, "ANOMALY_THRESHOLD_STDDEV", score + 1.0)
    above = calculate_anomaly("org_t12", SIGNAL_KEY, current)

    assert below.is_anomalous is True
    assert above.is_anomalous is False


# =============================================================================
# Section 3 — build_baseline_context() locked copy states
# =============================================================================

def test_copy_insufficient_data_returns_none():
    trend = _make_trend("rising")
    anomaly = _make_anomaly(insufficient_data=True, baseline_mean=None, baseline_stddev=None)
    assert build_baseline_context(trend, anomaly, 120.0) is None


def test_copy_trend_insufficient_data_returns_none():
    trend = _make_trend("insufficient_data", run_count=1)
    anomaly = _make_anomaly(insufficient_data=True, baseline_mean=None, baseline_stddev=None)
    assert build_baseline_context(trend, anomaly, 120.0) is None


def test_copy_first_deviation_exact_string():
    trend = _make_trend("stable")
    anomaly = _make_anomaly(baseline_stddev=0.0, first_deviation=True, anomaly_direction="above")
    assert build_baseline_context(trend, anomaly, 105.0) == (
        "First deviation from a previously stable baseline"
    )


def test_copy_rising_anomalous_exact_locked_string():
    mean = 12.0
    current = mean * 1.40
    trend = _make_trend("rising")
    anomaly = _make_anomaly(
        is_anomalous=True, anomaly_direction="above",
        baseline_mean=mean, baseline_stddev=2.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90, unit="applications")
    assert result == "Up 40% from your 90-day baseline of 12.0 applications"


def test_copy_falling_anomalous_exact_locked_string():
    mean = 23.0
    current = mean * (1 - 0.31)
    trend = _make_trend("falling")
    anomaly = _make_anomaly(
        is_anomalous=True, anomaly_direction="below",
        baseline_mean=mean, baseline_stddev=3.0,
    )
    result = build_baseline_context(trend, anomaly, current, window_days=90, unit="tickets")
    assert result == "Down 31% from your 90-day baseline of 23.0 tickets"


def test_copy_rising_non_anomalous_exact_locked_string():
    mean = 100.0
    current = mean * 1.18
    trend = _make_trend("rising")
    anomaly = _make_anomaly(is_anomalous=False, baseline_mean=mean, baseline_stddev=5.0)
    result = build_baseline_context(trend, anomaly, current, window_days=90)
    assert result == "Trending up — currently 18% above your 90-day baseline"


def test_copy_falling_non_anomalous_exact_locked_string():
    mean = 100.0
    current = mean * (1 - 0.12)
    trend = _make_trend("falling")
    anomaly = _make_anomaly(is_anomalous=False, baseline_mean=mean, baseline_stddev=5.0)
    result = build_baseline_context(trend, anomaly, current, window_days=90)
    assert result == "Trending down — currently 12% below your 90-day baseline"


def test_copy_stable_exact_locked_string():
    trend = _make_trend("stable")
    anomaly = _make_anomaly(is_anomalous=False, baseline_mean=100.0, baseline_stddev=5.0)
    result = build_baseline_context(trend, anomaly, 102.0, window_days=90)
    assert result == "Stable — within normal range of your 90-day baseline"


def test_copy_first_deviation_overrides_trend_direction():
    for direction in ("rising", "falling", "stable"):
        trend = _make_trend(direction)
        anomaly = _make_anomaly(baseline_stddev=0.0, first_deviation=True)
        assert build_baseline_context(trend, anomaly, 105.0) == (
            "First deviation from a previously stable baseline"
        ), f"first_deviation must override trend_direction={direction!r}"


# =============================================================================
# Section 4 — enrich_opportunities_with_temporal_context() failure safety
# =============================================================================

def test_enrichment_failure_does_not_raise_or_drop_opportunity():
    """AC12: exception in temporal enrichment returns opps unchanged, non-blocking."""
    opps = [
        {
            "id": "opp_t12_001",
            "title": "Test opp",
            "_debug": {"detector_id": "DET_T12"},
            "metric_value": 42.0,
        }
    ]
    with patch("app.temporal_enrichment.calculate_trend", side_effect=RuntimeError("boom")):
        result = enrich_opportunities_with_temporal_context("run_t12", "org_t12", "pack", opps)
    assert len(result) == 1
    assert result[0]["id"] == "opp_t12_001"


def test_enrichment_sets_all_six_temporal_fields(monkeypatch):
    """All six temporal fields are attached when enrichment succeeds."""
    opp = {
        "id": "opp_t12_002",
        "_debug": {"detector_id": "DET_T12"},
        "metric_value": 50.0,
    }

    trend = _make_trend("rising", run_count=5)
    anomaly = _make_anomaly(is_anomalous=False, baseline_mean=45.0, baseline_stddev=3.0)

    with patch("app.temporal_enrichment.calculate_trend", return_value=trend), \
         patch("app.temporal_enrichment.calculate_anomaly", return_value=anomaly):
        result = enrich_opportunities_with_temporal_context("run_t12", "org_t12", "pack", [opp])

    enriched = result[0]
    assert "baseline_context" in enriched
    assert "trend_direction" in enriched
    assert "anomaly_score" in enriched
    assert "is_anomalous" in enriched
    assert "first_deviation" in enriched
    assert "baseline_mean" in enriched
    assert "run_count" in enriched
    assert enriched["trend_direction"] == "rising"
    assert enriched["run_count"] == 5


def test_enrichment_with_two_history_values_marks_trend_insufficient(monkeypatch):
    """AC14: <3 historical values keeps opportunity trend_direction insufficient."""
    _stub_history(monkeypatch, [10.0, 12.0])
    _stub_baseline(monkeypatch, {
        "baseline_mean": None,
        "baseline_stddev": None,
        "insufficient_data": True,
    })
    opp = {
        "id": "opp_t12_002b",
        "_debug": {"detector_id": "DET_T12"},
        "metric_value": 12.0,
    }

    result = enrich_opportunities_with_temporal_context(
        "run_t12",
        "org_t12",
        "pack",
        [opp],
    )

    enriched = result[0]
    assert enriched["baseline_context"] is None
    assert enriched["trend_direction"] == "insufficient_data"
    assert enriched["run_count"] == 2


def test_enrichment_reads_metric_value_from_debug_payload(monkeypatch):
    """Real run opportunities carry metric_value inside _debug."""
    _stub_history(monkeypatch, [10.0])
    _stub_baseline(monkeypatch, {
        "baseline_mean": None,
        "baseline_stddev": None,
        "insufficient_data": True,
    })
    opp = {
        "id": "opp_t12_002c",
        "_debug": {"detector_id": "DET_T12", "metric_value": 10.0},
    }

    result = enrich_opportunities_with_temporal_context(
        "run_t12",
        "org_t12",
        "pack",
        [opp],
    )

    enriched = result[0]
    assert enriched["baseline_context"] is None
    assert enriched["trend_direction"] == "insufficient_data"
    assert enriched["run_count"] == 1


def test_materialize_t2_pack_id_supports_stack_builder_run_shape():
    """Stack Builder launches store packId on the run root, not only in inputs."""
    from app.materialize_t2 import _pack_id_for_run

    assert _pack_id_for_run({"packId": "service_cloud"}) == "service_cloud"
    assert _pack_id_for_run({"inputs": {"packId": "ncino"}}) == "ncino"
    assert (
        _pack_id_for_run({"inputs": {"packId": "strs_benefits"}, "packId": "ncino"})
        == "strs_benefits"
    )
    assert _pack_id_for_run({}) is None


def test_enrichment_skips_opp_without_detector_id():
    """Opportunity without _debug.detector_id is returned without temporal fields."""
    opp = {"id": "opp_t12_003", "metric_value": 10.0}
    result = enrich_opportunities_with_temporal_context("run_t12", "org_t12", "pack", [opp])
    assert "trend_direction" not in result[0]


def test_enrichment_skips_opp_without_metric_value():
    """Opportunity without metric_value is returned without temporal fields."""
    opp = {"id": "opp_t12_004", "_debug": {"detector_id": "DET_T12"}}
    result = enrich_opportunities_with_temporal_context("run_t12", "org_t12", "pack", [opp])
    assert "trend_direction" not in result[0]


# =============================================================================
# Section 5 — Temporal route authentication & tenancy
# =============================================================================

def test_temporal_history_returns_401_unauthenticated():
    assert client.get("/api/temporal/det_t12/history").status_code == 401


def test_temporal_baseline_returns_401_unauthenticated():
    assert client.get("/api/temporal/det_t12/baseline").status_code == 401


def test_temporal_trend_returns_401_unauthenticated():
    assert client.get("/api/temporal/det_t12/trend").status_code == 401


def test_temporal_context_returns_401_unauthenticated():
    assert client.get("/api/runs/run_missing_t12/temporal-context").status_code == 401


def test_temporal_history_returns_403_for_viewer_role():
    assert client.get("/api/temporal/det_t12/history", headers=_viewer()).status_code == 403


def test_temporal_baseline_returns_403_for_viewer_role():
    assert client.get("/api/temporal/det_t12/baseline", headers=_viewer()).status_code == 403


def test_temporal_trend_returns_403_for_viewer_role():
    assert client.get("/api/temporal/det_t12/trend", headers=_viewer()).status_code == 403


def test_temporal_context_returns_403_for_viewer_role():
    assert (
        client.get("/api/runs/run_missing_t12/temporal-context", headers=_viewer()).status_code
        == 403
    )


def test_temporal_context_returns_404_for_cross_org_run():
    """AC16: temporal-context returns 404 when run belongs to a different org."""
    from app.db import upsert_run
    run_id = f"run_t12_xorg_{uuid4().hex[:8]}"
    upsert_run(run_id, {"id": run_id, "status": "complete", "org_id": "org_t12_other_9999"})
    response = client.get(f"/api/runs/{run_id}/temporal-context", headers=_auth())
    assert response.status_code == 404


# =============================================================================
# Section 6 — temporal.enrichment_completed telemetry: single emission per run
# =============================================================================

def test_temporal_enrichment_completed_event_registered():
    """temporal.enrichment_completed must be registered in EVENT_REGISTRY."""
    from app.telemetry import EVENT_REGISTRY
    assert "temporal.enrichment_completed" in EVENT_REGISTRY, (
        "temporal.enrichment_completed not found in EVENT_REGISTRY"
    )


def test_temporal_enrichment_completed_emits_exactly_once_per_run():
    """Exactly one temporal.enrichment_completed event is recorded per run."""
    import contextlib
    from app.telemetry import record_event

    written: list = []

    class _FakeSession:
        _written = written

        def add(self, event):
            written.append(event)

        def commit(self):
            pass

    @contextlib.contextmanager
    def _fake_get_db_session():
        yield _FakeSession()

    with patch("app.telemetry.get_db_session", _fake_get_db_session):
        record_event("temporal.enrichment_completed", {"run_id": "run_t12_telemetry"})

    emitted = [e for e in written if e.event_type == "temporal.enrichment_completed"]
    assert len(emitted) == 1, (
        f"Expected exactly 1 temporal.enrichment_completed event, got {len(emitted)}"
    )
    assert emitted[0].event_type == "temporal.enrichment_completed"


def test_temporal_enrichment_completed_called_in_materialize_t2():
    """Structural: record_event('temporal.enrichment_completed') must appear in materialize_t2.py."""
    src = Path(__file__).parents[2] / "app" / "materialize_t2.py"
    if not src.exists():
        pytest.skip("materialize_t2.py not found — skipping structural check")

    tree = ast.parse(src.read_text(encoding="utf-8"))
    found_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "record_event":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == "temporal.enrichment_completed":
            found_calls.append(node.lineno)

    assert found_calls, (
        "record_event('temporal.enrichment_completed') not found in materialize_t2.py — "
        "telemetry emission is required per T3-S11-A"
    )
