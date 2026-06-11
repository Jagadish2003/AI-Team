"""Contract tests for ENT-1 — nCino Entity Extraction Hardening (overlays).

Covers all 8 acceptance criteria plus the base dataclasses and registry:

  AC1  — overlay PersonFieldRule used when registered; fields not in the overlay
         fall back to default extraction (additive union).
  AC2  — no overlay registered → extraction identical to T3-S12-A (no regression).
  AC3  — Salesforce OwnerId via overlay PersonFieldRule resolution_source='id'
         → Person with resolution_confidence=1.0.
  AC4  — Person from Salesforce OwnerId then seen in ServiceNow by name retains
         confidence=1.0 (anchored to ID) and lists both systems in metadata.sources.
  AC5  — display_name matching service_account_patterns is filtered (not stored);
         filtered count logged in entity.extraction_completed telemetry.
  AC6  — ncino_overlay common patterns extract LLC_BI__Loan__c OwnerId,
         LLC_BI__Loan_Officer__c, and LLC_BI__Covenant__c entities.
  AC7  — overlay registered at startup is active by the first run, no restart.
  AC8  — docs/entity_overlay_authoring.md exists with Session 1 template + guide.

All DB-backed tests use the temp SQLite DB seeded by conftest.py (entities table
from migration 0003). The registry is snapshotted and restored around every test
so overlay registrations never leak across tests or into other test modules.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> str:
    return os.environ["DB_PATH"]


@pytest.fixture(autouse=True)
def _isolate_overlay_registry():
    """Snapshot and restore OVERLAY_REGISTRY around each test (no leakage)."""
    from app.entity_overlays import overlay_registry as reg

    snapshot = dict(reg.OVERLAY_REGISTRY)
    try:
        yield
    finally:
        reg.OVERLAY_REGISTRY.clear()
        reg.OVERLAY_REGISTRY.update(snapshot)


def _extract(
    *,
    org_id: str,
    run_id: str,
    pack_id: str = "ncino",
    detector_results: List[Any] | None = None,
    ingestor_data: Dict[str, Any] | None = None,
):
    from app.entity_extractor import extract_entities

    return extract_entities(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        detector_results=detector_results or [],
        ingestor_data=ingestor_data or {},
    )


def _db_by_type(org_id: str, run_id: str, entity_type: str) -> List[Dict]:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM entities
                   WHERE org_id = ? AND last_seen_run_id = ? AND entity_type = ?""",
                (org_id, run_id, entity_type),
            ).fetchall()
        ]


def _person_by_canonical(org_id: str, canonical: str) -> Dict | None:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM entities WHERE org_id=? AND entity_type='person' AND canonical_name=?",
            (org_id, canonical),
        ).fetchone()
        return dict(row) if row else None


def _meta(row: Dict) -> Dict:
    raw = row.get("metadata")
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return raw or {}


def _fake_detector(*, signal_source: str, detector_id: str) -> SimpleNamespace:
    return SimpleNamespace(signal_source=signal_source, detector_id=detector_id)


# ===========================================================================
# T1 — base dataclasses
# ===========================================================================

