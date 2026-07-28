"""
Contract tests for MSP-B6 T6 (AT-741) — four-part-contract enforcement at the
pack boundary + graceful MSP-B5-absent degradation.

Acceptance criteria:
  AC1 — A seeded estate produces findings from at least 4 detectors, each carrying
        all four contract parts (evidence, confidence, corroboration status, source
        trace); a finding missing a part FAILS the run's pack execution
        (CloudOpsContractViolation).
  AC2 — With MSP-B5 (runbook matching) absent, the composite recurrence finding
        degrades to repeated-manual only, carrying an explicit
        "runbook match unavailable" label — never silently narrower.
"""
from __future__ import annotations

import pytest

from discovery.detectors import (
    cloud_ops_recurring_resolution_loop as rrl,
    cloud_ops_alert_triage_toil as att,
    cloud_ops_reassignment_ping_pong as pp,
    cloud_ops_queue_ageing as qa,
    cloud_ops_shared_ci_hotspot as hs,
)
from discovery.packs import cloud_ops_finding as fc


def _sn(block):
    return {"cloud_ops": block}


# ── A seeded estate exercising >= 4 detectors (AC1) ─────────────────────────────


def _seeded_estate():
    return _sn({
        "recurrence_records": [dict(
            signature="disk-loop", incident_kind="Disk space", resolution="clear logs",
            count=6, median_ttr_minutes=40, assignment_group="Storage Ops",
            affected_services=["billing-api"], close_code="Workaround",
            incident_ids=["INC001", "INC002"], event_signature="disk.usage",
        )],
        "event_signatures": [
            {"signature": "disk.usage", "event_count": 120, "recurring": True, "window_overlap": True},
            {"signature": "cpu.alert", "incident_count": 12, "event_count": 200,
             "median_ttr_minutes": 6, "close_code": "Auto", "distinct_close_codes": 1,
             "window_overlap": True, "assignment_group": "NOC", "affected_services": ["api"],
             "incident_ids": ["INC010"], "ci": "shared-db"},
        ],
        "oscillation_records": [dict(
            signature="osc-1", incident_id="INC500", hop_count=4, affected_service="checkout",
            hops=[{"from_group": "Network Ops", "to_group": "App Support"},
                  {"from_group": "App Support", "to_group": "DBA"}],
        )],
        "queues": [dict(queue="Tier 2", current_avg_age_hours=60, baseline_avg_age_hours=30,
                        baseline_runs=6, open_count=10)],
        # Shared-CI hotspot: three services → common CI within 1 hop, event-corroborated.
        "ci_graph": {"edges": [
            {"from": "svc-a-ci", "to": "shared-db"},
            {"from": "svc-b-ci", "to": "shared-db"},
            {"from": "svc-c-ci", "to": "shared-db"},
        ]},
        "service_incidents": [
            {"service": "svc-a", "ci": "svc-a-ci", "incident_ids": ["INC-A1"], "incident_count": 1},
            {"service": "svc-b", "ci": "svc-b-ci", "incident_ids": ["INC-B1"], "incident_count": 1},
            {"service": "svc-c", "ci": "svc-c-ci", "incident_ids": ["INC-C1"], "incident_count": 1},
        ],
    })


def _all_findings(sn):
    results = []
    for mod in (rrl, att, pp, qa, hs):
        results.extend(mod.detect(None, sn, None))
    return results


class TestAC1PackBoundaryEnforcement:

    def test_seeded_estate_fires_at_least_four_detectors(self):
        sn = _seeded_estate()
        fired_detectors = {dr.detector_id for dr in _all_findings(sn)}
        assert len(fired_detectors) >= 4, fired_detectors

    def test_every_finding_carries_all_four_parts(self):
        findings = _all_findings(_seeded_estate())
        for dr in findings:
            contract = dr.raw_evidence["finding_contract"]
            assert fc.is_contract_complete(contract)
            assert fc.missing_contract_parts(contract) == []

    def test_enforce_pack_findings_passes_for_complete_estate(self):
        findings = _all_findings(_seeded_estate())
        assert fc.enforce_pack_findings(findings) == len(findings)

    def test_missing_part_fails_the_run(self):
        """A finding missing any part is a contract violation that fails the pack."""
        findings = _all_findings(_seeded_estate())
        # Corrupt one finding: drop its source_trace (a required part).
        victim = findings[0]
        del victim.raw_evidence["finding_contract"]["source_trace"]
        with pytest.raises(fc.CloudOpsContractViolation) as exc:
            fc.enforce_pack_findings(findings)
        assert "source_trace" in str(exc.value)

    @pytest.mark.parametrize("part", list(fc.FOUR_PART_CONTRACT_FIELDS))
    def test_each_missing_part_is_a_violation(self, part):
        [dr] = rrl.detect(None, _sn({"recurrence_records": [dict(
            signature="l", incident_kind="k", resolution="r", count=5,
            median_ttr_minutes=10, assignment_group="G", affected_services=["s"],
            incident_ids=["INC1"])]}), None)
        del dr.raw_evidence["finding_contract"][part]
        with pytest.raises(fc.CloudOpsContractViolation):
            fc.enforce_pack_findings([dr])

    def test_no_contract_at_all_is_a_violation(self):
        class _R:
            detector_id = "QUEUE_AGEING"
            raw_evidence = {"departure_pct": 0.5}
        with pytest.raises(fc.CloudOpsContractViolation):
            fc.enforce_pack_findings([_R()])

    def test_individual_reference_is_a_violation(self):
        [dr] = qa.detect(None, _sn({"queues": [dict(
            queue="Q", current_avg_age_hours=60, baseline_avg_age_hours=30,
            baseline_runs=6, open_count=5)]}), None)
        # Inject a forbidden individual reference into the contract.
        dr.raw_evidence["finding_contract"]["evidence"]["assignee"] = "Jane Doe"
        with pytest.raises(fc.CloudOpsContractViolation):
            fc.enforce_pack_findings([dr])

    def test_empty_findings_is_fine(self):
        assert fc.enforce_pack_findings([]) == 0


