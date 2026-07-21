"""
Contract tests for MSP-B6 T2 (AT-737) — Cloud-Operations pack detectors.

Detectors under test (all read ``sn_data['cloud_ops']``):
  RECURRING_RESOLUTION_LOOP, ALERT_TRIAGE_TOIL, REASSIGNMENT_PING_PONG, QUEUE_AGEING

Acceptance criteria:
  AC1 — a recurrence corroborated by a window-gated event signature ranks with
        higher confidence than the identical ITSM-only recurrence, which is
        labelled single-source and capped; both shown, neither dropped.
  AC2 — no detector output references an individual person (groups/queues/
        services/CIs only).
  AC3 — each detector populates all four contract fields (evidence, confidence,
        corroboration status, source trace).
  AC4 — queue-ageing baseline is per-queue, never global.
"""
from __future__ import annotations

import pytest

from discovery.detectors import (
    cloud_ops_recurring_resolution_loop as rrl,
    cloud_ops_alert_triage_toil as att,
    cloud_ops_reassignment_ping_pong as pp,
    cloud_ops_queue_ageing as qa,
)
from discovery.packs import cloud_ops_finding as fc


def _sn(cloud_ops_block):
    return {"cloud_ops": cloud_ops_block}


def _contract(dr):
    return dr.raw_evidence["finding_contract"]


# ── AC1 — corroborated recurrence outranks the identical ITSM-only recurrence ───

class TestAC1RecurrenceCorroboration:

    def _two_identical_recurrences(self):
        """Same loop twice; one carries a window-gated recurring event signature."""
        base = dict(
            incident_kind="Disk space alert",
            resolution="Clear logs / restart",
            count=6,
            median_ttr_minutes=40,
            assignment_group="Storage Ops",
            affected_services=["billing-api"],
            close_code="Resolved (Workaround)",
            incident_ids=["INC001", "INC002"],
        )
        corroborated = dict(base, signature="disk-loop-A", event_signature="disk.usage.threshold")
        itsm_only = dict(base, signature="disk-loop-B")
        return _sn({
            "recurrence_records": [corroborated, itsm_only],
            "event_signatures": [
                {"signature": "disk.usage.threshold", "event_count": 120,
                 "recurring": True, "window_overlap": True},
            ],
        })

    def test_both_emitted_none_dropped(self):
        results = rrl.detect(None, self._two_identical_recurrences(), None)
        assert len(results) == 2

    def test_corroborated_outranks_itsm_only(self):
        results = {r.raw_evidence["signature"]: r for r in rrl.detect(None, self._two_identical_recurrences(), None)}
        corr = _contract(results["disk-loop-A"])
        itsm = _contract(results["disk-loop-B"])

        assert corr["corroboration"]["status"] == fc.STATUS_CORROBORATED
        assert corr["confidence"]["level"] == fc.CONFIDENCE_HIGH
        assert corr["confidence"]["eligible_for_high"] is True
        assert corr["corroboration"]["window_gated"] is True

        assert itsm["corroboration"]["status"] == fc.STATUS_SINGLE_SOURCE
        assert itsm["confidence"]["level"] == fc.CONFIDENCE_MEDIUM
        assert itsm["confidence"]["capped"] is True
        assert itsm["confidence"]["cap_reason"]

        # Higher confidence for the corroborated one.
        assert fc.CONFIDENCE_ORDER[corr["confidence"]["level"]] > fc.CONFIDENCE_ORDER[itsm["confidence"]["level"]]

    def test_itsm_only_labelled_single_source(self):
        results = {r.raw_evidence["signature"]: r for r in rrl.detect(None, self._two_identical_recurrences(), None)}
        itsm = _contract(results["disk-loop-B"])
        assert itsm["corroboration"]["label"] == fc.SINGLE_SOURCE_LABEL

    def test_event_signature_without_window_overlap_does_not_corroborate(self):
        sn = _sn({
            "recurrence_records": [dict(
                signature="loop-x", incident_kind="k", resolution="r", count=5,
                median_ttr_minutes=10, assignment_group="G", affected_services=["s"],
                incident_ids=["INC9"], event_signature="sig.x")],
            "event_signatures": [{"signature": "sig.x", "recurring": True, "window_overlap": False}],
        })
        [dr] = rrl.detect(None, sn, None)
        assert _contract(dr)["corroboration"]["status"] == fc.STATUS_SINGLE_SOURCE

    def test_effort_is_count_times_median_ttr(self):
        sn = _sn({"recurrence_records": [dict(
            signature="l", incident_kind="k", resolution="r", count=6,
            median_ttr_minutes=40, assignment_group="G", affected_services=["s"],
            incident_ids=["INC1"])]})
        [dr] = rrl.detect(None, sn, None)
        assert dr.raw_evidence["effort_score"] == pytest.approx(6 * 40)

    def test_below_threshold_does_not_fire(self):
        sn = _sn({"recurrence_records": [dict(
            signature="l", incident_kind="k", resolution="r", count=2,
            median_ttr_minutes=40, assignment_group="G", affected_services=["s"],
            incident_ids=["INC1"])]})
        assert rrl.detect(None, sn, None) == []
        assert rrl.evaluate(None, sn, None).fired is False