class TestBaseOverlayDataclasses:
    """T1: dataclasses construct, validate, and are usable without circular imports."""

    def test_person_field_rule_constructs(self):
        from app.entity_overlays.base_overlay import PersonFieldRule

        rule = PersonFieldRule(
            object_api_name="LLC_BI__Loan__c",
            field_api_name="OwnerId",
            resolution_source="id",
            label="Loan Owner",
        )
        assert rule.object_api_name == "LLC_BI__Loan__c"
        assert rule.resolution_source == "id"

    def test_person_field_rule_rejects_bad_resolution_source(self):
        from app.entity_overlays.base_overlay import PersonFieldRule

        with pytest.raises(ValueError):
            PersonFieldRule(
                object_api_name="LLC_BI__Loan__c",
                field_api_name="OwnerId",
                resolution_source="guess",  # invalid
            )

    def test_object_rule_constructs(self):
        from app.entity_overlays.base_overlay import ObjectRule

        rule = ObjectRule(
            object_api_name="LLC_BI__Covenant__c",
            entity_type="object",
            name_field="Name",
            record_type="Covenant",
        )
        assert rule.record_type == "Covenant"

    def test_team_field_rule_constructs(self):
        from app.entity_overlays.base_overlay import TeamFieldRule

        rule = TeamFieldRule(
            object_api_name="LLC_BI__Loan__c",
            field_api_name="LLC_BI__Credit_Team__c",
            label="Credit Team",
        )
        assert rule.label == "Credit Team"

    def test_overlay_constructs_with_all_fields(self):
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            ObjectRule,
            PersonFieldRule,
        )

        overlay = EntityExtractionOverlay(
            org_id="acme",
            connector_id="salesforce",
            version="1.2.3",
            person_fields=[PersonFieldRule("LLC_BI__Loan__c", "OwnerId")],
            object_rules=[ObjectRule("LLC_BI__Loan__c", "object", "Name", "Loan")],
            stage_map={"Intake": "application"},
            service_account_patterns=[r"^svc"],
        )
        assert overlay.org_id == "acme"
        assert overlay.version == "1.2.3"
        assert overlay.stage_map["Intake"] == "application"
        assert overlay.referenced_object_names() == {"LLC_BI__Loan__c"}

    def test_referenced_object_names_includes_all_rule_types(self):
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            ObjectRule,
            PersonFieldRule,
            TeamFieldRule,
        )

        overlay = EntityExtractionOverlay(
            org_id="all-names",
            connector_id="salesforce",
            person_fields=[PersonFieldRule("LLC_BI__Loan__c", "OwnerId")],
            team_fields=[TeamFieldRule("LLC_BI__Team__c", "Team__c")],
            object_rules=[ObjectRule("LLC_BI__Covenant__c", "object", "Name", "Covenant")],
        )

        assert overlay.referenced_object_names() == {
            "LLC_BI__Loan__c",
            "LLC_BI__Team__c",
            "LLC_BI__Covenant__c",
        }

    def test_overlay_defaults_are_empty_not_shared(self):
        """Default list/dict fields are independent per instance (no shared mutable default)."""
        from app.entity_overlays.base_overlay import EntityExtractionOverlay

        a = EntityExtractionOverlay(org_id="a", connector_id="salesforce")
        b = EntityExtractionOverlay(org_id="b", connector_id="salesforce")
        a.person_fields.append("x")
        assert b.person_fields == [], "default_factory must give each overlay its own list"

    def test_overlay_requires_org_and_connector(self):
        from app.entity_overlays.base_overlay import EntityExtractionOverlay

        with pytest.raises(ValueError):
            EntityExtractionOverlay(org_id="", connector_id="salesforce")
        with pytest.raises(ValueError):
            EntityExtractionOverlay(org_id="a", connector_id="")

    def test_no_circular_imports(self):
        """base_overlay imports standalone and the extractor importing it does not cycle."""
        import importlib

        importlib.import_module("app.entity_overlays.base_overlay")
        importlib.import_module("app.entity_overlays.overlay_registry")
        importlib.import_module("app.entity_overlays.ncino_overlay")
        importlib.import_module("app.entity_extractor")


# ===========================================================================
# T2 — overlay registry
# ===========================================================================

