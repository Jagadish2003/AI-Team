"""
MSP-B6 T7 (AT-742) — Section-4 acceptance suite for the Cloud-Operations pack.

This is the consolidated acceptance GATE: every criterion in MSP-B6 Section 4
(AC1–AC7) is expressed here as an automated assertion that FAILS WHEN VIOLATED,
and each AC is paired with an explicit negative control proving the guard bites.
It is pure test code — no product logic (that lives in T1–T6). Because it lives in
``backend/tests/contract/`` it runs in the merge-blocking Contract Tests CI job
(``.github/workflows/contract-tests.yml`` → ``pytest tests/contract/``), which is
asserted by ``TestTicketAC2RunsInCI`` (this ticket's own AC2).

Section 4 map:
  AC1 — seeded estate → findings from >= 4 detectors, each carrying all four
        contract parts; the pack FAILS on any missing part.
  AC2 — corroborated recurrence outranks the identical ITSM-only recurrence
        (labelled single-source, capped); both shown.
  AC3 — shared-CI hotspot fires within the depth bound with corroboration, and
        does NOT fire beyond the bound / without corroboration.
  AC4 — hotspot wording concentration-shaped, never causal (template-level check).
  AC5 — selecting the Managed Cloud Operations template pre-populates systems,
        roles, focus, pack — editable, recorded — with zero template-model code.
  AC6 — with MSP-B5 absent, the composite degrades to repeated-manual with an
        explicit "runbook match unavailable" label — never silently narrower.
  AC7 — no detector output references an individual (pack-wide sweep).
"""
from __future__ import annotations

import importlib

import pytest

from discovery.packs import cloud_ops_finding as fc
from discovery.packs.pack_config import get_detector_modules


# ── Detectors sourced from the REGISTERED pack (not a hardcoded list) ───────────

def _pack_detector_modules():
    return [importlib.import_module(path) for path in get_detector_modules("cloud_ops")]


def _run_pack(sn_data):
    """Run every registered cloud_ops detector, as the runner does, and flatten."""
    results = []
    for mod in _pack_detector_modules():
        results.extend(mod.detect(None, sn_data, None))
    return results


def _contract(dr):
    return dr.raw_evidence["finding_contract"]


# ── The seeded estate: exercises all five detectors in one ITSM/event block ─────