# ── AC2 — graceful MSP-B5-absent degradation, labelled ──────────────────────────


class TestAC2RunbookDegradation:

    _REC = dict(
        signature="disk-loop", incident_kind="Disk space", resolution="clear logs",
        count=6, median_ttr_minutes=40, assignment_group="Storage Ops",
        affected_services=["billing-api"], close_code="Workaround", incident_ids=["INC1"],
    )

    def test_b5_absent_degrades_to_repeated_manual_with_label(self):
        [dr] = rrl.detect(None, _sn({"recurrence_records": [dict(self._REC)]}), None)
        leg = dr.raw_evidence["finding_contract"]["evidence"]["composite"]
        assert leg["kind"] == fc.LEG_REPEATED_MANUAL
        assert leg["documented"] is False
        assert leg["degraded"] is True
        assert leg["label"] == fc.RUNBOOK_MATCH_UNAVAILABLE_LABEL
        assert leg["label"] == "runbook match unavailable"
        # Never silently narrower: the mirror flags the missing capability.
        assert dr.raw_evidence["runbook_match_available"] is False
        assert dr.raw_evidence["finding_kind"] == fc.LEG_REPEATED_MANUAL

    def test_b5_absent_still_carries_complete_contract(self):
        [dr] = rrl.detect(None, _sn({"recurrence_records": [dict(self._REC)]}), None)
        assert fc.is_contract_complete(dr.raw_evidence["finding_contract"])
        assert fc.enforce_pack_findings([dr]) == 1

    def test_b5_present_produces_documented_composite(self):
        block = {
            "recurrence_records": [dict(self._REC, runbook_match={"runbook_id": "RB-9", "title": "Clear logs"})],
            "runbook_matching": {"available": True},
        }
        [dr] = rrl.detect(None, _sn(block), None)
        leg = dr.raw_evidence["finding_contract"]["evidence"]["composite"]
        assert leg["kind"] == fc.LEG_DOCUMENTED_REPEATED_MANUAL
        assert leg["documented"] is True
        assert leg["runbook_id"] == "RB-9"
        assert dr.raw_evidence["runbook_match_available"] is True

    def test_b5_present_via_run_level_matches_map(self):
        """B5 may supply matches at the run level keyed by recurrence signature."""
        block = {
            "recurrence_records": [dict(self._REC)],
            "runbook_matching": {"available": True, "matches": {"disk-loop": {"runbook_id": "RB-42"}}},
        }
        [dr] = rrl.detect(None, _sn(block), None)
        leg = dr.raw_evidence["finding_contract"]["evidence"]["composite"]
        assert leg["kind"] == fc.LEG_DOCUMENTED_REPEATED_MANUAL
        assert leg["runbook_id"] == "RB-42"

    def test_b5_flag_false_degrades_even_if_match_present(self):
        """An explicit unavailable flag degrades, even if a stale match is present."""
        block = {
            "recurrence_records": [dict(self._REC, runbook_match={"runbook_id": "RB-9"})],
            "runbook_matching": {"available": False},
        }
        [dr] = rrl.detect(None, _sn(block), None)
        leg = dr.raw_evidence["finding_contract"]["evidence"]["composite"]
        assert leg["kind"] == fc.LEG_REPEATED_MANUAL
        assert leg["label"] == fc.RUNBOOK_MATCH_UNAVAILABLE_LABEL

    def test_runbook_matching_available_helper(self):
        assert fc.runbook_matching_available({"runbook_matching": {"available": True}}) is True
        assert fc.runbook_matching_available({"runbook_matching": {"available": False}}) is False
        assert fc.runbook_matching_available({"runbook_matching": {}}) is True   # present => available
        assert fc.runbook_matching_available({}) is False                         # absent => unavailable


# ── build helpers (unit) ─────────────────────────────────────────────────────────


class TestRunbookLegBuilder:

    def test_documented_leg(self):
        leg = fc.build_runbook_leg(runbook_match={"runbook_id": "RB-1"}, b5_available=True)
        assert leg == {
            "kind": "documented_repeated_manual", "documented": True, "b5_available": True,
            "degraded": False, "provisional": False, "runbook_state": "observed",
            "label": "Observed runbook match", "runbook_id": "RB-1",
            "runbook_title": "",
        }

    def test_degraded_leg(self):
        leg = fc.build_runbook_leg(runbook_match=None, b5_available=False)
        assert leg["kind"] == "repeated_manual"
        assert leg["degraded"] is True
        assert leg["label"] == "runbook match unavailable"

    def test_b5_available_but_no_match_is_not_an_outage(self):
        leg = fc.build_runbook_leg(runbook_match=None, b5_available=True)
        assert leg["kind"] == "repeated_manual"
        assert leg["degraded"] is False
        assert leg["runbook_state"] == "absent"
        assert leg["label"] == "no runbook match"

    def test_canonical_proposed_match_stays_visibly_provisional(self):
        leg = fc.build_runbook_leg(
            runbook_match={
                "match_state": "proposed",
                "runbook": {
                    "source_artifact": "page-42",
                    "title": "Restart the worker",
                },
            },
            b5_available=True,
        )
        assert leg["documented"] is False
        assert leg["provisional"] is True
        assert leg["runbook_state"] == "proposed"
        assert leg["runbook_id"] == "page-42"
        assert leg["runbook_title"] == "Restart the worker"