class TestOverlayRegistry:
    """T2: register/get, None default, org + connector isolation."""

    def _overlay(self, org_id, connector_id="salesforce", version="1.0.0"):
        from app.entity_overlays.base_overlay import EntityExtractionOverlay

        return EntityExtractionOverlay(
            org_id=org_id, connector_id=connector_id, version=version
        )

    def test_register_then_get(self):
        from app.entity_overlays.overlay_registry import get_overlay, register_overlay

        ov = self._overlay("reg-org-1")
        register_overlay(ov)
        assert get_overlay("reg-org-1", "salesforce") is ov

    def test_get_returns_none_when_unregistered(self):
        """Returning None keeps default extraction safe for orgs without an overlay."""
        from app.entity_overlays.overlay_registry import get_overlay

        assert get_overlay("never-registered-org", "salesforce") is None

    def test_org_isolation(self):
        from app.entity_overlays.overlay_registry import get_overlay, register_overlay

        register_overlay(self._overlay("iso-org-A"))
        assert get_overlay("iso-org-A", "salesforce") is not None
        assert get_overlay("iso-org-B", "salesforce") is None

    def test_connector_isolation(self):
        from app.entity_overlays.overlay_registry import get_overlay, register_overlay

        register_overlay(self._overlay("conn-iso", connector_id="salesforce"))
        assert get_overlay("conn-iso", "salesforce") is not None
        assert get_overlay("conn-iso", "servicenow") is None

    def test_reregister_replaces_for_version_bump(self):
        from app.entity_overlays.overlay_registry import get_overlay, register_overlay

        register_overlay(self._overlay("ver-org", version="1.0.0"))
        register_overlay(self._overlay("ver-org", version="2.0.0"))
        assert get_overlay("ver-org", "salesforce").version == "2.0.0"

    def test_unregister(self):
        from app.entity_overlays.overlay_registry import (
            get_overlay,
            register_overlay,
            unregister_overlay,
        )

        register_overlay(self._overlay("unreg-org"))
        unregister_overlay("unreg-org", "salesforce")
        assert get_overlay("unreg-org", "salesforce") is None

    def test_register_rejects_non_overlay(self):
        from app.entity_overlays.overlay_registry import register_overlay

        with pytest.raises(TypeError):
            register_overlay({"org_id": "x"})

    def test_register_rejects_invalid_service_account_regex(self):
        from app.entity_overlays.base_overlay import EntityExtractionOverlay
        from app.entity_overlays.overlay_registry import register_overlay

        overlay = EntityExtractionOverlay(
            org_id="bad-regex",
            connector_id="salesforce",
            service_account_patterns=[r"^[invalid"],
        )

        with pytest.raises(ValueError, match="Invalid service_account_patterns regex"):
            register_overlay(overlay)

    def test_startup_helper_logs_invalid_overlay_and_continues(self, caplog):
        from app.entity_overlays.base_overlay import EntityExtractionOverlay
        from app.entity_overlays.overlay_registry import (
            get_overlay,
            register_startup_overlay,
        )

        broken = EntityExtractionOverlay(
            org_id="startup-bad",
            connector_id="salesforce",
            service_account_patterns=[r"^[invalid"],
        )

        caplog.set_level(logging.WARNING)
        assert register_startup_overlay(broken) is False
        assert get_overlay("startup-bad", "salesforce") is None
        assert "entity overlay startup registration failed" in caplog.text

    def test_startup_sequence_callable_and_safe(self):
        """AC7 support: the startup registration hook runs without raising."""
        from app.entity_overlays.overlay_registry import register_startup_overlays

        register_startup_overlays()  # must not raise


# ===========================================================================
# T3 / AC6 — nCino common patterns
# ===========================================================================

class TestNcinoCommonPatterns:
    """AC6: common nCino patterns extract loan/officer/covenant entities."""

    def _register_common(self, org_id):
        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        from app.entity_overlays.overlay_registry import register_overlay

        register_overlay(build_ncino_overlay(org_id))

    def test_common_overlay_has_expected_person_fields(self):
        from app.entity_overlays.ncino_overlay import NCINO_COMMON_PERSON_FIELDS

        fields = {(r.object_api_name, r.field_api_name) for r in NCINO_COMMON_PERSON_FIELDS}
        assert ("LLC_BI__Loan__c", "OwnerId") in fields
        assert ("LLC_BI__Loan__c", "LLC_BI__Loan_Officer__c") in fields
        assert ("LLC_BI__Covenant__c", "LLC_BI__Covenant_Analyst__c") in fields

    def test_common_overlay_has_service_account_patterns(self):
        from app.entity_overlays.ncino_overlay import NCINO_SERVICE_ACCOUNT_PATTERNS

        assert any("integration" in p for p in NCINO_SERVICE_ACCOUNT_PATTERNS)

    def test_build_ncino_overlay_accepts_none_extras(self):
        from app.entity_overlays.ncino_overlay import build_ncino_overlay

        overlay = build_ncino_overlay(
            "none-extras",
            extra_person_fields=None,
            extra_team_fields=None,
            extra_object_rules=None,
            extra_service_account_patterns=None,
        )

        assert overlay.org_id == "none-extras"
        assert overlay.service_account_patterns

    def test_extracts_loan_owner_id_person(self):
        org, run = "ncino-owner", "run-ncino-owner"
        self._register_common(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"OwnerId": {"name": "Sarah Chen", "id": "005xx0000001"}, "Name": "LOAN-1"}
                ]
            }},
        )
        persons = _db_by_type(org, run, "person")
        names = {p["display_name"] for p in persons}
        assert "Sarah Chen" in names

    def test_extracts_loan_officer_person(self):
        org, run = "ncino-officer", "run-ncino-officer"
        self._register_common(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"LLC_BI__Loan_Officer__c": {"name": "Bob Lee", "id": "005xx0000777"},
                     "Name": "LOAN-2"}
                ]
            }},
        )
        persons = _db_by_type(org, run, "person")
        assert any(p["display_name"] == "Bob Lee" for p in persons)

    def test_extracts_covenant_analyst_and_covenant_object(self):
        org, run = "ncino-cov", "run-ncino-cov"
        self._register_common(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Covenant__c": [
                    {
                        "Name": "Covenant DSCR",
                        "Id": "a01xx0000001",
                        "LLC_BI__Covenant_Analyst__c": {"name": "Dana Fox", "id": "005xx0000009"},
                    }
                ]
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        objects = {o["display_name"] for o in _db_by_type(org, run, "object")}
        assert "Dana Fox" in persons
        assert "Covenant DSCR" in objects

    def test_extracts_loan_object(self):
        org, run = "ncino-loanobj", "run-ncino-loanobj"
        self._register_common(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [{"Name": "LOAN-XYZ", "Id": "a02xx0000001"}]
            }},
        )
        objects = {o["display_name"] for o in _db_by_type(org, run, "object")}
        assert "LOAN-XYZ" in objects