def seeded_estate(*, b5_available=False, hotspot_event=True, hotspot_beyond_bound=False):
    edges = [
        {"from": "billing-api-ci", "to": "storage-tier"},
        {"from": "invoice-ci", "to": "storage-tier"},
        {"from": "payments-ci", "to": "cache-tier"},
        {"from": "cache-tier", "to": "storage-tier"},
    ]
    service_incidents = [
        {"service": "billing-api", "ci": "billing-api-ci", "incident_ids": ["INC1", "INC2"], "incident_count": 2},
        {"service": "invoice-svc", "ci": "invoice-ci", "incident_ids": ["INC3"], "incident_count": 1},
        {"service": "payments", "ci": "payments-ci", "incident_ids": ["INC4", "INC5"], "incident_count": 2},
    ]
    if hotspot_beyond_bound:
        # Push the shared CI to 3 hops for every service → beyond the 2-hop bound.
        edges = [
            {"from": "billing-api-ci", "to": "mid-a"},
            {"from": "mid-a", "to": "mid-b"},
            {"from": "mid-b", "to": "storage-tier"},
            {"from": "invoice-ci", "to": "mid-a"},
            {"from": "payments-ci", "to": "mid-a"},
        ]

    event_signatures = [
        # Recurrence corroborator: recurring + window-overlapping (no TTR → won't
        # trip alert-triage-toil, which requires a short positive TTR).
        {"signature": "disk.usage.threshold", "event_count": 120, "recurring": True, "window_overlap": True},
        # Alert-triage-toil: high volume, trivially resolved, single close code, in window.
        {"signature": "cpu.autoscale.alert", "incident_count": 15, "event_count": 300,
         "median_ttr_minutes": 6, "close_code": "Auto-resolved", "distinct_close_codes": 1,
         "window_overlap": True, "assignment_group": "NOC Tier 1", "affected_services": ["billing-api"],
         "incident_ids": ["INC50", "INC51"]},
    ]
    if hotspot_event:
        # CI-level event on the shared dependency, in window (hotspot corroboration).
        event_signatures.append(
            {"signature": "disk.saturation", "ci": "storage-tier", "window_overlap": True, "event_count": 40})

    # Two identical recurrences of the SAME pattern: one event-corroborated, one ITSM-only.
    recurrence_base = dict(
        incident_kind="Disk space alert", resolution="Clear logs / restart",
        count=6, median_ttr_minutes=40, assignment_group="Storage Ops",
        affected_services=["billing-api"], close_code="Resolved (Workaround)",
        incident_ids=["INC10", "INC11"],
    )
    block = {
        "recurrence_records": [
            dict(recurrence_base, signature="disk-loop-A", event_signature="disk.usage.threshold"),
            dict(recurrence_base, signature="disk-loop-B"),  # ITSM-only, identical pattern
        ],
        "oscillation_records": [{
            "signature": "net-vs-app", "incident_id": "INC500", "hop_count": 4,
            "affected_service": "checkout-api",
            "hops": [
                {"from_group": "Network Ops", "to_group": "App Support"},
                {"from_group": "App Support", "to_group": "Network Ops"},
                {"from_group": "Network Ops", "to_group": "DBA"},
            ],
        }],
        "queues": [{
            "queue": "Tier 2 Escalations", "current_avg_age_hours": 60.0,
            "baseline_avg_age_hours": 30.0, "baseline_runs": 6, "open_count": 20,
        }],
        "ci_graph": {"edges": edges},
        "service_incidents": service_incidents,
        "event_signatures": event_signatures,
    }
    if b5_available:
        block["runbook_matching"] = {
            "available": True,
            "matches": {"disk-loop-A": {"runbook_id": "RB-1", "title": "Disk cleanup runbook"}},
        }
    return {"cloud_ops": block}


# ── AC1 — >= 4 detectors, four-part contract, pack fails on any missing part ────

class TestAC1FourPartAcrossFourDetectors:

    def test_at_least_four_detectors_fire(self):
        results = _run_pack(seeded_estate())
        detector_ids = {dr.detector_id for dr in results}
        assert len(detector_ids) >= 4, f"expected >=4 detectors, got {sorted(detector_ids)}"

    def test_every_finding_carries_all_four_parts(self):
        for dr in _run_pack(seeded_estate()):
            contract = _contract(dr)
            assert fc.is_contract_complete(contract), (
                f"{dr.detector_id} missing {fc.missing_contract_parts(contract)}")
            assert contract["source_trace"]["systems"] and contract["source_trace"]["artifacts"]

    def test_pack_boundary_enforcement_passes_on_valid_estate(self):
        results = _run_pack(seeded_estate())
        validated = fc.enforce_pack_findings(results)
        assert validated == len(results) and validated >= 4

    def test_pack_fails_when_a_part_is_missing(self):
        """The negative control: strip a part → the pack boundary must FAIL."""
        results = _run_pack(seeded_estate())
        broken = results[0]
        broken.raw_evidence["finding_contract"].pop("evidence")
        with pytest.raises(fc.CloudOpsContractViolation):
            fc.enforce_pack_findings(results)

    def test_pack_fails_when_contract_absent(self):
        results = _run_pack(seeded_estate())
        results[0].raw_evidence.pop("finding_contract")
        with pytest.raises(fc.CloudOpsContractViolation):
            fc.enforce_pack_findings(results)


# ── AC2 — corroborated recurrence outranks identical ITSM-only ──────────────────

