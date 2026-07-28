"""MSP-B12 release contract for the complete Security Operations pack.

The suite composes production contracts over deterministic fixtures. It performs
no network I/O and requires neither ServiceNow nor a model provider.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

import pytest

from discovery.models import DetectorResult
from discovery.packs import security_ops_aggregation_floor as aggregation
from discovery.packs import security_ops_ai_mode as ai_mode
from discovery.packs import security_ops_evidence_resolver as evidence_resolver
from discovery.packs import security_ops_finding as finding_contract
from discovery.packs import security_ops_scorer
from discovery.packs.template_registry import (
    TemplateDefinition,
    get_template,
    resolve_launch_config,
)


try:
    fixtures = importlib.import_module("test_security_ops_detectors")
except ModuleNotFoundError:  # pragma: no cover - package-style test collection
    sys.path.insert(0, os.path.dirname(__file__))
    fixtures = importlib.import_module("test_security_ops_detectors")


DETECTORS = tuple(fixtures.ALL_DETECTORS)
HOST_MARKER = "10.47.47.47"
VULNERABILITY_MARKER = "CVE-2099-42424"
PERSON_MARKERS = ("Alex Analyst", "Dana Cruz", "Release Person")


def _module(name: str):
    return importlib.import_module(f"discovery.detectors.{name}")


def _estate() -> Dict[str, Any]:
    """Canonical estate, enriched with source-only identifiers that must not leak."""
    estate = fixtures._estate()
    for item in estate["vulnerability_response"]["vulnerable_items"]:
        item["hostname"] = HOST_MARKER
        item["cve"] = VULNERABILITY_MARKER
        item["vulnerability_instance_id"] = f"instance-{item['sys_id']}"
    return estate


def _detect(estate: Dict[str, Any]) -> List[DetectorResult]:
    return [
        result
        for detector in DETECTORS
        for result in _module(detector).detect(None, estate, None)
    ]


def _stable_shape(results: Iterable[DetectorResult]) -> List[tuple]:
    return sorted(
        (
            result.detector_id,
            result.metric_value,
            result.threshold,
            result.raw_evidence["finding_ref"],
        )
        for result in results
    )


def _serialized_surfaces(results: List[DetectorResult]) -> Dict[str, Any]:
    """Build the pack's persisted and user-facing shapes from real findings."""
    findings = [asdict(result) for result in results]
    raw_evidence = [result.raw_evidence for result in results]
    summaries = [{
        "detector_id": result.detector_id,
        "confidence": result.raw_evidence["finding_contract"]["confidence"]["level"],
        "corroboration_status": result.raw_evidence["finding_contract"]["corroboration"]["status"],
    } for result in results]
    reports = {"sections": [{
        "title": "Security Operations workload",
        "cells": [
            {
                "detector_id": result.detector_id,
                "evidence": result.raw_evidence["finding_contract"]["evidence"],
            }
            for result in results
        ],
    }]}
    exports = [{
        "detector_id": result.detector_id,
        "finding_ref": result.raw_evidence["finding_ref"],
        "contract": result.raw_evidence["finding_contract"],
    } for result in results]
    api_response = json.loads(json.dumps({
        "packId": "security_ops",
        "findings": findings,
        "summary": summaries,
        "report": reports,
        "export": exports,
    }, sort_keys=True))
    return {
        "findings": findings,
        "raw_evidence": raw_evidence,
        "summaries": summaries,
        "reports": reports,
        "exports": exports,
        "serialized_api_response": api_response,
    }


class TestRepresentativeEstate:
    def test_at_least_four_detectors_fire_with_complete_contracts(self):
        results = _detect(_estate())
        fired = {result.detector_id for result in results}
        assert len(fired) >= 4
        assert len(fired) == 5
        for result in results:
            contract = result.raw_evidence["finding_contract"]
            assert finding_contract.is_contract_complete(contract)
            assert contract["evidence"]
            assert contract["confidence"]["level"]
            assert contract["corroboration"]["status"]
            assert contract["source_trace"]["systems"]
            assert contract["source_trace"]["artifacts"]
            assert finding_contract.find_invalid_evidence_pointers(contract["source_trace"]) == []

    def test_pack_boundary_accepts_every_seeded_finding(self):
        results = _detect(_estate())
        assert finding_contract.enforce_pack_findings(results) == len(results)
        assert aggregation.enforce_pack_output(results) == len(results)


