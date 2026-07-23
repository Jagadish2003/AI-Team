"""
Contract tests for MSP-B12 T3 — the mandatory Security Operations aggregation
boundary + org-scoped, access-controlled, audited evidence-pointer resolution.

Covers the T3-owned acceptance criterion (AC2):
  * A pack-wide sweep recursively inspects EVERY output surface (titles,
    explanations, raw evidence, nested fields, reports, exports, telemetry
    payloads) for prohibited host×vulnerability pairs and person-level data.
  * Safe class / service / CI-class (and the other allowed) aggregates remain
    available and useful — positive tests.
  * Individual source records resolve ONLY via org-scoped, access-controlled
    evidence pointers; every resolution produces an audit event; unauthorized and
    cross-org callers are denied.
"""
from __future__ import annotations

import importlib

import pytest


def _mod(dotted):
    try:
        return importlib.import_module(f"backend.{dotted}")
    except ModuleNotFoundError:
        return importlib.import_module(dotted)


af = _mod("discovery.packs.security_ops_aggregation_floor")
rv = _mod("discovery.packs.security_ops_evidence_resolver")
estate_mod = _mod("tests.contract.test_security_ops_detectors") if False else None

# Reuse the T2 seeded estate + detector set.
import importlib as _il
try:
    _est = _il.import_module("test_security_ops_detectors")
except ModuleNotFoundError:  # when run as a package path
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    _est = _il.import_module("test_security_ops_detectors")

DETECTORS = [
    "security_ops_remediation_recurrence",
    "security_ops_security_it_pingpong",
    "security_ops_sla_deferral_ageing",
    "security_ops_shared_infra_concentration",
    "security_ops_sir_triage_toil",
]


def _all_findings():
    estate = _est._estate()
    results = []
    for name in DETECTORS:
        results += _mod(f"discovery.detectors.{name}").detect(None, estate, None)
    return results


# ── The sweep passes real findings; safe aggregates survive (positive) ───────────

class TestSafeAggregatesSurvive:

    def test_every_real_finding_passes_the_sweep(self):
        results = _all_findings()
        assert results
        assert af.enforce_pack_output(results) == len(results)
        for r in results:
            assert af.find_output_violations(r.raw_evidence) == []

    def test_all_allowed_aggregation_dimensions_pass(self):
        """The nine allowed group-level dimensions are safe and useful."""
        safe = {
            "vulnerability_class": "missing patch",
            "service": "payments-api",
            "ci_class": "cmdb_ci_storage_device",
            "assignment_group": "Vulnerability Management",
            "remediation_path": ["detected", "assigned", "patched", "closed"],
            "severity_band": "critical",
            "queue": "Patch Ops",
            "deferral_class": "Risk Accepted",
            "incident_category": "Phishing",
            "counts": {"recurrence_count": 12, "service_count": 3},
        }
        assert af.find_output_violations(safe) == []
        af.assert_output_safe(safe, where="safe aggregate")

    def test_real_findings_actually_carry_useful_aggregates(self):
        """Positive: the safe dimensions are present and readable in outputs."""
        by_id = {r.detector_id: r.raw_evidence["finding_contract"]["evidence"] for r in _all_findings()}
        assert by_id["SECOPS_REMEDIATION_RECURRENCE"]["vulnerability_class"] == "missing patch"
        assert by_id["SECOPS_REMEDIATION_RECURRENCE"]["ci_class"] == "cmdb_ci_server"
        assert by_id["SECOPS_SLA_DEFERRAL_AGEING"]["queue"] == "Patch Ops"
        assert by_id["SECOPS_SLA_DEFERRAL_AGEING"]["severity_band"] == "medium"
        assert by_id["SECOPS_SHARED_INFRA_CONCENTRATION"]["common_ci_class"] == "cmdb_ci_storage_device"
        assert by_id["SECOPS_SIR_TRIAGE_TOIL"]["category"] == "Phishing"


# ── The sweep catches prohibited data on EVERY surface, however deep ─────────────