class TestAC2RecurrenceConfidence:

    def _recurrences(self):
        rrl = importlib.import_module("discovery.detectors.cloud_ops_recurring_resolution_loop")
        results = {dr.raw_evidence["signature"]: dr for dr in rrl.detect(None, seeded_estate(), None)}
        return results["disk-loop-A"], results["disk-loop-B"]

    def test_both_emitted(self):
        corr, itsm = self._recurrences()
        assert corr is not None and itsm is not None

    def test_corroborated_higher_confidence_than_itsm_only(self):
        corr, itsm = self._recurrences()
        cc, ic = _contract(corr)["confidence"], _contract(itsm)["confidence"]
        assert fc.CONFIDENCE_ORDER[cc["level"]] > fc.CONFIDENCE_ORDER[ic["level"]]
        assert cc["level"] == fc.CONFIDENCE_HIGH

    def test_itsm_only_capped_and_labelled_single_source(self):
        _corr, itsm = self._recurrences()
        c = _contract(itsm)
        assert c["confidence"]["capped"] is True and c["confidence"]["cap_reason"]
        assert c["corroboration"]["status"] == fc.STATUS_SINGLE_SOURCE
        assert c["corroboration"]["label"] == fc.SINGLE_SOURCE_LABEL

    def test_corroborated_is_window_gated(self):
        corr, _itsm = self._recurrences()
        assert _contract(corr)["corroboration"]["window_gated"] is True


# ── AC3 — shared-CI hotspot fires within bound + corroboration; else not ────────

class TestAC3SharedCiHotspot:

    def _hot(self):
        return importlib.import_module("discovery.detectors.cloud_ops_shared_ci_hotspot")

    def test_fires_within_bound_with_corroboration(self):
        results = self._hot().detect(None, seeded_estate(), None)
        assert len(results) == 1
        assert _contract(results[0])["evidence"]["common_ci"] == "storage-tier"
        assert _contract(results[0])["evidence"]["service_count"] == 3

    def test_does_not_fire_beyond_depth_bound(self):
        assert self._hot().detect(None, seeded_estate(hotspot_beyond_bound=True), None) == []

    def test_does_not_fire_without_event_corroboration(self):
        assert self._hot().detect(None, seeded_estate(hotspot_event=False), None) == []


# ── AC4 — concentration-shaped wording, never causal (template-level check) ─────

class TestAC4CausalGate:

    def _hotspot_finding(self):
        hot = importlib.import_module("discovery.detectors.cloud_ops_shared_ci_hotspot")
        return hot.detect(None, seeded_estate(), None)[0]

    def test_statement_is_concentration_shaped(self):
        stmt = _contract(self._hotspot_finding())["evidence"]["statement"]
        assert "concentrate on a shared dependency" in stmt
        assert fc.find_causal_language(stmt) == []

    def test_no_causal_language_anywhere_in_finding(self):
        assert fc.find_causal_language(repr(self._hotspot_finding().raw_evidence)) == []

    @pytest.mark.parametrize("bad", [
        "Outage caused by storage-tier",
        "Incidents due to the shared dependency",
        "storage-tier is responsible for these incidents",
        "the shared CI triggered by load",
    ])
    def test_causal_wording_is_rejected(self, bad):
        assert fc.find_causal_language(bad)          # detected
        with pytest.raises(ValueError):
            fc.assert_not_causal(bad)                # and rejected by the gate


# ── AC5 — Managed Cloud Operations template (config-only, editable, recorded) ───

class TestAC5Template:

    def _resolve(self, **over):
        from discovery.packs.template_registry import resolve_launch_config
        return resolve_launch_config("managed_cloud_operations", **over)

    def test_template_registered_as_config(self):
        from discovery.packs.template_registry import get_template
        from discovery.packs.pack_config import list_packs
        defn = get_template("managed_cloud_operations")
        assert defn is not None
        assert defn.pack_id == "cloud_ops" and defn.pack_id in list_packs()

    def test_untouched_launch_prepopulates_pack_focus_systems_roles(self):
        out = self._resolve()
        eff = out["effective"]
        assert eff["pack_id"] == "cloud_ops"
        assert eff["focus_id"] == "core_operations"
        assert "servicenow" in eff["selected_system_ids"]
        assert eff["roles"]["servicenow"] == "system_of_record"
        assert any("event_source" in s for s in eff["selected_system_ids"])
        assert out["provenance"]["applied"] is True
        assert out["provenance"]["untouched"] is True

    def test_defaults_are_editable_and_recorded(self):
        out = self._resolve(focus_id="approvals_compliance")
        assert out["effective"]["focus_id"] == "approvals_compliance"   # edit wins
        assert "focus_id" in out["provenance"]["edited_fields"]          # recorded
        assert out["provenance"]["untouched"] is False

    def test_zero_template_model_code_change_resolves_through_generic_path(self):
        # The SAME generic resolver serves lending and cloud-ops — no special-casing.
        from discovery.packs.template_registry import resolve_launch_config
        lending = resolve_launch_config("commercial_lending")
        cloud = resolve_launch_config("managed_cloud_operations")
        assert lending["provenance"]["applied"] and cloud["provenance"]["applied"]
        assert cloud["provenance"]["template_defaults"]["pack_id"] == "cloud_ops"