# ── AC2 — never an individual person ────────────────────────────────────────────

class TestAC2NoIndividuals:

    def test_ping_pong_drops_person_fields_on_hops(self):
        """Even when the raw oscillation record carries person fields, none leak."""
        sn = _sn({"oscillation_records": [{
            "signature": "osc-1",
            "incident_id": "INC500",
            "hop_count": 4,
            "affected_service": "checkout-api",
            "hops": [
                {"from_group": "Network Ops", "to_group": "App Support",
                 "assignee": "Jane Doe", "user_email": "jane@corp.com"},
                {"from_group": "App Support", "to_group": "Network Ops",
                 "assigned_to": "John Roe"},
            ],
            "assignee": "Jane Doe",
        }]})
        [dr] = pp.detect(None, sn, None)
        assert fc.find_individual_references(dr.raw_evidence) == []
        # The person names must not appear anywhere in the serialized finding.
        blob = repr(dr.raw_evidence)
        assert "Jane Doe" not in blob and "John Roe" not in blob and "jane@corp.com" not in blob

    def test_ping_pong_groups_preserved(self):
        sn = _sn({"oscillation_records": [{
            "signature": "osc-1", "incident_id": "INC500", "hop_count": 3,
            "hops": [{"from_group": "Network Ops", "to_group": "App Support"},
                     {"from_group": "App Support", "to_group": "DBA"}],
        }]})
        [dr] = pp.detect(None, sn, None)
        groups = _contract(dr)["evidence"]["groups"]
        assert "Network Ops" in groups and "App Support" in groups

    @pytest.mark.parametrize("mod,block", [
        (rrl, {"recurrence_records": [dict(signature="l", incident_kind="k", resolution="r",
               count=5, median_ttr_minutes=10, assignment_group="G", affected_services=["s"],
               incident_ids=["INC1"], assignee="Someone Person", caller="Other Person")]}),
        (att, {"event_signatures": [dict(signature="e", incident_count=9, median_ttr_minutes=5,
               close_code="Auto", distinct_close_codes=1, window_overlap=True,
               assignment_group="NOC", affected_services=["svc"], opened_by="A Person")]}),
        (qa, {"queues": [dict(queue="Tier 2", current_avg_age_hours=60, baseline_avg_age_hours=30,
              baseline_runs=5, open_count=10, resolved_by="A Person")]}),
    ])
    def test_no_detector_leaks_individuals(self, mod, block):
        results = mod.detect(None, _sn(block), None)
        assert results, "detector should fire for this fixture"
        for dr in results:
            assert fc.find_individual_references(dr.raw_evidence) == []
            assert "Person" not in repr(dr.raw_evidence)


# ── AC3 — four-part contract on every finding ───────────────────────────────────