# ===========================================================================
# AC1 — overlay precedence + fall back to default (additive union)
# ===========================================================================

class TestAC1OverlayPrecedenceAndFallback:
    def _overlay_with_custom_officer(self, org_id):
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            PersonFieldRule,
        )
        from app.entity_overlays.overlay_registry import register_overlay

        register_overlay(
            EntityExtractionOverlay(
                org_id=org_id,
                connector_id="salesforce",
                version="1.0.0",
                person_fields=[
                    PersonFieldRule(
                        object_api_name="LLC_BI__Loan__c",
                        field_api_name="CNB_Relationship_Manager__c",
                        resolution_source="id",
                        label="Relationship Manager",
                    )
                ],
            )
        )

    def test_overlay_custom_field_extracted(self):
        """AC1: an overlay-only custom field produces a Person the default path would miss."""
        org, run = "ac1-custom", "run-ac1-custom"
        self._overlay_with_custom_officer(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"CNB_Relationship_Manager__c": {"name": "Rita Vance", "id": "005rm0001"}}
                ]
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Rita Vance" in persons

    def test_default_fields_still_extracted_with_overlay_active(self):
        """AC1: fields not covered by the overlay fall back to default extraction."""
        org, run = "ac1-fallback", "run-ac1-fallback"
        self._overlay_with_custom_officer(org)
        # 'approval_processes' is a DEFAULT salesforce extraction path, not in the overlay.
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [{"process_name": "Credit", "approver_ids": ["005APPR1"]}],
                "LLC_BI__Loan__c": [
                    {"CNB_Relationship_Manager__c": {"name": "Rita Vance", "id": "005rm0001"}}
                ],
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Rita Vance" in persons, "overlay custom field must be extracted"
        assert "005APPR1" in persons, "default approval path must still run (fallback)"

    def test_no_overlay_does_not_extract_custom_field(self):
        """Control: without the overlay, the custom field is NOT extracted (proves overlay caused it)."""
        org, run = "ac1-control", "run-ac1-control"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"CNB_Relationship_Manager__c": {"name": "Rita Vance", "id": "005rm0001"}}
                ]
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Rita Vance" not in persons


# ===========================================================================
# AC3 — overlay OwnerId resolution_source='id' → confidence 1.0
# ===========================================================================

class TestAC3IdResolutionConfidence:
    def test_id_based_person_confidence_1_0(self):
        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "ac3-id", "run-ac3-id"
        register_overlay(build_ncino_overlay(org))
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"OwnerId": {"name": " Id Anchor", "id": "005ID0001"}, "Name": "L-1"}
                ]
            }},
        )
        person = _person_by_canonical(org, "id anchor")
        assert person is not None
        assert person["resolution_confidence"] == 1.0
        assert person["source_record_id"] == "005ID0001"

    def test_name_based_rule_confidence_0_8(self):
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            PersonFieldRule,
        )
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "ac3-name", "run-ac3-name"
        register_overlay(
            EntityExtractionOverlay(
                org_id=org, connector_id="salesforce",
                person_fields=[
                    PersonFieldRule(
                        object_api_name="LLC_BI__Loan__c",
                        field_api_name="Loan_Officer_Name__c",
                        resolution_source="name",  # name-only → 0.8
                    )
                ],
            )
        )
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [{"Loan_Officer_Name__c": "Plain Name"}]
            }},
        )
        person = _person_by_canonical(org, "plain name")
        assert person is not None
        assert person["resolution_confidence"] == 0.8
        assert person["source_record_id"] is None