class TestProhibitedDataCaught:

    def test_host_vuln_pair_in_free_text(self):
        v = af.find_output_violations({"explanation": "10.0.0.5 is exposed to CVE-2021-44228"})
        assert any(x["kind"] == "host_vuln_pair_in_text" for x in v)

    def test_cve_in_free_text(self):
        v = af.find_output_violations({"title": "Widespread CVE-2019-0708 exposure"})
        assert any(x["kind"] == "cve_in_text" for x in v)

    def test_ip_in_free_text(self):
        v = af.find_output_violations({"note": "seen on 192.168.10.42"})
        assert any(x["kind"] == "host_in_text" for x in v)

    def test_mac_in_free_text(self):
        v = af.find_output_violations({"note": "device 00:1A:2B:3C:4D:5E"})
        assert any(x["kind"] == "host_in_text" for x in v)

    def test_person_field_and_email(self):
        v = af.find_output_violations({"assigned_to": "Jane Doe", "contact": "jane@corp.com"})
        assert any(x["kind"] == "individual" for x in v)

    def test_host_and_vuln_instance_fields(self):
        v = af.find_output_violations({"hostname": "web-01", "qid": "150004"})
        kinds = {x["kind"] for x in v}
        assert "host_field" in kinds and "vuln_instance_field" in kinds

    def test_violation_hidden_deep_in_nested_evidence_is_caught(self):
        """Filtering the title alone is insufficient — a hidden field is caught."""
        finding = {
            "title": "Remediation recurrence on a CI class",   # clean title
            "evidence": {"vulnerability_class": "missing patch",
                         "detail": {"rows": [{"ok": 1}, {"host_cve": "web-9 / CVE-2020-1472"}]}},
        }
        v = af.find_output_violations(finding)
        assert any("evidence.detail.rows[1]" in x["path"] for x in v)

    def test_enforce_pack_output_raises_on_a_bad_finding(self):
        class _R:
            detector_id = "SECOPS_X"
            raw_evidence = {"finding_contract": {"evidence": {"hostname": "web-01", "cve": "CVE-2021-1"}}}
        with pytest.raises(af.SecOpsAggregationFloorViolation):
            af.enforce_pack_output([_R()])

    def test_assert_output_safe_raises_with_detail(self):
        with pytest.raises(af.SecOpsAggregationFloorViolation):
            af.assert_output_safe({"row": {"ip_address": "10.1.1.1"}}, where="export cell")


# ── Applies to report / export / telemetry surfaces (output-agnostic) ────────────

class TestEveryOutputSurface:

    def test_report_cell_swept(self):
        report = {"sections": [{"title": "Top classes", "cells": [{"label": "ok"},
                   {"label": "host srv-1 / CVE-2017-0144"}]}]}
        assert af.find_output_violations(report)

    def test_export_row_swept(self):
        export = {"rows": [{"vulnerability_class": "missing patch", "count": 9},
                           {"vulnerability_class": "x", "host": "db-7"}]}
        assert af.find_output_violations(export)

    def test_telemetry_payload_swept(self):
        telemetry = {"event": "secops.finding", "attrs": {"assigned_to": "Sam"}}
        assert af.find_output_violations(telemetry)

    def test_safe_report_passes(self):
        report = {"sections": [{"title": "Workload by CI class",
                   "cells": [{"ci_class": "cmdb_ci_server", "count": 42},
                             {"ci_class": "cmdb_ci_storage_device", "count": 12}]}]}
        assert af.find_output_violations(report) == []

    def test_materialization_boundary_fails_closed(self):
        materialize = _mod("app.materialize_t2")
        with pytest.raises(materialize.SecOpsAggregationFloorViolation):
            materialize._assert_secops_materialized(
                {"report": {"cells": [{"ip_address": "10.0.0.5"}]}},
                where="post-detector report",
                enabled=True,
            )

    def test_materialization_slice_is_pack_isolated(self):
        materialize = _mod("app.materialize_t2")
        opps = [
            {"id": "s", "packId": "security_ops", "evidenceIds": ["es"]},
            {"id": "c", "packId": "cloud_ops", "evidenceIds": ["ec"]},
        ]
        evidence = [{"id": "es"}, {"id": "ec"}]
        secops_opps, secops_evidence = materialize._secops_seed_slice(opps, evidence)
        assert [item["id"] for item in secops_opps] == ["s"]
        assert [item["id"] for item in secops_evidence] == ["es"]


# ── Evidence-pointer resolution: org-scoped, access-controlled, audited ──────────

