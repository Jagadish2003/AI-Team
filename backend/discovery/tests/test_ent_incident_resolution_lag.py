"""
ENT-5 / AT-262 (T1) — ENT_INCIDENT_RESOLUTION_LAG detector unit tests.

Acceptance criteria coverage:
  AC1 — fires when unresolved_pct >= 0.30 AND avg_lag_days >= 14
         AND incident_count_30d >= 10.
  AC2 — does NOT fire when incident_count_30d < 10 (minimum volume guard),
         even when unresolved_pct and avg_lag_days are well above threshold.
  AC6 (partial) — SIGNAL_METRICS defined; all five metrics numeric, present in
         raw_evidence, and at most 8 metrics.

Plus edge cases: exact-threshold firing, below-threshold non-firing on each
condition independently, degraded-signal skip, missing data, the SN<->Jira
join itself (referenced-but-missing Jira issue treated as unresolved), and the
deterministic ``as_of`` reference for still-open issues.
"""
from __future__ import annotations

from discovery.detectors.ent_incident_resolution_lag import (
    DETECTOR_ID,
    MIN_INCIDENT_VOLUME,
    SIGNAL_METRICS,
    UNRESOLVED_PCT_THRESHOLD,
    AVG_LAG_DAYS_THRESHOLD,
    detect,
    evaluate,
)

# A fixed reference date so still-open lag is fully deterministic.
AS_OF = "2026-06-10"


def _incident(idx: int, closed_at: str, jira_key: str) -> dict:
    return {
        "incident_id": f"INC{idx:07d}",
        "closed_at": closed_at,
        "jira_issue_key": jira_key,
    }


def _build_data(
    *,
    total: int,
    unresolved: int,
    incident_closed_at: str = "2026-05-20",
    resolved_lag_days_at: str = "2026-06-09",
    unresolved_resolved_status: bool = False,
    as_of: str = AS_OF,
    degraded: bool = False,
    drop_unresolved_from_jira: bool = False,
):
    """Construct (sn_data, jira_data) for a scenario.

    ``total`` closed incidents reference Jira issues; ``unresolved`` of them
    point at still-open issues. Each incident is closed on ``incident_closed_at``.
    Resolved issues carry ``resolved_lag_days_at`` as their resolution date, so
    the per-incident lag is deterministic.
    """
    incidents = []
    jira_issues: dict = {}
    for i in range(total):
        key = f"OPS-{i}"
        incidents.append(_incident(i, incident_closed_at, key))
        is_unresolved = i < unresolved
        if is_unresolved:
            if drop_unresolved_from_jira:
                # Referenced but absent from the Jira pull — must count as unresolved.
                continue
            jira_issues[key] = {
                "status": "In Progress",
                "resolved": unresolved_resolved_status,
                "resolved_at": None,
            }
        else:
            jira_issues[key] = {
                "status": "Done",
                "resolved": True,
                "resolved_at": resolved_lag_days_at,
            }

    sn_data = {
        "incident_resolution": {
            "closed_incidents": incidents,
            "as_of": as_of,
            "degraded_signal": degraded,
        }
    }
    jira_data = {"issue_resolution": {"issues": jira_issues}}
    return sn_data, jira_data


# ─── AC6: SIGNAL_METRICS shape ────────────────────────────────────────────────

class TestSignalMetrics:
    def test_signal_metrics_exact_set(self):
        assert SIGNAL_METRICS == [
            "unresolved_pct",
            "avg_lag_days",
            "max_lag_days",
            "incident_count_30d",
            "unresolved_count",
        ]

    def test_signal_metrics_max_eight(self):
        assert len(SIGNAL_METRICS) <= 8

    def test_each_metric_numeric_and_in_raw_evidence(self):
        # unresolved=6/12 = 0.5, avg_lag ~20 days, count=12 → fires.
        sn_data, jira_data = _build_data(total=12, unresolved=6)
        ev = evaluate(None, sn_data, jira_data)
        for metric in SIGNAL_METRICS:
            assert metric in ev.raw_evidence, f"{metric} missing from raw_evidence"
            assert isinstance(ev.raw_evidence[metric], (int, float)), (
                f"{metric} must be numeric"
            )


# ─── AC1: fires when all three conditions met ─────────────────────────────────

