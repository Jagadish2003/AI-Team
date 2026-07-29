"""
Contract tests for MSP-B12 T2 — the five Security Operations detectors.

Covers the T2-owned acceptance criteria:
  AC1 — a seeded SecOps estate produces findings from ≥4 of the 5 detectors, each
        carrying the four-part contract (evidence, confidence, corroboration,
        source trace), enforced by the inherited pack-boundary test.
  AC3 — shared-infrastructure concentration fires on a seeded multi-service→common-CI
        pattern within depth bounds, is worded as concentration (never causal), and
        does NOT fire without the dependency path.
  AC7 — no detector output names an individual — groups, queues, services, CI
        classes only (pack-wide sweep, inherited).

Plus: determinism (same input → same output) and fail-safe behaviour (missing
data → no findings, no exceptions). All findings consume ONLY the MSP-B11 signal
(``sn_data['secops']`` / ``['vulnerability_response']``) and MSP-B3
(``sn_data['cmdb']``).
"""
from __future__ import annotations

import importlib

import pytest

ORG = "org-a"


def _mod(name):
    try:
        return importlib.import_module(f"backend.discovery.detectors.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(f"discovery.detectors.{name}")


def _finding_module():
    try:
        import backend.discovery.packs.security_ops_finding as m
    except ModuleNotFoundError:
        import discovery.packs.security_ops_finding as m
    return m


RECURRENCE = "security_ops_remediation_recurrence"
PINGPONG = "security_ops_security_it_pingpong"
AGEING = "security_ops_sla_deferral_ageing"
CONCENTRATION = "security_ops_shared_infra_concentration"
TRIAGE = "security_ops_sir_triage_toil"
ALL_DETECTORS = [RECURRENCE, PINGPONG, AGEING, CONCENTRATION, TRIAGE]


def _transition(frm, to, at):
    return {"field": "assignment_group", "from_value": frm, "to_value": to, "changed_at": at}


def _vuln_item(sys_id, **kw):
    base = {
        "sys_id": sys_id, "number": sys_id.upper(), "org_id": ORG,
        "source_timestamp": "2026-07-01 00:00:00", "origin": "observed",
        "source_type": "servicenow_vulnerable_item",
        "assigned_to": "Alex Analyst",  # PERSON — a finding must never surface this
        "state_history": [], "assignment_history": [],
    }
    base.update(kw)
    return base


def _sir(sys_id, **kw):
    base = {
        "sys_id": sys_id, "number": sys_id.upper(), "org_id": ORG,
        "source_timestamp": "2026-07-01 00:00:00", "origin": "observed",
        "source_type": "servicenow_security_incident",
        "assigned_to": "Dana Cruz",  # PERSON — must never be surfaced
        "state_history": [], "assignment_history": [],
    }
    base.update(kw)
    return base


def _estate():
    """A deterministic SecOps estate exercising all five detectors (org-a)."""
    # ── Recurrence + concentration: 3 server vulnerable items, same remediation
    # signature, each on a distinct server CI that depends on one shared storage CI.
    def _server_item(n, first_found):
        return _vuln_item(
            f"vi-server-{n}",
            vulnerability_class="missing patch", severity="1 - Critical",
            cmdb_ci=f"ci-server-{n}", assignment_group="Vulnerability Management",
            first_found=first_found, opened_at=first_found,
            resolved_at="2026-06-20 00:00:00", closed_at="2026-06-20 00:00:00",
            state="Closed", close_code="Fixed by patch", resolution_status="Remediated",
            remediation_signature="1:openssl-server-patch",
            remediation_signature_components={
                "version": "1", "vulnerability_class": "missing patch",
                "ci_class": "cmdb_ci_server",
                "remediation_path": ["detected", "assigned", "patched", "closed"],
            },
        )
    server_items = [
        _server_item("001", "2026-04-01 00:00:00"),
        _server_item("002", "2026-05-01 00:00:00"),
        _server_item("003", "2026-06-01 00:00:00"),
    ]

    # ── SLA ageing: queue=Patch Ops, severity Medium. 3 resolved (baseline ~10d) +
    # 2 open aged ~30d against as_of 2026-07-01. Includes deferral/exception classes.
    def _baseline_item(n, resolved_at):
        return _vuln_item(
            f"vi-base-{n}", vulnerability_class="missing patch", severity="3 - Medium",
            cmdb_ci="ci-db-001", assignment_group="Patch Ops",
            opened_at="2026-06-01 00:00:00", resolved_at=resolved_at, closed_at=resolved_at,
            state="Closed", close_code="Fixed",
        )
    baseline_items = [
        _baseline_item("1", "2026-06-11 00:00:00"),
        _baseline_item("2", "2026-06-11 00:00:00"),
        _baseline_item("3", "2026-06-12 00:00:00"),
    ]
    open_items = [
        _vuln_item(
            "vi-open-1", vulnerability_class="missing patch", severity="3 - Medium",
            cmdb_ci="ci-db-001", assignment_group="Patch Ops",
            opened_at="2026-06-01 00:00:00", state="In Progress",
            deferral_category="Risk Accepted", justification_class="Business Justification",
        ),
        _vuln_item(
            "vi-open-2", vulnerability_class="missing patch", severity="3 - Medium",
            cmdb_ci="ci-db-001", assignment_group="Patch Ops",
            opened_at="2026-06-01 00:00:00", state="Deferred",
            exception_category="False Positive", justification_class="Analyst Reviewed",
        ),
    ]

    # ── Ping-pong: one remediation task oscillating Security → IT → Security.
    pingpong_task = _vuln_item(
        "rt-pingpong-1", state="In Progress", assignment_group="Vulnerability Management",
        source_type="servicenow_remediation_task",
        assignment_history=[
            _transition("Vulnerability Management", "Server Team", "2026-06-10 00:00:00"),
            _transition("Server Team", "Vulnerability Management", "2026-06-15 00:00:00"),
        ],
    )

    # ── SIR triage toil: 5 phishing incidents closed the same way in ~20 min.
    triage_incidents = [
        _sir(
            f"sir-{i}", category="Phishing", subcategory="Credential harvesting",
            severity="3 - Low", assignment_group="SecOps Triage",
            opened_at="2026-06-28 14:00:00", resolved_at="2026-06-28 14:20:00",
            closed_at="2026-06-28 14:20:00", state="Closed",
            close_code="Resolved by mitigation", resolution_code="True positive",
        )
        for i in range(5)
    ]

    return {
        "secops": {"org_id": ORG, "run_id": "r1", "security_incidents": triage_incidents, "streams": {}},
        "vulnerability_response": {
            "org_id": ORG, "run_id": "r1",
            "vulnerable_items": server_items + baseline_items + open_items,
            "vulnerability_groups": [],
            "remediation_tasks": [pingpong_task],
            "workload_summary": {}, "streams": {},
        },
        "cmdb": {
            "org_id": ORG, "class_scope": ["cmdb_ci_server", "cmdb_ci_storage_device"],
            "configuration_items": [
                {"sys_id": "ci-server-001", "ci_class": "cmdb_ci_server", "name": "app-01"},
                {"sys_id": "ci-server-002", "ci_class": "cmdb_ci_server", "name": "app-02"},
                {"sys_id": "ci-server-003", "ci_class": "cmdb_ci_server", "name": "app-03"},
                {"sys_id": "ci-storage-001", "ci_class": "cmdb_ci_storage_device", "name": "san-01"},
            ],
            "relationships": [
                {"relationship_type": "depends_on", "source_ci_id": "ci-server-001", "target_ci_id": "ci-storage-001"},
                {"relationship_type": "depends_on", "source_ci_id": "ci-server-002", "target_ci_id": "ci-storage-001"},
                {"relationship_type": "depends_on", "source_ci_id": "ci-server-003", "target_ci_id": "ci-storage-001"},
            ],
            "relationship_deletions": [], "streams": {},
        },
    }


def _detect(name, sn_data):
    return _mod(name).detect(None, sn_data, None)


# ── AC1 — ≥4 of 5 detectors fire, each carrying the four-part contract ───────────

class TestAC1FourPartContract:

    def test_at_least_four_of_five_detectors_fire(self):
        estate = _estate()
        fired = {name: bool(_detect(name, estate)) for name in ALL_DETECTORS}
        assert sum(fired.values()) >= 4, f"only these fired: {fired}"

    def test_all_five_detectors_fire_on_this_estate(self):
        estate = _estate()
        for name in ALL_DETECTORS:
            assert _detect(name, estate), f"{name} did not fire on the seeded estate"

    def test_every_finding_carries_all_four_parts(self):
        estate = _estate()
        f = _finding_module()
        for name in ALL_DETECTORS:
            for result in _detect(name, estate):
                contract = result.raw_evidence["finding_contract"]
                assert f.is_contract_complete(contract), f"{name}: {f.missing_contract_parts(contract)}"
                # source_trace traces back through valid evidence pointers.
                assert f.find_invalid_evidence_pointers(contract["source_trace"]) == []

    def test_pack_boundary_enforcement_passes_all_findings(self):
        """The inherited pack-boundary test validates every emitted finding."""
        estate = _estate()
        f = _finding_module()
        results = [r for name in ALL_DETECTORS for r in _detect(name, estate)]
        assert f.enforce_pack_findings(results) == len(results)
        assert results

    def test_findings_are_observed_provenance(self):
        estate = _estate()
        for name in ALL_DETECTORS:
            for result in _detect(name, estate):
                assert result.provenance_type == "observed"
                assert result.raw_evidence["corroboration_sources"]


# ── AC3 — shared-infrastructure concentration ────────────────────────────────────

class TestAC3Concentration:

    def test_fires_on_multi_service_common_ci(self):
        results = _detect(CONCENTRATION, _estate())
        assert len(results) == 1
        ev = results[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["service_count"] == 3
        assert ev["common_ci_class"] == "cmdb_ci_storage_device"

    def test_wording_is_concentration_not_causal(self):
        f = _finding_module()
        results = _detect(CONCENTRATION, _estate())
        statement = results[0].raw_evidence["statement"]
        assert "concentrate" in statement.lower()
        # The inherited causal gate must find no causal language.
        assert f.find_causal_language(statement) == []
        f.assert_not_causal(statement)

    def test_is_depth_bounded(self):
        ev = _detect(CONCENTRATION, _estate())[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["depth_bounded"] is True
        assert ev["max_hop_observed"] <= ev["max_hops"]

    def test_does_not_fire_without_dependency_path(self):
        estate = _estate()
        estate["cmdb"]["relationships"] = []  # remove the dependency edges
        assert _detect(CONCENTRATION, estate) == []

    def test_does_not_fire_without_enough_services(self):
        estate = _estate()
        # Only one server depends on the shared CI → below min_services.
        estate["cmdb"]["relationships"] = [
            {"relationship_type": "depends_on", "source_ci_id": "ci-server-001", "target_ci_id": "ci-storage-001"},
        ]
        assert _detect(CONCENTRATION, estate) == []


# ── AC7 — no individual named anywhere in detector output ────────────────────────

class TestAC7NoIndividuals:

    def test_no_finding_references_an_individual(self):
        estate = _estate()
        f = _finding_module()
        for name in ALL_DETECTORS:
            for result in _detect(name, estate):
                # Sweep the ENTIRE result payload, not just the contract.
                assert f.find_aggregation_floor_violations(result.raw_evidence) == [], name

    def test_assigned_to_is_never_propagated(self):
        """The raw B11 records carry assigned_to; findings must not echo it."""
        estate = _estate()
        for name in ALL_DETECTORS:
            for result in _detect(name, estate):
                blob = repr(result.raw_evidence)
                assert "Alex Analyst" not in blob and "Dana Cruz" not in blob, name

    def test_no_host_or_vuln_instance_fields(self):
        estate = _estate()
        f = _finding_module()
        for name in ALL_DETECTORS:
            for result in _detect(name, estate):
                contract = result.raw_evidence["finding_contract"]
                assert f.find_host_or_asset_references(contract["evidence"]) == []
                assert f.find_vulnerability_instance_references(contract["evidence"]) == []


# ── Per-detector shape assertions ────────────────────────────────────────────────

class TestDetectorShapes:

    def test_remediation_recurrence_reports_count_cycles_ttr(self):
        ev = _detect(RECURRENCE, _estate())[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["recurrence_count"] == 3
        assert ev["observed_cycles"] == 3            # 3 distinct scan-cycle dates
        assert ev["median_time_in_state_seconds"] > 0
        assert ev["vulnerability_class"] == "missing patch"
        assert ev["ci_class"] == "cmdb_ci_server"

    def test_pingpong_counts_hops_groups_only(self):
        ev = _detect(PINGPONG, _estate())[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["hop_count"] == 2
        assert ev["groups_involved"] == ["Vulnerability Management", "Server Team"]
        assert ev["security_it_boundary"] is True

    def test_sla_ageing_reports_queue_severity_and_justification_volumes(self):
        ev = _detect(AGEING, _estate())[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["queue"] == "Patch Ops"
        assert ev["severity_band"] == "medium"
        assert ev["departure_pct"] >= 0.25
        assert ev["deferral_volumes_by_justification"]     # grouped by justification class
        assert ev["exception_volumes_by_justification"]

    def test_sir_triage_toil_high_volume_same_classification_short(self):
        ev = _detect(TRIAGE, _estate())[0].raw_evidence["finding_contract"]["evidence"]
        assert ev["incident_volume"] == 5
        assert ev["distinct_classifications"] == 1
        assert ev["median_close_minutes"] <= 30
        assert ev["category"] == "Phishing"


class TestRunbookMatching:

    @pytest.mark.parametrize("detector", [RECURRENCE, TRIAGE])
    def test_b5_absence_is_explicitly_labelled(self, detector):
        result = _detect(detector, _estate())[0]
        leg = result.raw_evidence["runbook_leg"]
        assert leg["degraded"] is True
        assert leg["label"] == "runbook match unavailable"
        assert result.raw_evidence["runbook_match_available"] is False

    def test_remediation_recurrence_carries_b5_match(self):
        estate = _estate()
        estate["vulnerability_response"]["runbook_matching"] = {
            "available": True,
            "matches": {
                "1:openssl-server-patch": {
                    "runbook_id": "RB-PATCH-1",
                    "title": "Standard server patch playbook",
                }
            },
        }
        result = _detect(RECURRENCE, estate)[0]
        leg = result.raw_evidence["runbook_leg"]
        assert leg["documented"] is True
        assert leg["runbook_id"] == "RB-PATCH-1"
        assert result.raw_evidence["runbook_match_available"] is True

    def test_sir_triage_carries_b5_match(self):
        estate = _estate()
        estate["secops"]["runbook_matching"] = {
            "available": True,
            "matches": {
                "Phishing|Credential harvesting|Resolved by mitigation": {
                    "runbook_id": "RB-PHISH-1",
                    "title": "Phishing triage playbook",
                }
            },
        }
        result = _detect(TRIAGE, estate)[0]
        assert result.raw_evidence["runbook_leg"]["runbook_id"] == "RB-PHISH-1"
        assert result.raw_evidence["runbook_match_available"] is True


# ── Determinism + fail-safe ──────────────────────────────────────────────────────

class TestDeterminismAndFailSafe:

    def test_deterministic_output(self):
        for name in ALL_DETECTORS:
            a = [r.raw_evidence["finding_ref"] for r in _detect(name, _estate())]
            b = [r.raw_evidence["finding_ref"] for r in _detect(name, _estate())]
            assert a == b, name

    @pytest.mark.parametrize("name", ALL_DETECTORS)
    def test_empty_signal_no_findings_no_error(self, name):
        assert _detect(name, {}) == []
        assert _detect(name, None) == []
        assert _detect(name, {"secops": {}, "vulnerability_response": {}, "cmdb": {}}) == []

    @pytest.mark.parametrize("name", ALL_DETECTORS)
    def test_evaluate_matches_detect(self, name):
        estate = _estate()
        evaluation = _mod(name).evaluate(None, estate, None)
        assert evaluation.fired == bool(_detect(name, estate))

    def test_other_org_records_excluded(self):
        estate = _estate()
        for item in estate["vulnerability_response"]["vulnerable_items"]:
            item["org_id"] = "org-b"
        for inc in estate["secops"]["security_incidents"]:
            inc["org_id"] = "org-b"
        # The block org_id is still org-a, so org-b records are out of scope.
        assert _detect(RECURRENCE, estate) == []
        assert _detect(TRIAGE, estate) == []