class TestRecursiveAggregationBoundary:
    def test_source_identifiers_never_reach_any_output_surface(self):
        estate = _estate()
        assert HOST_MARKER in json.dumps(estate)
        assert VULNERABILITY_MARKER in json.dumps(estate)

        for surface, payload in _serialized_surfaces(_detect(estate)).items():
            serialized = json.dumps(payload, sort_keys=True)
            assert HOST_MARKER not in serialized, surface
            assert VULNERABILITY_MARKER not in serialized, surface
            assert aggregation.find_output_violations(payload) == [], surface

    def test_recursive_sweep_rejects_a_pair_on_every_surface_shape(self):
        pair = f"{HOST_MARKER} is affected by {VULNERABILITY_MARKER}"
        shapes = {
            "finding": {"explanation": pair},
            "raw_evidence": {"finding_contract": {"evidence": {"detail": pair}}},
            "summary": {"summary": {"text": pair}},
            "report": {"sections": [{"cells": [{"text": pair}]}]},
            "export": {"rows": [{"detail": pair}]},
            "api": json.loads(json.dumps({"data": {"findings": [{"detail": pair}]}})),
        }
        for surface, payload in shapes.items():
            violations = aggregation.find_output_violations(payload)
            assert any(v["kind"] == "host_vuln_pair_in_text" for v in violations), surface

    def test_person_names_and_person_fields_never_reach_outputs(self):
        surfaces = _serialized_surfaces(_detect(_estate()))
        serialized = json.dumps(surfaces, sort_keys=True)
        for person in PERSON_MARKERS:
            assert person not in serialized
        assert aggregation.find_output_violations(surfaces) == []


def _depth_estate(hops: int) -> Dict[str, Any]:
    estate = _estate()
    cmdb = estate["cmdb"]
    relationships = []
    extra_cis = []
    for suffix in ("001", "002", "003"):
        previous = f"ci-server-{suffix}"
        for level in range(1, hops):
            bridge = f"ci-bridge-{suffix}-{level}"
            extra_cis.append({
                "sys_id": bridge,
                "ci_class": "cmdb_ci_service",
                "name": f"bridge-{suffix}-{level}",
            })
            relationships.append({
                "relationship_type": "depends_on",
                "source_ci_id": previous,
                "target_ci_id": bridge,
            })
            previous = bridge
        relationships.append({
            "relationship_type": "depends_on",
            "source_ci_id": previous,
            "target_ci_id": "ci-storage-001",
        })
    cmdb["configuration_items"].extend(extra_cis)
    cmdb["relationships"] = relationships
    return estate


class TestSharedInfrastructureConcentration:
    def test_multi_service_common_ci_fires_within_depth_and_never_claims_causation(self):
        results = fixtures._detect(fixtures.CONCENTRATION, _depth_estate(hops=2))
        assert len(results) == 1
        contract = results[0].raw_evidence["finding_contract"]
        evidence = contract["evidence"]
        assert evidence["service_count"] == 3
        assert evidence["max_hop_observed"] == 2
        assert evidence["max_hop_observed"] <= evidence["max_hops"]
        explanations = [evidence["statement"], contract["corroboration"]["label"]]
        assert all("concentrat" in text.casefold() for text in explanations)
        assert all(finding_contract.find_causal_language(text) == [] for text in explanations)

    @pytest.mark.parametrize("estate", [lambda: _depth_estate(3), lambda: _without_paths()])
    def test_no_finding_without_a_path_inside_the_depth_limit(self, estate):
        assert fixtures._detect(fixtures.CONCENTRATION, estate()) == []


def _without_paths() -> Dict[str, Any]:
    estate = _estate()
    estate["cmdb"]["relationships"] = []
    return estate


class TestAiModes:
    @pytest.mark.parametrize(
        "mode,allowed,expected_calls",
        [("hosted", False, 0), ("in_boundary", True, 5), ("customer_tenant", True, 5)],
    )
    def test_mode_changes_assembly_only_never_detector_results(self, mode, allowed, expected_calls):
        results = _detect(_estate())
        before = _stable_shape(results)
        gate = ai_mode.apply_ai_mode_gate(results, mode=mode)
        assert _stable_shape(results) == before
        assert gate["ai_assembly_allowed"] is allowed

        calls = []
        assembled = ai_mode.assemble_narrative(
            results,
            generate_fn=lambda finding: calls.append(finding.detector_id) or "deterministic-test-narrative",
            mode=mode,
        )
        assert len(calls) == expected_calls
        assert assembled["ai_assembled"] is allowed
        for result in results:
            if mode == "hosted":
                assert result.raw_evidence["ai_mode_label"] == ai_mode.HOSTED_NARRATIVE_LABEL
                assert result.raw_evidence["ai_narrative_available"] is False
            else:
                assert "ai_mode_label" not in result.raw_evidence
                assert result.raw_evidence["ai_narrative_available"] is True


