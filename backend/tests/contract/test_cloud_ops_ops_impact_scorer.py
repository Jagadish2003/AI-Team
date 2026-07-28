"""
Contract tests for MSP-B6 T4 (AT-739) — Cloud-Operations ops-impact scorer.

Acceptance criteria:
  AC1 — the scorer ranks findings using all four Section-2 dimensions
        (effort concentration, breadth, recurrence stability, automation shape).
  AC2 — all calibration weights load from config; changing a weight alters the
        ranked order with no code change.
  AC3 — in a seeded case, a trivially-resolved finding ranks above a
        judgment-heavy finding of equal effort / breadth / recurrence.
"""
from __future__ import annotations

import json

import pytest

from discovery.models import DetectorResult
from discovery.packs import cloud_ops_scorer as scorer
from discovery.packs import cloud_ops_config as cfg
from discovery.packs import cloud_ops_finding as fc


# ── helpers ──────────────────────────────────────────────────────────────────


def _finding(detector_id, *, evidence, confidence="MEDIUM", corroborated=False, signature=""):
    """Build a cloud_ops DetectorResult carrying a four-part contract in evidence,
    the same raw_evidence shape the real detectors emit."""
    contract = {
        "evidence": dict(evidence),
        "confidence": {"level": confidence},
        "corroboration": {"status": "corroborated" if corroborated else "single_source"},
        "source_trace": {"systems": ["servicenow"], "artifacts": [{"type": "x", "id": "1"}]},
    }
    raw = {
        **evidence,
        "signature": signature or detector_id.lower(),
        "confidence": confidence,
        "corroborated": corroborated,
        "corroboration_sources": ["servicenow"],
        "finding_contract": contract,
    }
    return DetectorResult(
        detector_id=detector_id,
        signal_source="servicenow",
        metric_value=1.0,
        threshold=0.0,
        raw_evidence=raw,
    )


def _order(ranking, findings):
    """Return findings sorted by the ranking's assigned rank (1-based)."""
    return sorted(findings, key=lambda dr: ranking[id(dr)]["rank"])


# ── AC1 — ranks using all four dimensions ──────────────────────────────────────


class TestAC1FourDimensions:

    def test_ranking_covers_all_four_dimensions(self):
        f = _finding("RECURRING_RESOLUTION_LOOP", evidence={
            "recurrence_count": 6, "median_ttr_minutes": 40, "effort_score": 240,
            "affected_services": ["a", "b"],
        })
        ranking = scorer.rank_cloud_ops_findings([f])
        dims = ranking[id(f)]["dimensions"]
        for d in scorer.DIMENSIONS:
            assert d in dims
        assert set(ranking[id(f)]["weights"]) == set(scorer.DIMENSIONS)

    def test_higher_effort_ranks_higher_all_else_equal(self):
        big = _finding("RECURRING_RESOLUTION_LOOP", signature="big", evidence={
            "effort_score": 1000, "breadth": 2,
            "recurrence_stability": 0.6, "automation_shape": 0.5,
        })
        small = _finding("RECURRING_RESOLUTION_LOOP", signature="small", evidence={
            "effort_score": 100, "breadth": 2,
            "recurrence_stability": 0.6, "automation_shape": 0.5,
        })
        ranking = scorer.rank_cloud_ops_findings([small, big])
        assert _order(ranking, [small, big])[0] is big
        assert ranking[id(big)]["ops_impact_score"] > ranking[id(small)]["ops_impact_score"]

    def test_broader_finding_ranks_higher_all_else_equal(self):
        broad = _finding("SHARED_CI_HOTSPOT", signature="broad", evidence={
            "effort_score": 100, "breadth": 12,
            "recurrence_stability": 0.6, "automation_shape": 0.5,
        })
        narrow = _finding("SHARED_CI_HOTSPOT", signature="narrow", evidence={
            "effort_score": 100, "breadth": 3,
            "recurrence_stability": 0.6, "automation_shape": 0.5,
        })
        ranking = scorer.rank_cloud_ops_findings([narrow, broad])
        assert _order(ranking, [broad, narrow])[0] is broad

    def test_steady_recurrence_ranks_higher_than_burst(self):
        steady = _finding("RECURRING_RESOLUTION_LOOP", signature="steady", evidence={
            "effort_score": 100, "breadth": 2,
            "recurrence_shape": "steady", "automation_shape": 0.5,
        })
        burst = _finding("RECURRING_RESOLUTION_LOOP", signature="burst", evidence={
            "effort_score": 100, "breadth": 2,
            "recurrence_shape": "burst", "automation_shape": 0.5,
        })
        ranking = scorer.rank_cloud_ops_findings([burst, steady])
        assert ranking[id(steady)]["ops_impact_score"] > ranking[id(burst)]["ops_impact_score"]


# ── AC3 — trivially-resolved outranks judgment-heavy (equal else) ───────────────