class TestAC3FourPartContract:

    _CASES = [
        (rrl, {"recurrence_records": [dict(signature="l", incident_kind="Disk", resolution="restart",
               count=5, median_ttr_minutes=10, assignment_group="G", affected_services=["s"],
               incident_ids=["INC1"])]}),
        (att, {"event_signatures": [dict(signature="e", incident_count=9, median_ttr_minutes=5,
               close_code="Auto", distinct_close_codes=1, window_overlap=True,
               assignment_group="NOC", affected_services=["svc"], incident_ids=["INC2"])]}),
        (pp, {"oscillation_records": [dict(signature="o", incident_id="INC3", hop_count=4,
              affected_service="svc", hops=[{"from_group": "A", "to_group": "B"}])]}),
        (qa, {"queues": [dict(queue="Tier 2", current_avg_age_hours=60, baseline_avg_age_hours=30,
              baseline_runs=5, open_count=10)]}),
    ]

    @pytest.mark.parametrize("mod,block", _CASES)
    def test_all_four_parts_present_and_complete(self, mod, block):
        results = mod.detect(None, _sn(block), None)
        assert results
        for dr in results:
            contract = _contract(dr)
            for field in fc.FOUR_PART_CONTRACT_FIELDS:
                assert field in contract and contract[field], f"{mod.DETECTOR_ID} missing {field}"
            assert fc.is_contract_complete(contract)

    @pytest.mark.parametrize("mod,block", _CASES)
    def test_source_trace_resolves_to_systems_and_artifacts(self, mod, block):
        for dr in mod.detect(None, _sn(block), None):
            st = _contract(dr)["source_trace"]
            assert st["systems"], "source trace must name originating systems"
            assert st["artifacts"], "source trace must resolve to artifacts"

    @pytest.mark.parametrize("mod,block", _CASES)
    def test_confidence_and_corroboration_shape(self, mod, block):
        for dr in mod.detect(None, _sn(block), None):
            c = _contract(dr)
            assert c["confidence"]["level"] in fc.CONFIDENCE_ORDER
            assert c["corroboration"]["status"] in (fc.STATUS_CORROBORATED, fc.STATUS_SINGLE_SOURCE)


# ── AC4 — queue-ageing is per-queue, never global ───────────────────────────────

class TestAC4PerQueueBaseline:

    def test_each_queue_judged_against_own_baseline(self):
        """One queue high vs a global mean but normal vs its own baseline (no fire);
        another modest vs the global mean but elevated vs its own baseline (fire)."""
        sn = _sn({"queues": [
            # current 100h but its own baseline is 100h → 0% departure → no fire,
            # even though 100h dwarfs the other queue (a global baseline would fire it).
            dict(queue="Batch (naturally slow)", current_avg_age_hours=100,
                 baseline_avg_age_hours=100, baseline_runs=6, open_count=5),
            # current 12h, own baseline 8h → 50% departure → fires, though 12h is
            # small next to the other queue (a global baseline would miss it).
            dict(queue="Interactive", current_avg_age_hours=12,
                 baseline_avg_age_hours=8, baseline_runs=6, open_count=20),
        ]})
        fired = {dr.raw_evidence["queue"]: dr for dr in qa.detect(None, sn, None)}
        assert "Interactive" in fired
        assert "Batch (naturally slow)" not in fired
        assert fired["Interactive"].raw_evidence["baseline_scope"] == "per_queue"
        assert _contract(fired["Interactive"])["evidence"]["baseline_avg_age_hours"] == 8

    def test_unbaselined_queue_does_not_fire(self):
        sn = _sn({"queues": [dict(queue="New", current_avg_age_hours=99,
                  baseline_avg_age_hours=0, baseline_runs=6, open_count=5)]})
        assert qa.detect(None, sn, None) == []

    def test_insufficient_baseline_runs_does_not_fire(self):
        sn = _sn({"queues": [dict(queue="Q", current_avg_age_hours=60,
                  baseline_avg_age_hours=30, baseline_runs=1, open_count=5)]})
        assert qa.detect(None, sn, None) == []

    def test_departure_pct_is_metric_value(self):
        sn = _sn({"queues": [dict(queue="Q", current_avg_age_hours=45,
                  baseline_avg_age_hours=30, baseline_runs=6, open_count=5)]})
        [dr] = qa.detect(None, sn, None)
        assert dr.metric_value == pytest.approx(0.5)


# ── alert-triage-toil firing shape ──────────────────────────────────────────────