class TestTemplateAndScoring:
    def test_security_and_cloud_templates_coexist_without_model_changes(self):
        assert [field.name for field in fields(TemplateDefinition)] == [
            "template_id", "label", "description", "suggested_systems",
            "suggested_roles", "focus_defaults", "pack_id", "detector_emphasis",
            "terminology", "metadata",
        ]
        security = get_template("security_operations")
        cloud = get_template("managed_cloud_operations")
        assert security is not None and cloud is not None
        resolved = resolve_launch_config(
            "managed_cloud_operations",
            template_ids=["managed_cloud_operations", "security_operations"],
        )
        assert resolved["effective"]["pack_ids"] == ["cloud_ops", "security_ops"]
        assert {item["pack_id"] for item in resolved["effective"]["pack_boundaries"]} == {
            "cloud_ops", "security_ops",
        }

    def test_critical_workload_outranks_informational_at_equal_inputs(self):
        base = fixtures._detect(fixtures.RECURRENCE, _estate())[0]
        critical = copy.deepcopy(base)
        informational = copy.deepcopy(base)
        for result, band, ref in (
            (critical, "critical", "critical-equal-input"),
            (informational, "informational", "informational-equal-input"),
        ):
            result.raw_evidence["severity_band"] = band
            result.raw_evidence["finding_contract"]["evidence"]["severity_band"] = band
            result.raw_evidence["finding_ref"] = ref
        ranking = security_ops_scorer.rank_security_ops_findings([informational, critical])
        assert ranking[id(critical)]["rank"] == 1
        assert ranking[id(critical)]["ops_impact_score"] > ranking[id(informational)]["ops_impact_score"]


class TestTenantAndEvidenceSecurity:
    def test_foreign_org_records_do_not_change_any_detector_output(self):
        estate = _estate()
        expected = _stable_shape(_detect(estate))
        mixed = copy.deepcopy(estate)
        for block, collection in (
            ("vulnerability_response", "vulnerable_items"),
            ("vulnerability_response", "remediation_tasks"),
            ("secops", "security_incidents"),
        ):
            foreign = copy.deepcopy(mixed[block][collection][0])
            foreign["org_id"] = "org-b"
            foreign["sys_id"] = f"org-b-{foreign['sys_id']}"
            foreign["assignment_group"] = "Foreign Org Queue"
            foreign["assigned_to"] = "Release Person"
            mixed[block][collection].append(foreign)
        assert _stable_shape(_detect(mixed)) == expected

    def test_pointer_resolution_is_org_scoped_authorized_and_access_logged(self):
        estate = _estate()
        results = _detect(estate)
        pointer = results[0].raw_evidence["finding_contract"]["source_trace"]["artifacts"][0]
        store = evidence_resolver.InMemoryEvidenceRecordStore()
        assert evidence_resolver.index_signal_records(store, "org-a", estate) > 0
        events = []
        clock = lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

        resolved = evidence_resolver.resolve_evidence_pointer(
            pointer,
            requesting_org="org-a",
            user_id="release-analyst",
            role="analyst",
            store=store,
            emit=lambda event, payload: events.append((event, payload)),
            now=clock,
        )
        assert resolved["resolved"] is True
        assert events[-1][0] == evidence_resolver.AUDIT_EVENT
        assert events[-1][1]["outcome"] == evidence_resolver.OUTCOME_RESOLVED
        assert events[-1][1]["org_id"] == "org-a"

        with pytest.raises(evidence_resolver.EvidenceAccessDenied):
            evidence_resolver.resolve_evidence_pointer(
                pointer,
                requesting_org="org-b",
                user_id="foreign-analyst",
                role="analyst",
                store=store,
                emit=lambda event, payload: events.append((event, payload)),
                now=clock,
            )
        assert events[-1][1]["outcome"] == evidence_resolver.OUTCOME_DENIED
        assert events[-1][1]["reason"] == evidence_resolver.REASON_NOT_FOUND_OR_CROSS_ORG

    def test_viewer_cannot_resolve_an_evidence_pointer(self):
        estate = _estate()
        pointer = _detect(estate)[0].raw_evidence["finding_contract"]["source_trace"]["artifacts"][0]
        store = evidence_resolver.InMemoryEvidenceRecordStore()
        evidence_resolver.index_signal_records(store, "org-a", estate)
        with pytest.raises(evidence_resolver.EvidenceAccessDenied) as error:
            evidence_resolver.resolve_evidence_pointer(
                pointer,
                requesting_org="org-a",
                user_id="release-viewer",
                role="viewer",
                store=store,
                emit=lambda *_: None,
            )
        assert error.value.reason == evidence_resolver.REASON_INSUFFICIENT_ROLE