# ===========================================================================
# AC4 — cross-system Person resolution anchored to Salesforce OwnerId
# ===========================================================================

class TestAC4CrossSystemResolution:
    def test_confidence_retained_and_sources_listed(self):
        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "ac4-xsys", "run-ac4-xsys"
        register_overlay(build_ncino_overlay(org))
        _extract(
            org_id=org, run_id=run,
            ingestor_data={
                # Salesforce overlay: loan owner by ID (confidence 1.0, anchor)
                "salesforce": {
                    "LLC_BI__Loan__c": [
                        {"OwnerId": {"name": "Sarah Chen", "id": "005xx0000001"}, "Name": "L-9"}
                    ]
                },
                # ServiceNow default: same person seen by name
                "servicenow": {
                    "incident_metrics": {
                        "incidents": [
                            {"number": "INC900", "assigned_to": {"display_value": "Sarah Chen"}}
                        ]
                    }
                },
            },
        )
        person = _person_by_canonical(org, "sarah chen")
        assert person is not None
        # Confidence stays 1.0 — anchored to the Salesforce ID, never downgraded.
        assert person["resolution_confidence"] == 1.0
        # Both systems recorded in metadata.sources.
        sources = _meta(person).get("sources", [])
        assert "salesforce" in sources
        assert "servicenow" in sources

    def test_single_resolved_entity_across_systems(self):
        """The same loan officer across SF + SN resolves to ONE person row."""
        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "ac4-single", "run-ac4-single"
        register_overlay(build_ncino_overlay(org))
        _extract(
            org_id=org, run_id=run,
            ingestor_data={
                "salesforce": {
                    "LLC_BI__Loan__c": [
                        {"OwnerId": {"name": "Uno Person", "id": "005uno"}, "Name": "L-1"}
                    ]
                },
                "servicenow": {
                    "incident_metrics": {
                        "incidents": [
                            {"number": "INC1", "assigned_to": {"display_value": "Uno Person"}}
                        ]
                    }
                },
            },
        )
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entities WHERE org_id=? AND entity_type='person' AND canonical_name='uno person'",
                (org,),
            ).fetchall()
        assert len(rows) == 1, "cross-system person must resolve to a single row"


# ===========================================================================
# AC5 — service account filtering + telemetry
# ===========================================================================

