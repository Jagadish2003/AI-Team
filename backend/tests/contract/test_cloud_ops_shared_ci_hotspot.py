"""
Contract tests for MSP-B6 T3 (AT-738) — SHARED_CI_HOTSPOT detector.

Acceptance criteria:
  AC1 — fires on a seeded multi-service concentration within the depth bound with
        event corroboration present.
  AC2 — does NOT fire when the common CI is beyond the depth bound, or when event
        corroboration is absent within windows.
  AC3 — finding wording is concentration-shaped, never causal — enforced by a
        template-level check (not review).
  AC4 — references only services/CIs, never individuals.
"""
from __future__ import annotations

import pytest

from discovery.detectors import cloud_ops_shared_ci_hotspot as h
from discovery.packs import cloud_ops_finding as fc


def _sn(block):
    return {"cloud_ops": block}


def _contract(dr):
    return dr.raw_evidence["finding_contract"]


def _seeded_estate(*, event_window_overlap=True, include_event=True, deep_extra=False):
    """3 services concentrate on 'storage-tier' within 2 hops; event on it in-window.

    billing-api-ci -> storage-tier            (1 hop)
    invoice-ci     -> storage-tier            (1 hop)
    payments-ci    -> cache-tier -> storage-tier  (2 hops)
    """
    edges = [
        {"from": "billing-api-ci", "to": "storage-tier"},
        {"from": "invoice-ci", "to": "storage-tier"},
        {"from": "payments-ci", "to": "cache-tier"},
        {"from": "cache-tier", "to": "storage-tier"},
    ]
    if deep_extra:
        # A 4th service 3 hops away — beyond the default 2-hop bound.
        edges += [
            {"from": "reporting-ci", "to": "etl-tier"},
            {"from": "etl-tier", "to": "warehouse-tier"},
            {"from": "warehouse-tier", "to": "storage-tier"},
        ]
    service_incidents = [
        {"service": "billing-api", "ci": "billing-api-ci", "incident_ids": ["INC1", "INC2"], "incident_count": 2},
        {"service": "invoice-svc", "ci": "invoice-ci", "incident_ids": ["INC3"], "incident_count": 1},
        {"service": "payments", "ci": "payments-ci", "incident_ids": ["INC4", "INC5"], "incident_count": 2},
    ]
    if deep_extra:
        service_incidents.append(
            {"service": "reporting", "ci": "reporting-ci", "incident_ids": ["INC9"], "incident_count": 1})
    block = {"ci_graph": {"edges": edges}, "service_incidents": service_incidents}
    if include_event:
        block["event_signatures"] = [
            {"signature": "disk.saturation", "ci": "storage-tier",
             "window_overlap": event_window_overlap, "event_count": 40},
        ]
    return _sn(block)


# ── AC1 — fires on concentration within bound + event corroboration ─────────────

class TestAC1Fires:

    def test_fires_and_identifies_common_ci(self):
        results = h.detect(None, _seeded_estate(), None)
        assert len(results) == 1
        dr = results[0]
        ev = _contract(dr)["evidence"]
        assert ev["common_ci"] == "storage-tier"
        assert ev["service_count"] == 3
        assert set(ev["services"]) == {"billing-api", "invoice-svc", "payments"}

    def test_two_hop_service_included_within_bound(self):
        dr = h.detect(None, _seeded_estate(), None)[0]
        hops = _contract(dr)["evidence"]["hops_by_service"]
        assert hops["payments"] == 2  # reached at the depth bound
        assert hops["billing-api"] == 1

    def test_confidence_high_and_window_gated(self):
        dr = h.detect(None, _seeded_estate(), None)[0]
        c = _contract(dr)
        assert c["confidence"]["level"] == fc.CONFIDENCE_HIGH
        assert c["corroboration"]["status"] == fc.STATUS_CORROBORATED
        assert c["corroboration"]["window_gated"] is True
        assert dr.metric_value == 3.0

    def test_evaluate_agrees(self):
        assert h.evaluate(None, _seeded_estate(), None).fired is True


# ── AC2 — does NOT fire beyond depth bound or without corroboration ─────────────

