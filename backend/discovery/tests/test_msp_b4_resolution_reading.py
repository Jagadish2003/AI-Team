"""MSP-B4 T1 — ServiceNow incident resolution-depth reading.

Proves that resolved incidents carry the new structured resolution payload
(close code, resolution categories, resolved-by GROUP, normalised timestamps,
time-to-resolve, evidence pointers, and notes-as-evidence) on the existing
incident read path, while the established incident metrics, assignment-group
rollup, and CMDB behaviour continue to work unchanged.

All tests run offline. No ServiceNow credentials required.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"


@pytest.fixture
def incident_metrics():
    from discovery.ingest.servicenow import get_incident_metrics

    return get_incident_metrics()


def _by_number(incident_metrics):
    return {i["number"]: i for i in incident_metrics["incidents"]}


# ─────────────────────────────────────────────────────────────────────────────
# Resolution fields present on resolved incidents
# ─────────────────────────────────────────────────────────────────────────────


class TestResolutionFieldsRead:
    def test_every_incident_has_a_resolution_block(self, incident_metrics):
        for incident in incident_metrics["incidents"]:
            assert "resolution" in incident, incident.get("number")
            assert isinstance(incident["resolution"], dict)

    def test_resolved_incident_has_close_code_categories_and_group(self, incident_metrics):
        res = _by_number(incident_metrics)["INC0000001"]["resolution"]
        assert res["is_resolved"] is True
        assert res["close_code"] == "Solved (Permanently)"
        assert res["resolution_category"] == "compliance"
        assert res["resolution_subcategory"] == "covenant"
        # resolved-by is a GROUP/queue, never a person.
        assert res["resolved_by_group"] == "Level 2 Support"

    def test_resolution_timestamps_normalised(self, incident_metrics):
        res = _by_number(incident_metrics)["INC0000001"]["resolution"]
        assert res["created_at"] == "2026-06-01 09:00:00"
        assert res["resolved_at"] == "2026-06-01 13:00:00"
        assert res["closed_at"] == "2026-06-02 13:00:00"
        # created -> resolved = 4 hours = 14400s (no first-assigned field configured)
        assert res["time_to_resolve_seconds"] == 4 * 3600

    def test_time_to_resolve_supports_median_ttr_downstream(self, incident_metrics):
        """Every resolved incident yields a usable TTR so B4 recurrence can take a median."""
        resolved = [
            i["resolution"]
            for i in incident_metrics["incidents"]
            if i["resolution"]["is_resolved"]
        ]
        assert len(resolved) >= 2
        ttrs = [r["time_to_resolve_seconds"] for r in resolved]
        assert all(isinstance(t, int) and t >= 0 for t in ttrs)

    def test_unresolved_incident_has_empty_resolution(self, incident_metrics):
        res = _by_number(incident_metrics)["INC0000003"]["resolution"]
        assert res["is_resolved"] is False
        assert res["close_code"] is None
        assert res["resolved_at"] is None
        assert res["closed_at"] is None
        assert res["time_to_resolve_seconds"] is None
        assert res["has_resolution_notes"] is False
        assert res["runbook_references"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Evidence references
# ─────────────────────────────────────────────────────────────────────────────


class TestEvidenceReferences:
    def test_resolution_carries_observed_evidence_pointer_to_incident(self, incident_metrics):
        from app.provenance import EvidencePointer

        res = _by_number(incident_metrics)["INC0000001"]["resolution"]
        pointer = res["evidence"]
        assert pointer["source_system"] == "servicenow"
        assert pointer["source_artifact"] == "incident-sys-0001"
        assert pointer["source_artifact_type"] == "record_id"
        assert pointer["origin"] == "observed"
        # A valid, storable observed pointer (no extraction_job_id required).
        assert EvidencePointer.from_dict(pointer).is_valid()

    def test_evidence_pointer_uses_stable_sys_id_not_display_name(self, incident_metrics):
        for incident in incident_metrics["incidents"]:
            pointer = incident["resolution"]["evidence"]
            assert pointer["source_artifact"].startswith("incident-sys-")


# ─────────────────────────────────────────────────────────────────────────────
# Notes are stored as EVIDENCE, never as free-text in the detector payload
# ─────────────────────────────────────────────────────────────────────────────


class TestNotesStoredAsEvidence:
    def test_notes_present_flagged_and_pointered(self, incident_metrics):
        res = _by_number(incident_metrics)["INC0000001"]["resolution"]
        assert res["has_resolution_notes"] is True
        assert res["notes_evidence"] is not None
        assert res["notes_evidence"]["source_system"] == "servicenow"
        assert res["notes_evidence"]["source_artifact"] == "incident-sys-0001"

    def test_deterministic_runbook_identifier_extracted(self, incident_metrics):
        by_number = _by_number(incident_metrics)
        assert by_number["INC0000001"]["resolution"]["runbook_references"] == ["KB0010234"]
        assert by_number["INC0000002"]["resolution"]["runbook_references"] == [
            "RUNBOOK-LOAN-CLOSE"
        ]
        assert by_number["INC0000005"]["resolution"]["runbook_references"] == ["KB0010500"]

    def test_incident_without_runbook_ref_has_none(self, incident_metrics):
        res = _by_number(incident_metrics)["INC0000004"]["resolution"]
        assert res["has_resolution_notes"] is True
        assert res["runbook_references"] == []

    def test_raw_resolution_note_text_never_exposed_in_payload(self, incident_metrics):
        """The free-text note must not leak into the detector-facing payload.

        Semantic matching of notes is MSP-B5; B4 exposes only deterministic
        references + an evidence pointer. The distinctive prose from the seeded
        notes must not appear anywhere in the resolution blocks.
        """
        import json

        secrets_prose = [
            "Cache flushed and validated",
            "permanent fix tracked separately",
            "reprocessed the queued approvals",
            "confirmed with origination",
        ]
        blocks = json.dumps(
            [i["resolution"] for i in incident_metrics["incidents"]]
        )
        for phrase in secrets_prose:
            assert phrase not in blocks, f"raw note text leaked: {phrase!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Existing behaviour unchanged
# ─────────────────────────────────────────────────────────────────────────────


class TestExistingBehaviourUnchanged:
    def test_incident_metrics_totals_unchanged(self, incident_metrics):
        assert incident_metrics["total_incidents_90d"] == 500
        assert incident_metrics["avg_resolution_hours"] == 18.4
        assert isinstance(incident_metrics["category_breakdown"], list)

    def test_assignment_group_rollup_unchanged(self):
        from discovery.ingest.servicenow import ingest

        data = ingest()
        groups = {g["group_name"] for g in data["assignment_groups"]}
        assert {"Level 1 Support", "Level 2 Support", "Network Ops"} <= groups

    def test_cross_system_echo_unchanged(self):
        from discovery.ingest.servicenow import get_cross_system_references

        csr = get_cross_system_references()
        assert csr["sn_echo_score"] == 0.16
        assert csr["sn_match_count"] == 80

    def test_cmdb_ingest_still_present(self):
        from discovery.ingest.servicenow import ingest

        data = ingest()
        assert "cmdb" in data
        assert "configuration_items" in data["cmdb"]


# ─────────────────────────────────────────────────────────────────────────────
# Live path: resolution read from the SAME incident query (no new scan)
# ─────────────────────────────────────────────────────────────────────────────


class TestLiveResolutionRead:
    def test_live_query_requests_resolution_fields_on_incident_table(self, monkeypatch):
        from discovery.ingest import servicenow as sn_mod

        seen = {}

        class Client:
            instance_url = "https://acme.service-now.com"

            def aggregate_count(self, table, query):
                return 1

            def table_query(self, table, params):
                seen["table"] = table
                seen["fields"] = params["sysparm_fields"]
                return [
                    {
                        "sys_id": {"value": "sys-1", "display_value": "sys-1"},
                        "number": {"value": "INC001", "display_value": "INC001"},
                        "category": {"value": "software", "display_value": "Software"},
                        "subcategory": {"value": "email", "display_value": "Email"},
                        "state": {"value": "6", "display_value": "Resolved"},
                        "assignment_group": {"value": "g1", "display_value": "Loan Ops"},
                        "opened_at": "2026-06-01 10:00:00",
                        "resolved_at": "2026-06-01 12:00:00",
                        "closed_at": "2026-06-02 12:00:00",
                        "close_code": {"value": "Solved (Permanently)", "display_value": "Solved (Permanently)"},
                        "close_notes": "Fixed per runbook KB0009999.",
                        "sys_created_on": "2026-06-01 10:00:00",
                        "sys_updated_on": "2026-06-01 12:00:00",
                    }
                ]

        monkeypatch.setattr(sn_mod, "is_live", lambda: True)
        result = sn_mod.get_incident_metrics(Client())

        # Read on the SAME incident query — not a separate table/scan.
        assert seen["table"] == "incident"
        for field in ("close_code", "close_notes", "opened_at", "closed_at", "subcategory"):
            assert field in seen["fields"]

        res = result["incidents"][0]["resolution"]
        assert res["is_resolved"] is True
        assert res["close_code"] == "Solved (Permanently)"
        assert res["resolution_subcategory"] == "Email"
        assert res["resolved_by_group"] == "Loan Ops"
        assert res["time_to_resolve_seconds"] == 2 * 3600
        assert res["runbook_references"] == ["KB0009999"]
        assert res["evidence"]["source_artifact"] == "sys-1"

    def test_first_assigned_field_read_only_when_configured(self, monkeypatch):
        from discovery.ingest import servicenow as sn_mod

        monkeypatch.setenv(sn_mod.FIRST_ASSIGNED_FIELD_ENV, "u_first_assigned")

        class Client:
            instance_url = "https://acme.service-now.com"

            def aggregate_count(self, table, query):
                return 1

            def table_query(self, table, params):
                assert "u_first_assigned" in params["sysparm_fields"]
                return [
                    {
                        "sys_id": {"value": "sys-1", "display_value": "sys-1"},
                        "number": {"value": "INC001", "display_value": "INC001"},
                        "category": {"value": "software", "display_value": "Software"},
                        "state": {"value": "6", "display_value": "Resolved"},
                        "opened_at": "2026-06-01 08:00:00",
                        "u_first_assigned": "2026-06-01 10:00:00",
                        "resolved_at": "2026-06-01 12:00:00",
                        "sys_created_on": "2026-06-01 08:00:00",
                        "sys_updated_on": "2026-06-01 12:00:00",
                    }
                ]

        monkeypatch.setattr(sn_mod, "is_live", lambda: True)
        result = sn_mod.get_incident_metrics(Client())
        res = result["incidents"][0]["resolution"]
        assert res["first_assigned_at"] == "2026-06-01 10:00:00"
        # TTR measured from first-assignment (10:00) -> resolved (12:00) = 2h.
        assert res["time_to_resolve_seconds"] == 2 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — reads are read-only and org-scoped
# ─────────────────────────────────────────────────────────────────────────────


class TestReadOnlyAndOrgScoped:
    def test_client_exposes_no_write_methods(self):
        from discovery.ingest.servicenow import ServiceNowClient

        for verb in ("post", "put", "patch", "delete", "insert", "update", "write"):
            assert not hasattr(ServiceNowClient, verb), verb

    def test_two_org_reads_are_isolated(self):
        """Resolution reading under two orgs stays isolated — no shared mutation."""
        from discovery.ingest import get_ingest_org, set_ingest_org
        from discovery.ingest.servicenow import get_incident_metrics

        try:
            set_ingest_org("org-acme")
            assert get_ingest_org() == "org-acme"
            acme = get_incident_metrics()

            set_ingest_org("org-globex")
            assert get_ingest_org() == "org-globex"
            globex = get_incident_metrics()
        finally:
            set_ingest_org(None)

        # Each read builds its own payload; mutating one must not affect the other.
        acme["incidents"][0]["resolution"]["close_code"] = "MUTATED"
        assert globex["incidents"][0]["resolution"]["close_code"] != "MUTATED"