class TestAC5ServiceAccountFilter:
    def _register_with_patterns(self, org_id):
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            PersonFieldRule,
        )
        from app.entity_overlays.overlay_registry import register_overlay

        register_overlay(
            EntityExtractionOverlay(
                org_id=org_id,
                connector_id="salesforce",
                person_fields=[
                    PersonFieldRule("LLC_BI__Loan__c", "OwnerId", "id", "Loan Owner")
                ],
                service_account_patterns=[r"^integration[_\s]user", r"^api[_\s]user"],
            )
        )

    def test_matching_overlay_person_filtered(self):
        """A service-account display_name from an overlay field is not stored."""
        org, run = "ac5-overlay", "run-ac5-overlay"
        self._register_with_patterns(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"OwnerId": {"name": "Integration User", "id": "005int"}, "Name": "L-1"},
                    {"OwnerId": {"name": "Real Officer", "id": "005real"}, "Name": "L-2"},
                ]
            }},
        )
        names = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Integration User" not in names, "service account must be filtered"
        assert "Real Officer" in names, "non-matching person must still be stored"

    def test_matching_default_path_person_filtered(self):
        """Filtering also covers the DEFAULT extraction path while an overlay is active."""
        org, run = "ac5-default", "run-ac5-default"
        self._register_with_patterns(org)
        # 'approval_processes' is the DEFAULT salesforce path (not overlay).
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [
                    {"process_name": "P", "approver_ids": ["API User", "Genuine Approver"]}
                ]
            }},
        )
        names = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "API User" not in names
        assert "Genuine Approver" in names

    def test_mixed_case_service_account_is_filtered(self):
        """Service-account regex matching is case-insensitive for real Salesforce names."""
        org, run = "ac5-case", "run-ac5-case"
        self._register_with_patterns(org)
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [
                    {"process_name": "P", "approver_ids": ["Integration_User", "Real User"]}
                ]
            }},
        )
        names = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Integration_User" not in names
        assert "Real User" in names

    def test_filtered_default_path_does_not_log_warning_or_increment_failure_count(
        self,
        caplog,
    ):
        """Regression: filtered service accounts are not extraction failures."""
        org, run = "ac5-no-fail", "run-ac5-no-fail"
        self._register_with_patterns(org)
        caplog.set_level(logging.WARNING)

        with patch("app.telemetry.record_event") as mock_record:
            _extract(
                org_id=org, run_id=run,
                ingestor_data={"salesforce": {
                    "approval_processes": [
                        {"process_name": "P", "approver_ids": ["API User"]}
                    ]
                }},
            )

        payloads = [
            c.args[1] for c in mock_record.call_args_list
            if c.args and c.args[0] == "entity.extraction_completed"
        ]
        assert payloads
        assert payloads[0]["filtered_service_account_count"] == 1
        assert payloads[0]["failure_count"] == 0
        assert "extraction failed" not in caplog.text.lower()

    def test_filtered_count_in_telemetry(self):
        org, run = "ac5-telemetry", "run-ac5-telemetry"
        self._register_with_patterns(org)
        with patch("app.telemetry.record_event") as mock_record:
            _extract(
                org_id=org, run_id=run,
                ingestor_data={"salesforce": {
                    "LLC_BI__Loan__c": [
                        {"OwnerId": {"name": "Integration User", "id": "005int"}, "Name": "L-1"},
                    ]
                }},
            )
        # locate the entity.extraction_completed call
        payloads = [
            c.args[1] for c in mock_record.call_args_list
            if c.args and c.args[0] == "entity.extraction_completed"
        ]
        assert payloads, "entity.extraction_completed must be emitted"
        assert payloads[0].get("filtered_service_account_count", 0) >= 1

    def test_no_overlay_filter_count_zero(self):
        """Without an overlay there is no filtering; telemetry count is 0."""
        org, run = "ac5-none", "run-ac5-none"
        with patch("app.telemetry.record_event") as mock_record:
            _extract(
                org_id=org, run_id=run,
                ingestor_data={"salesforce": {
                    "approval_processes": [{"approver_ids": ["Integration User"]}]
                }},
            )
        payloads = [
            c.args[1] for c in mock_record.call_args_list
            if c.args and c.args[0] == "entity.extraction_completed"
        ]
        assert payloads
        assert payloads[0].get("filtered_service_account_count", 0) == 0
        # And the would-be service account IS stored when no overlay is active.
        names = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "Integration User" in names


class TestOverlayRecordDeduplication:
    def test_same_record_under_direct_and_typed_bucket_is_extracted_once(self):
        """Regression: duplicate payload shapes must not double-increment run_count."""
        from app.entity_overlays.base_overlay import (
            EntityExtractionOverlay,
            PersonFieldRule,
        )
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "dedupe-org", "run-dedupe"
        register_overlay(
            EntityExtractionOverlay(
                org_id=org,
                connector_id="salesforce",
                person_fields=[
                    PersonFieldRule(
                        object_api_name="LLC_BI__Loan__c",
                        field_api_name="CNB_Relationship_Manager__c",
                        resolution_source="id",
                    )
                ],
            )
        )

        direct_record = {
            "Id": "a02same",
            "CNB_Relationship_Manager__c": {"name": "Rita Vance", "id": "005rm0001"},
        }
        typed_record = {
            "Id": "a02same",
            "attributes": {"type": "LLC_BI__Loan__c"},
            "CNB_Relationship_Manager__c": {"name": "Rita Vance", "id": "005rm0001"},
        }

        _extract(
            org_id=org,
            run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [direct_record],
                "records": [typed_record],
            }},
        )

        person = _person_by_canonical(org, "rita vance")
        assert person is not None
        assert person["run_count"] == 1