class TestAC3AutomationShape:

    def _pair(self):
        """Two findings, equal effort/breadth/recurrence; one trivially resolved
        (short MTTR, single close code), one judgment-heavy (long MTTR)."""
        trivial = _finding("ALERT_TRIAGE_TOIL", signature="trivial", evidence={
            "effort_score": 500, "breadth": 3, "recurrence_stability": 0.6,
            "median_ttr_minutes": 5, "distinct_close_codes": 1,
        })
        judgment = _finding("RECURRING_RESOLUTION_LOOP", signature="judgment", evidence={
            "effort_score": 500, "breadth": 3, "recurrence_stability": 0.6,
            "median_ttr_minutes": 300, "distinct_close_codes": 4,
        })
        return trivial, judgment

    def test_trivial_outranks_judgment_heavy(self):
        trivial, judgment = self._pair()
        ranking = scorer.rank_cloud_ops_findings([judgment, trivial])
        assert ranking[id(trivial)]["rank"] == 1
        assert ranking[id(judgment)]["rank"] == 2
        assert ranking[id(trivial)]["ops_impact_score"] > ranking[id(judgment)]["ops_impact_score"]

    def test_only_automation_shape_dimension_differs(self):
        trivial, judgment = self._pair()
        ranking = scorer.rank_cloud_ops_findings([judgment, trivial])
        td, jd = ranking[id(trivial)]["normalized"], ranking[id(judgment)]["normalized"]
        # Effort/breadth/recurrence are equal after normalisation; automation shape is not.
        assert td["effort_concentration"] == pytest.approx(jd["effort_concentration"])
        assert td["breadth"] == pytest.approx(jd["breadth"])
        assert td["recurrence_stability"] == pytest.approx(jd["recurrence_stability"])
        assert td["automation_shape"] > jd["automation_shape"]

    def test_trivial_gets_lower_effort_to_automate(self):
        trivial, judgment = self._pair()
        ranking = scorer.rank_cloud_ops_findings([judgment, trivial])
        st = scorer.score_cloud_ops(trivial, ranking=ranking)
        sj = scorer.score_cloud_ops(judgment, ranking=ranking)
        assert st["effort"] <= sj["effort"]


# ── AC2 — weights load from config; a change alters ranked order (no code) ──────


class TestAC2ConfigDrivenWeights:

    def _pair(self):
        """A: high effort, low automation. B: low effort, high automation.
        Effort-heavy weights favour A; automation-heavy weights favour B."""
        a = _finding("RECURRING_RESOLUTION_LOOP", signature="A", evidence={
            "effort_score": 1000, "breadth": 2,
            "recurrence_stability": 0.5, "automation_shape": 0.0,
        })
        b = _finding("ALERT_TRIAGE_TOIL", signature="B", evidence={
            "effort_score": 100, "breadth": 2,
            "recurrence_stability": 0.5, "automation_shape": 1.0,
        })
        return a, b

    def _write_config(self, tmp_path, weights):
        raw = {
            "packVersion": "9.9.9",
            "terminology": {"glossary": {t: f"def-{t}" for t in cfg.REQUIRED_NOC_TERMS}},
            "thresholds": {},
            "calibration": {"impact_weights": weights, "confidence": {"single_source_cap": "MEDIUM"}},
        }
        p = tmp_path / "cloud_ops_pack_config.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        return str(p)

    def test_weights_load_from_config(self):
        # The live config supplies the weights the scorer ranks with.
        cal = cfg.get_calibration()
        a, b = self._pair()
        ranking = scorer.rank_cloud_ops_findings([a, b], calibration=cal)
        assert ranking[id(a)]["weights"]["effort_concentration"] == pytest.approx(
            cal.impact_weights["effort_concentration"]
        )

    def test_weight_change_flips_ranked_order(self, tmp_path):
        a, b = self._pair()

        effort_heavy = cfg.get_calibration(self._write_config(_mkdir(tmp_path, "e"), {
            "effort_concentration": 0.9, "breadth": 0.05,
            "recurrence_stability": 0.025, "automation_shape": 0.025,
        }))
        automation_heavy = cfg.get_calibration(self._write_config(_mkdir(tmp_path, "a"), {
            "effort_concentration": 0.05, "breadth": 0.05,
            "recurrence_stability": 0.025, "automation_shape": 0.875,
        }))

        r_effort = scorer.rank_cloud_ops_findings([a, b], calibration=effort_heavy)
        r_auto = scorer.rank_cloud_ops_findings([a, b], calibration=automation_heavy)

        # Effort-heavy config: A (big effort) ranks first.
        assert r_effort[id(a)]["rank"] == 1
        # Automation-heavy config: B (trivially resolved) ranks first — order flipped
        # purely by editing config weights, with no code change.
        assert r_auto[id(b)]["rank"] == 1


# ── per-finding score shape + confidence pass-through ───────────────────────────


class TestScoreShape:

    def test_is_cloud_ops_detector(self):
        assert scorer.is_cloud_ops_detector("RECURRING_RESOLUTION_LOOP")
        assert scorer.is_cloud_ops_detector("SHARED_CI_HOTSPOT")
        assert not scorer.is_cloud_ops_detector("HANDOFF_FRICTION")

    def test_score_shape_and_confidence_passthrough(self):
        f = _finding("ALERT_TRIAGE_TOIL", confidence="HIGH", corroborated=True, evidence={
            "effort_score": 500, "breadth": 3, "median_ttr_minutes": 5, "distinct_close_codes": 1,
        })
        scored = scorer.score_cloud_ops(f)
        for key in ("tier", "impact", "effort", "confidence", "roadmap_stage", "score_debug",
                    "ops_impact_score", "ops_impact_rank"):
            assert key in scored
        # Confidence is NOT recomputed — it is the detector's honest contract level.
        assert scored["confidence"] == "HIGH"
        assert scored["corroborated"] is True
        assert 1 <= scored["impact"] <= 10
        assert scored["score_debug"]["scorer"] == "cloud_ops"

    def test_unknown_detector_gets_default_and_warns(self, caplog):
        f = _finding("NOT_A_CLOUD_OPS_DETECTOR", evidence={"effort_score": 10})
        scored = scorer.score_cloud_ops(f)
        assert scored["score_debug"].get("note") == "unknown detector - default score applied"

    def test_isolated_scoring_without_ranking(self):
        f = _finding("QUEUE_AGEING", evidence={
            "open_count": 20, "current_avg_age_hours": 12,
        })
        scored = scorer.score_cloud_ops(f)  # no ranking passed
        assert scored["ops_impact_score"] >= 0.0
        assert scored["ops_impact_rank"] == 1


def _mkdir(base, name):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d