class TestFires:
    def test_fires_when_all_conditions_met(self):
        # 12 incidents, 6 unresolved (0.50 >= 0.30); closed 2026-05-20.
        # Open issues measured to as_of 2026-06-10 → 21 days; resolved at
        # 2026-06-09 → 20 days. avg ~20.5 >= 14. count 12 >= 10.
        sn_data, jira_data = _build_data(total=12, unresolved=6)
        results = detect(None, sn_data, jira_data)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == DETECTOR_ID
        assert r.signal_source == "servicenow"
        assert r.metric_value == 0.5
        assert r.threshold == UNRESOLVED_PCT_THRESHOLD
        assert r.raw_evidence["incident_count_30d"] == 12
        assert r.raw_evidence["unresolved_count"] == 6
        assert r.raw_evidence["avg_lag_days"] >= AVG_LAG_DAYS_THRESHOLD
        assert r.raw_evidence["max_lag_days"] >= r.raw_evidence["avg_lag_days"]

    def test_fires_at_exact_thresholds(self):
        # 10 incidents (== MIN), 3 unresolved (0.30 == threshold).
        # Closed 2026-05-27, as_of 2026-06-10 → open lag 14 days (== threshold);
        # resolved issues resolved exactly 14 days after close as well.
        sn_data, jira_data = _build_data(
            total=10,
            unresolved=3,
            incident_closed_at="2026-05-27",
            resolved_lag_days_at="2026-06-10",
        )
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["incident_count_30d"] == 10
        assert ev.raw_evidence["unresolved_pct"] == 0.3
        assert ev.raw_evidence["avg_lag_days"] == 14.0
        assert ev.fired is True

    def test_referenced_but_missing_jira_issue_counts_as_unresolved(self):
        # Cross-system join: 4 of 12 referenced Jira issues are absent from the
        # Jira pull → still treated as unresolved (4/12 = 0.333 >= 0.30).
        sn_data, jira_data = _build_data(
            total=12, unresolved=4, drop_unresolved_from_jira=True
        )
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["unresolved_count"] == 4
        assert ev.raw_evidence["incident_count_30d"] == 12
        assert ev.fired is True


# ─── AC2: minimum volume guard ────────────────────────────────────────────────

class TestVolumeGuard:
    def test_does_not_fire_below_min_volume(self):
        # 9 incidents (< 10), all unresolved with huge lag — must NOT fire.
        sn_data, jira_data = _build_data(total=9, unresolved=9)
        assert evaluate(None, sn_data, jira_data).raw_evidence["incident_count_30d"] == 9
        assert detect(None, sn_data, jira_data) == []

    def test_fires_at_exactly_min_volume(self):
        sn_data, jira_data = _build_data(total=MIN_INCIDENT_VOLUME, unresolved=10)
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["incident_count_30d"] == MIN_INCIDENT_VOLUME
        assert ev.fired is True


# ─── Below-threshold non-firing on each condition ─────────────────────────────

class TestDoesNotFire:
    def test_does_not_fire_when_unresolved_pct_below_threshold(self):
        # 12 incidents, 2 unresolved → 0.167 < 0.30. Lag/volume are fine.
        sn_data, jira_data = _build_data(total=12, unresolved=2)
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["unresolved_pct"] < UNRESOLVED_PCT_THRESHOLD
        assert ev.fired is False

    def test_does_not_fire_when_avg_lag_below_threshold(self):
        # Unresolved pct and volume fine, but lag is small: closed 2026-06-05,
        # resolved 2026-06-06, as_of 2026-06-10 → lags well below 14.
        sn_data, jira_data = _build_data(
            total=12,
            unresolved=6,
            incident_closed_at="2026-06-05",
            resolved_lag_days_at="2026-06-06",
            as_of="2026-06-10",
        )
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["avg_lag_days"] < AVG_LAG_DAYS_THRESHOLD
        assert ev.fired is False

    def test_does_not_fire_when_degraded_signal(self):
        sn_data, jira_data = _build_data(total=12, unresolved=12, degraded=True)
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["degraded_signal"] is True
        assert ev.fired is False

    def test_does_not_fire_when_no_data(self):
        assert detect(None, {}, {}) == []
        assert detect(None, None, None) == []

    def test_does_not_fire_when_no_jira_references(self):
        # Closed incidents exist but none reference a Jira issue → nothing to
        # analyse, incident_count_30d == 0.
        sn_data = {
            "incident_resolution": {
                "closed_incidents": [
                    {"incident_id": "INC1", "closed_at": "2026-05-01"},
                    {"incident_id": "INC2", "closed_at": "2026-05-02"},
                ],
                "as_of": AS_OF,
            }
        }
        ev = evaluate(None, sn_data, {})
        assert ev.raw_evidence["incident_count_30d"] == 0
        assert ev.fired is False


# ─── evaluate() contract ──────────────────────────────────────────────────────

class TestEvaluateContract:
    def test_evaluate_unfired_metric_value_is_unresolved_pct(self):
        sn_data, jira_data = _build_data(total=12, unresolved=2)
        ev = evaluate(None, sn_data, jira_data)
        assert ev.fired is False
        assert ev.metric_value == ev.raw_evidence["unresolved_pct"]
        assert ev.detector_id == DETECTOR_ID

    def test_evaluate_handles_standard_jira_issue_metrics_shape(self):
        # The detector also accepts the standard ingest shape where Jira issues
        # live under issue_metrics with fields.status.name / resolutiondate.
        incidents = [_incident(i, "2026-05-20", f"OPS-{i}") for i in range(12)]
        jira_issues = []
        for i in range(12):
            if i < 6:
                jira_issues.append({"key": f"OPS-{i}", "fields": {"status": {"name": "In Progress"}}})
            else:
                jira_issues.append({
                    "key": f"OPS-{i}",
                    "fields": {"status": {"name": "Done"}, "resolutiondate": "2026-06-09"},
                })
        sn_data = {"incident_resolution": {"closed_incidents": incidents, "as_of": AS_OF}}
        jira_data = {"issue_metrics": {"issues": jira_issues}}
        ev = evaluate(None, sn_data, jira_data)
        assert ev.raw_evidence["unresolved_count"] == 6
        assert ev.fired is True