class TestOverlayContextIsolation:
    def test_concurrent_extracts_keep_service_account_context_per_thread(self):
        from app.entity_overlays.base_overlay import EntityExtractionOverlay
        from app.entity_overlays.overlay_registry import register_overlay

        org_a, run_a = "thread-org-a", "run-thread-a"
        org_b, run_b = "thread-org-b", "run-thread-b"
        register_overlay(
            EntityExtractionOverlay(
                org_id=org_a,
                connector_id="salesforce",
                service_account_patterns=[r"^api user a$"],
            )
        )
        register_overlay(
            EntityExtractionOverlay(
                org_id=org_b,
                connector_id="salesforce",
                service_account_patterns=[r"^api user b$"],
            )
        )

        def _run(org: str, run: str) -> set[str]:
            _extract(
                org_id=org,
                run_id=run,
                ingestor_data={"salesforce": {
                    "approval_processes": [
                        {"process_name": "P", "approver_ids": ["API User A", "API User B"]}
                    ]
                }},
            )
            return {p["display_name"] for p in _db_by_type(org, run, "person")}

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_run, org_a, run_a)
            future_b = pool.submit(_run, org_b, run_b)
            names_a = future_a.result()
            names_b = future_b.result()

        assert "API User A" not in names_a
        assert "API User B" in names_a
        assert "API User A" in names_b
        assert "API User B" not in names_b


# ===========================================================================
# AC2 — no overlay → default extraction unchanged
# ===========================================================================

class TestAC2NoRegressionWithoutOverlay:
    def test_jira_person_unchanged(self):
        org, run = "ac2-jira", "run-ac2-jira"
        _extract(
            org_id=org, run_id=run, pack_id="service_cloud",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [
                    {"key": "CRM-1", "assignee": {"displayName": "Alice Wan"}}
                ]}
            }},
        )
        person = _person_by_canonical(org, "alice wan")
        assert person is not None
        assert person["resolution_confidence"] == 0.8
        assert person["source_record_id"] is None
        # No overlay → no provenance metadata injected.
        assert "sources" not in _meta(person)

    def test_salesforce_default_owner_unchanged(self):
        org, run = "ac2-sf", "run-ac2-sf"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "records": [{"OwnerId": "005DEFAULT"}]
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "005DEFAULT" in persons

    def test_detector_system_and_process_unchanged(self):
        org, run = "ac2-det", "run-ac2-det"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="salesforce", detector_id="HANDOFF")],
        )
        systems = {s["display_name"] for s in _db_by_type(org, run, "system")}
        processes = {p["display_name"] for p in _db_by_type(org, run, "process")}
        assert "salesforce" in systems
        assert "HANDOFF" in processes

    def test_extract_returns_list_without_overlay(self):
        result = _extract(org_id="ac2-empty", run_id="run-ac2-empty")
        assert isinstance(result, list)


# ===========================================================================
# AC7 — overlay active by first run, no restart
# ===========================================================================

class TestAC7ActiveByFirstRun:
    def test_overlay_active_immediately_after_registration(self):
        from app.entity_overlays.ncino_overlay import build_ncino_overlay
        from app.entity_overlays.overlay_registry import register_overlay

        org, run = "ac7-immediate", "run-ac7-immediate"
        # Register, then run extraction in the same process — no restart.
        register_overlay(build_ncino_overlay(org))
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "LLC_BI__Loan__c": [
                    {"OwnerId": {"name": "First Run", "id": "005first"}, "Name": "L-1"}
                ]
            }},
        )
        persons = {p["display_name"] for p in _db_by_type(org, run, "person")}
        assert "First Run" in persons


# ===========================================================================
# AC8 — authoring docs exist
# ===========================================================================

class TestAC8AuthoringDocs:
    def _doc_path(self) -> Path:
        # tests/contract/ -> backend/ -> repo root
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "docs" / "entity_overlay_authoring.md"

    def test_doc_exists(self):
        assert self._doc_path().exists(), "docs/entity_overlay_authoring.md must exist"

    def test_doc_has_session_1_template(self):
        text = self._doc_path().read_text(encoding="utf-8").lower()
        assert "session 1 field inventory" in text
        assert "object api name" in text
        assert "service-account" in text or "service account" in text

    def test_doc_has_authoring_steps_and_example(self):
        text = self._doc_path().read_text(encoding="utf-8")
        lowered = text.lower()
        assert "register_overlay" in text
        assert "example only" in lowered
        assert "default extraction paths" in lowered
        assert "city national" in lowered  # example structure present
        # step-by-step process present
        assert "authoring process" in lowered or "step" in lowered