def _fixed_clock():
    from datetime import datetime, timezone
    return lambda: datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class TestEvidenceResolution:

    def _setup(self):
        estate = _est._estate()
        store = rv.InMemoryEvidenceRecordStore()
        indexed = rv.index_signal_records(store, "org-a", estate)
        results = []
        for name in DETECTORS:
            results += _mod(f"discovery.detectors.{name}").detect(None, estate, None)
        pointer = results[0].raw_evidence["finding_contract"]["source_trace"]["artifacts"][0]
        return store, pointer, indexed

    def test_index_populates_store(self):
        _, _, indexed = self._setup()
        assert indexed > 0

    def test_authorized_resolution_returns_record_and_provenance(self):
        store, pointer, _ = self._setup()
        events = []
        out = rv.resolve_evidence_pointer(
            pointer, requesting_org="org-a", user_id="u1", role="analyst",
            store=store, emit=lambda e, p: events.append((e, p)), now=_fixed_clock(),
        )
        assert out["resolved"] is True
        assert out["record"]["sys_id"]
        assert out["provenance"]["source_system"] == "servicenow"

    def test_resolution_emits_audit_with_required_fields(self):
        store, pointer, _ = self._setup()
        events = []
        rv.resolve_evidence_pointer(
            pointer, requesting_org="org-a", user_id="analyst-7", role="analyst",
            store=store, emit=lambda e, p: events.append((e, p)), now=_fixed_clock(),
        )
        assert len(events) == 1
        name, payload = events[0]
        assert name == "secops.evidence_pointer_resolved"
        assert payload["outcome"] == "resolved"
        # requesting org, user, source system, pointer id, access time (AC2).
        for field in ("org_id", "user_id", "source_system", "pointer_id", "access_time"):
            assert payload[field], f"audit missing {field}"
        assert payload["org_id"] == "org-a" and payload["user_id"] == "analyst-7"
        assert payload["access_time"] == "2026-07-22T12:00:00+00:00"

    def test_cross_org_user_denied_and_audited(self):
        store, pointer, _ = self._setup()
        events = []
        with pytest.raises(rv.EvidenceAccessDenied) as ei:
            rv.resolve_evidence_pointer(
                pointer, requesting_org="org-b", user_id="intruder", role="analyst",
                store=store, emit=lambda e, p: events.append((e, p)),
            )
        assert ei.value.reason == rv.REASON_NOT_FOUND_OR_CROSS_ORG
        assert events[-1][1]["outcome"] == "denied"  # denial is audited too

    def test_viewer_denied(self):
        store, pointer, _ = self._setup()
        with pytest.raises(rv.EvidenceAccessDenied) as ei:
            rv.resolve_evidence_pointer(
                pointer, requesting_org="org-a", user_id="v", role="viewer",
                store=store, emit=lambda e, p: None,
            )
        assert ei.value.reason == rv.REASON_INSUFFICIENT_ROLE

    def test_unauthenticated_denied(self):
        store, pointer, _ = self._setup()
        with pytest.raises(rv.EvidenceAccessDenied):
            rv.resolve_evidence_pointer(
                pointer, requesting_org="org-a", user_id=None, role=None,
                store=store, emit=lambda e, p: None,
            )

    def test_invalid_pointer_denied_and_audited(self):
        store, _, _ = self._setup()
        events = []
        with pytest.raises(rv.EvidenceAccessDenied) as ei:
            rv.resolve_evidence_pointer(
                {"type": "x"}, requesting_org="org-a", user_id="u", role="analyst",
                store=store, emit=lambda e, p: events.append((e, p)),
            )
        assert ei.value.reason == rv.REASON_INVALID_POINTER
        assert events[-1][1]["outcome"] == "denied"

    def test_store_is_org_partitioned(self):
        store = rv.InMemoryEvidenceRecordStore()
        store.put("org-a", "servicenow", "vi-1", {"sys_id": "vi-1", "state": "Open"})
        assert store.get("org-a", "servicenow", "vi-1") is not None
        assert store.get("org-b", "servicenow", "vi-1") is None

    def test_run_kv_store_persists_for_production_resolution(self):
        class _Db:
            values = {}

            @classmethod
            def run_kv_get(cls, key, run_id, default=None):
                return cls.values.get((key, run_id), default)

            @classmethod
            def run_kv_set(cls, key, run_id, value):
                cls.values[(key, run_id)] = value

        estate = _est._estate()
        writer = rv.RunKVEvidenceRecordStore(
            "run-1", "org-a", db_api=_Db
        )
        indexed = rv.index_signal_records(writer, "org-a", estate)
        assert writer.flush() == indexed

        reader = rv.RunKVEvidenceRecordStore("run-1", "org-a", db_api=_Db)
        record = reader.get("org-a", "servicenow", "vi-server-001")
        assert record["vulnerability_class"] == "missing patch"

        foreign = rv.RunKVEvidenceRecordStore("run-1", "org-b", db_api=_Db)
        assert foreign.get("org-b", "servicenow", "vi-server-001") is None

    def test_pointer_on_finding_is_lean_no_record_content(self):
        """The pointer names artifact + provenance only — no record content embedded."""
        _, pointer, _ = self._setup()
        ptr = pointer["evidence_pointer"]
        assert set(ptr) <= set(rv.EvidencePointer.__dataclass_fields__)
        # No sensitive/workflow record content rides on the pointer.
        for leaked in ("state", "assignment_group", "assigned_to", "severity", "work_notes"):
            assert leaked not in ptr

    def test_default_emit_uses_registered_telemetry_event(self):
        """The audit event type is registered, so the default emit path won't raise."""
        from app.telemetry import REGISTERED_EVENT_TYPES
        assert "secops.evidence_pointer_resolved" in REGISTERED_EVENT_TYPES