# ── AC6 — B5-absent degradation is explicit, never silent ───────────────────────

class TestAC6RunbookDegradation:

    def _loops(self, *, b5):
        rrl = importlib.import_module("discovery.detectors.cloud_ops_recurring_resolution_loop")
        return {dr.raw_evidence["signature"]: dr
                for dr in rrl.detect(None, seeded_estate(b5_available=b5), None)}

    def test_b5_absent_degrades_to_repeated_manual_with_label(self):
        dr = self._loops(b5=False)["disk-loop-A"]
        composite = _contract(dr)["evidence"]["composite"]
        assert composite["kind"] == fc.LEG_REPEATED_MANUAL
        assert composite["documented"] is False
        assert composite["degraded"] is True
        assert composite["label"] == fc.RUNBOOK_MATCH_UNAVAILABLE_LABEL   # never silent
        assert dr.raw_evidence["finding_kind"] == fc.LEG_REPEATED_MANUAL

    def test_b5_present_yields_documented_composite(self):
        dr = self._loops(b5=True)["disk-loop-A"]
        composite = _contract(dr)["evidence"]["composite"]
        assert composite["kind"] == fc.LEG_DOCUMENTED_REPEATED_MANUAL
        assert composite["documented"] is True
        assert composite["runbook_id"] == "RB-1"

    def test_contract_stays_complete_either_way(self):
        for b5 in (True, False):
            for dr in self._loops(b5=b5).values():
                assert fc.is_contract_complete(_contract(dr))


# ── AC7 — pack-wide individual-reference sweep ──────────────────────────────────

class TestAC7NoIndividuals:

    def test_pack_wide_sweep_is_clean(self):
        # Inject person fields into EVERY input shape; none may surface.
        estate = seeded_estate()
        block = estate["cloud_ops"]
        block["recurrence_records"][0]["assignee"] = "Jane Doe"
        block["oscillation_records"][0]["hops"][0]["assigned_to"] = "John Roe"
        block["oscillation_records"][0]["caller"] = "Ann Poe"
        block["queues"][0]["resolved_by"] = "Sam Loe"
        block["service_incidents"][0]["opened_by"] = "Kim Moe"
        block["event_signatures"][1]["assignee"] = "Lee Noe"

        results = _run_pack(estate)
        assert len(results) >= 5
        for dr in results:
            assert fc.find_individual_references(dr.raw_evidence) == [], (
                f"{dr.detector_id} leaked an individual reference")
        blob = repr([dr.raw_evidence for dr in results])
        for name in ("Jane Doe", "John Roe", "Ann Poe", "Sam Loe", "Kim Moe", "Lee Noe"):
            assert name not in blob

    def test_sweep_detects_an_individual_when_present(self):
        # Negative control: the sweep must actually bite.
        assert fc.find_individual_references({"assignee": "Jane Doe"})
        assert fc.find_individual_references({"contact": "x@example.com"})


# ── Ticket AC2 — the suite runs in the merge-blocking CI ────────────────────────

class TestTicketAC2RunsInCI:

    def _workflow_text(self):
        import os
        here = os.path.dirname(__file__)
        path = os.path.abspath(os.path.join(here, "..", "..", "..", ".github", "workflows", "contract-tests.yml"))
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_ci_runs_contract_tests_directory(self):
        text = self._workflow_text()
        assert "pytest tests/contract/" in text
        # Triggers on backend changes for both PRs and pushes to main → blocks merge.
        assert "backend/**" in text
        assert "pull_request" in text

    def test_this_suite_lives_under_contract_dir(self):
        import os
        assert os.path.sep + os.path.join("tests", "contract") + os.path.sep in __file__