class TestAC2DoesNotFire:

    def test_no_fire_without_event_corroboration(self):
        assert h.detect(None, _seeded_estate(include_event=False), None) == []
        assert h.evaluate(None, _seeded_estate(include_event=False), None).fired is False

    def test_no_fire_when_event_outside_window(self):
        assert h.detect(None, _seeded_estate(event_window_overlap=False), None) == []

    def test_common_ci_beyond_depth_bound_excluded(self):
        """A service whose only path to the shared CI is 3 hops is not counted."""
        dr = h.detect(None, _seeded_estate(deep_extra=True), None)[0]
        # 'reporting' is 3 hops from storage-tier → excluded; still exactly 3 services.
        assert "reporting" not in _contract(dr)["evidence"]["services"]
        assert _contract(dr)["evidence"]["service_count"] == 3

    def test_depth_bound_shrinks_concentration_below_min_services(self, monkeypatch):
        """With max_hops=1, the 2-hop 'payments' service drops out → 2 services < 3 → no fire."""
        monkeypatch.setattr(h, "get_detector_thresholds", lambda section, defaults: {
            "max_hops": 1, "min_services": 3, "require_event_corroboration": True})
        assert h.detect(None, _seeded_estate(), None) == []

    def test_below_min_services_does_not_fire(self):
        block = {
            "ci_graph": {"edges": [{"from": "a-ci", "to": "shared"}, {"from": "b-ci", "to": "shared"}]},
            "service_incidents": [
                {"service": "a", "ci": "a-ci", "incident_ids": ["INC1"], "incident_count": 1},
                {"service": "b", "ci": "b-ci", "incident_ids": ["INC2"], "incident_count": 1},
            ],
            "event_signatures": [{"signature": "e", "ci": "shared", "window_overlap": True}],
        }
        assert h.detect(None, _sn(block), None) == []  # only 2 services


# ── AC3 — concentration-shaped wording, never causal (template-level check) ─────

class TestAC3CausalGate:

    def test_statement_is_concentration_shaped(self):
        dr = h.detect(None, _seeded_estate(), None)[0]
        statement = _contract(dr)["evidence"]["statement"]
        assert "concentrate on a shared dependency" in statement
        assert fc.find_causal_language(statement) == []

    def test_no_causal_language_anywhere_in_finding(self):
        dr = h.detect(None, _seeded_estate(), None)[0]
        assert fc.find_causal_language(repr(dr.raw_evidence)) == []

    def test_builder_rejects_causal_output(self):
        # The gate itself: causal phrasing must raise, not slip through.
        for bad in ("Outage caused by storage-tier",
                    "Incidents due to the shared dependency",
                    "storage-tier is responsible for these incidents",
                    "the shared CI triggered by load"):
            with pytest.raises(ValueError):
                fc.assert_not_causal(bad)

    def test_build_concentration_statement_is_safe(self):
        s = fc.build_concentration_statement(service_count=12, common_ci="storage-tier", incident_count=40)
        assert "concentrate" in s and "caused by" not in s.lower()

    def test_find_causal_language_detects_phrases(self):
        assert "caused by" in fc.find_causal_language("this was caused by that")


# ── AC4 — services/CIs only, never individuals ──────────────────────────────────

class TestAC4NoIndividuals:

    def test_no_individual_references(self):
        estate = _seeded_estate()
        # Inject person fields into the input — they must never surface.
        estate["cloud_ops"]["service_incidents"][0]["assignee"] = "Jane Doe"
        estate["cloud_ops"]["service_incidents"][0]["caller"] = "John Roe"
        dr = h.detect(None, estate, None)[0]
        assert fc.find_individual_references(dr.raw_evidence) == []
        assert "Jane Doe" not in repr(dr.raw_evidence)
        assert "John Roe" not in repr(dr.raw_evidence)


# ── four-part contract + wiring ─────────────────────────────────────────────────

class TestContractAndWiring:

    def test_four_part_contract_complete(self):
        dr = h.detect(None, _seeded_estate(), None)[0]
        contract = _contract(dr)
        for field in fc.FOUR_PART_CONTRACT_FIELDS:
            assert contract.get(field)
        assert fc.is_contract_complete(contract)
        assert contract["source_trace"]["systems"] and contract["source_trace"]["artifacts"]

    def test_detector_id(self):
        assert h.DETECTOR_ID == "SHARED_CI_HOTSPOT"

    def test_registered_in_pack(self):
        from discovery.packs.pack_config import get_detector_modules
        mods = get_detector_modules("cloud_ops")
        assert any("cloud_ops_shared_ci_hotspot" in m for m in mods)
        assert len(mods) == 5

    def test_emphasised_by_a_focus(self):
        from discovery.packs.focus_affinity import all_affinity_detector_ids
        assert "SHARED_CI_HOTSPOT" in all_affinity_detector_ids()

    def test_empty_block_no_fire(self):
        assert h.detect(None, {}, None) == []
        assert h.detect(None, None, None) == []
        assert h.evaluate(None, {}, None).fired is False

    def test_config_driven_defaults_present(self):
        """The T1 external config carries the hotspot thresholds this detector reads."""
        from discovery.packs.cloud_ops_config import get_thresholds
        hp = get_thresholds().get("shared_ci_hotspot", {})
        assert hp.get("max_hops") == 2
        assert hp.get("min_services") == 3
        assert hp.get("require_event_corroboration") is True