class TestAlertTriageToil:

    def _ev(self, **over):
        base = dict(signature="cpu.alert", incident_count=12, event_count=200,
                    median_ttr_minutes=6, close_code="Auto-resolved", distinct_close_codes=1,
                    window_overlap=True, assignment_group="NOC Tier 1", affected_services=["api"],
                    incident_ids=["INC10"])
        base.update(over)
        return _sn({"event_signatures": [base]})

    def test_fires_on_high_volume_trivial_resolution(self):
        [dr] = att.detect(None, self._ev(), None)
        assert _contract(dr)["corroboration"]["status"] == fc.STATUS_CORROBORATED
        assert _contract(dr)["confidence"]["level"] == fc.CONFIDENCE_HIGH

    def test_no_fire_when_ttr_too_long(self):
        assert att.detect(None, self._ev(median_ttr_minutes=120), None) == []

    def test_no_fire_when_multiple_close_codes(self):
        assert att.detect(None, self._ev(distinct_close_codes=3), None) == []

    def test_no_fire_when_low_volume(self):
        assert att.detect(None, self._ev(incident_count=1), None) == []

    def test_no_fire_without_window_overlap(self):
        assert att.detect(None, self._ev(window_overlap=False), None) == []


# ── config-driven thresholds (T1 AC2 carried into detectors) ────────────────────

class TestConfigDrivenThresholds:

    def test_ping_pong_uses_config_min_hops(self, monkeypatch):
        # Force min_hops via the config accessor the detector reads.
        monkeypatch.setattr(pp, "get_detector_thresholds", lambda section, defaults: {"min_hops": 10})
        sn = _sn({"oscillation_records": [dict(signature="o", incident_id="INC3", hop_count=4,
                  hops=[{"from_group": "A", "to_group": "B"}])]})
        assert pp.detect(None, sn, None) == []  # 4 < 10

    def test_recurrence_uses_config_min_occurrences(self, monkeypatch):
        monkeypatch.setattr(rrl, "get_detector_thresholds", lambda section, defaults: {"min_occurrences": 2})
        sn = _sn({"recurrence_records": [dict(signature="l", incident_kind="k", resolution="r",
                  count=2, median_ttr_minutes=10, assignment_group="G",
                  affected_services=["s"], incident_ids=["INC1"])]})
        assert len(rrl.detect(None, sn, None)) == 1  # 2 >= 2


# ── empty / missing block is safe ───────────────────────────────────────────────

class TestEmptyInputs:

    @pytest.mark.parametrize("mod", [rrl, att, pp, qa])
    def test_no_block_no_fire(self, mod):
        assert mod.detect(None, {}, None) == []
        assert mod.detect(None, None, None) == []
        assert mod.evaluate(None, {}, None).fired is False


# ── detector identity + pack wiring ─────────────────────────────────────────────

class TestDetectorIdentityAndWiring:

    def test_detector_ids(self):
        assert rrl.DETECTOR_ID == "RECURRING_RESOLUTION_LOOP"
        assert att.DETECTOR_ID == "ALERT_TRIAGE_TOIL"
        assert pp.DETECTOR_ID == "REASSIGNMENT_PING_PONG"
        assert qa.DETECTOR_ID == "QUEUE_AGEING"

    def test_all_registered_in_pack(self):
        from discovery.packs.pack_config import get_detector_modules
        mods = get_detector_modules("cloud_ops")
        assert len(mods) == 4
        for name in (
            "cloud_ops_recurring_resolution_loop",
            "cloud_ops_alert_triage_toil",
            "cloud_ops_reassignment_ping_pong",
            "cloud_ops_queue_ageing",
        ):
            assert any(name in m for m in mods)

    def test_each_module_has_signal_metrics(self):
        for mod in (rrl, att, pp, qa):
            assert isinstance(mod.SIGNAL_METRICS, list) and mod.SIGNAL_METRICS

    def test_evaluate_detect_agree(self):
        sn = _sn({"recurrence_records": [dict(signature="l", incident_kind="k", resolution="r",
                  count=5, median_ttr_minutes=10, assignment_group="G",
                  affected_services=["s"], incident_ids=["INC1"])]})
        assert rrl.evaluate(None, sn, None).fired == bool(rrl.detect(None, sn, None))
