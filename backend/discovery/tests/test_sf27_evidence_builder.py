"""
SF-2.7 tests — Evidence Builder.

Five official unit tests grounded in SF-1.5 worked examples.
Plus schema validation, decision lock, id format, tsLabel format, and error handling.
"""
from __future__ import annotations
import itertools
import re
import pytest
from backend.discovery.models import DetectorResult
from backend.discovery.evidence_builder import build_evidence, _validate_evidence


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic ID factory for tests
# ─────────────────────────────────────────────────────────────────────────────

def make_factory(prefix="a"):
    """Returns sequential IDs: a00001, a00002, ..."""
    counter = itertools.count(1)
    return lambda: f"{prefix}{next(counter):05d}"


# ─────────────────────────────────────────────────────────────────────────────
# DetectorResult helpers (SF-1.3 dev org values)
# ─────────────────────────────────────────────────────────────────────────────

def dr_d1():
    return DetectorResult(
        detector_id="REPETITIVE_AUTOMATION", signal_source="salesforce",
        metric_value=2.128, threshold=0.6,
        raw_evidence={"flow_id": "301abc1", "flow_label": "Case-Notify",
                      "trigger_object": "Case", "records_90d": 300,
                      "element_count": 6, "active_flow_count_on_object": 4,
                      "flow_activity_score": 2.128},
    )

def dr_d2():
    return DetectorResult(
        detector_id="HANDOFF_FRICTION", signal_source="salesforce",
        metric_value=1.6, threshold=1.5,
        raw_evidence={"owner_changes_90d": 480, "total_cases_90d": 300,
                      "handoff_score": 1.6,
                      "top_categories": [
                          {"category": "Technical Issue", "handoff_score": 4.0},
                          {"category": "Billing Inquiry", "handoff_score": 3.8},
                      ]},
    )

def dr_d3():
    return DetectorResult(
        detector_id="APPROVAL_BOTTLENECK", signal_source="salesforce",
        metric_value=5.0, threshold=3.0,
        raw_evidence={"process_name": "Discount Approval", "pending_count": 60,
                      "avg_delay_days": 5.0, "approver_count": 2,
                      "bottleneck_score": 30.0},
    )

def dr_d4():
    return DetectorResult(
        detector_id="KNOWLEDGE_GAP", signal_source="salesforce",
        metric_value=0.5, threshold=0.4,
        raw_evidence={"closed_cases_90d": 60, "cases_with_kb_link": 30,
                      "knowledge_gap_score": 0.5},
    )

def dr_d5():
    return DetectorResult(
        detector_id="INTEGRATION_CONCENTRATION", signal_source="salesforce",
        metric_value=3.0, threshold=3.0,
        raw_evidence={"credential_name": "ServiceNow Integration",
                      "credential_developer_name": "ServiceNow_Integration",
                      "flow_reference_count": 3,
                      "referencing_flow_ids": ["301abc2","301abc3","301abc4"],
                      "match_type": "field_exact"},
    )

def dr_d6():
    return DetectorResult(
        detector_id="PERMISSION_BOTTLENECK", signal_source="salesforce",
        metric_value=30.0, threshold=10.0,
        raw_evidence={"process_name": "Discount Approval", "pending_count": 60,
                      "approver_count": 2, "bottleneck_score": 30.0},
    )

def dr_d7():
    return DetectorResult(
        detector_id="CROSS_SYSTEM_ECHO", signal_source="salesforce",
        metric_value=0.25, threshold=0.15,
        raw_evidence={"sf_echo_count": 75, "sf_total_cases": 300,
                      "sf_echo_score": 0.25, "sn_match_count": 80,
                      "sn_total_incidents": 500, "sn_echo_score": 0.16,
                      "jira_echo_score": 0.2214,
                      "jira_sf_label_count": 62, "jira_total_issues": 280,
                      "matched_patterns": ["INC-", "JIRA-", "CS-"]},
    )

def opp(conf="MEDIUM"):
    return {"confidence": conf, "impact": 3, "effort": 2, "tier": "Quick Win"}


# ─────────────────────────────────────────────────────────────────────────────
# Official SF-1.5 worked examples
# ─────────────────────────────────────────────────────────────────────────────

class TestOfficialWorkedExamples:
    """One test per SF-1.5 worked example. Same input must produce same structure."""

    def test_example1_d2_handoff_friction(self):
        evs = build_evidence(dr_d2(), opp("MEDIUM"), id_factory=make_factory())
        assert len(evs) == 1
        e = evs[0]
        assert e["source"] == "Salesforce"
        assert e["evidenceType"] == "Metric"
        assert "480" in e["snippet"]
        assert "300" in e["snippet"]
        assert "1.6" in e["snippet"]
        assert "1.5" in e["snippet"]   # threshold
        assert e["confidence"] == "MEDIUM"
        assert e["decision"] == "UNREVIEWED"

    def test_example2_d6_permission_bottleneck(self):
        evs = build_evidence(dr_d6(), opp("MEDIUM"), id_factory=make_factory())
        assert len(evs) == 1
        e = evs[0]
        assert e["evidenceType"] == "Metric"
        assert "60" in e["snippet"]
        assert "2" in e["snippet"]   # approver_count
        assert "30.0" in e["snippet"]  # bottleneck_score
        assert "10.0" in e["snippet"]  # threshold
        assert "Discount Approval" in e["snippet"]

    def test_example3_d3_approval_bottleneck(self):
        evs = build_evidence(dr_d3(), opp("MEDIUM"), id_factory=make_factory())
        assert len(evs) == 1
        e = evs[0]
        assert e["evidenceType"] == "Metric"
        assert "60" in e["snippet"]
        assert "5.0" in e["snippet"]   # avg_delay_days
        assert "30.0" in e["snippet"]  # bottleneck_score
        assert "Discount Approval" in e["snippet"]

    def test_example4_d4_knowledge_gap(self):
        evs = build_evidence(dr_d4(), opp("MEDIUM"), id_factory=make_factory())
        assert len(evs) == 1
        e = evs[0]
        assert e["evidenceType"] == "Metric"
        assert "30" in e["snippet"]   # cases_with_kb_link
        assert "60" in e["snippet"]   # closed_cases_90d
        assert "0.5" in e["snippet"]  # score
        assert "0.4" in e["snippet"]  # threshold

    def test_example5_d1_repetitive_automation(self):
        evs = build_evidence(dr_d1(), opp("HIGH"), id_factory=make_factory())
        assert len(evs) == 1
        e = evs[0]
        assert e["evidenceType"] == "Config"   # SF-1.5: D1 is Config not Metric
        assert e["confidence"] == "HIGH"
        assert "4" in e["snippet"]    # active_flow_count_on_object
        assert "300" in e["snippet"]  # records_90d
        assert "2.128" in e["snippet"]  # flow_activity_score
        assert "0.6" in e["snippet"]    # threshold


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation tests (SF-1.5 rules R1–R7)
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaValidation:
    def test_all_nine_fields_present(self):
        evs = build_evidence(dr_d2(), opp(), id_factory=make_factory())
        e = evs[0]
        for field in ("id", "tsLabel", "source", "evidenceType",
                      "title", "snippet", "entities", "confidence", "decision"):
            assert field in e, f"Missing field: {field}"

    def test_decision_always_unreviewed(self):
        """SF-1.5 R7: decision MUST be UNREVIEWED — algorithm never sets other values."""
        for dr in [dr_d1(), dr_d2(), dr_d3(), dr_d4(), dr_d5(), dr_d6(), dr_d7()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert evs[0]["decision"] == "UNREVIEWED", \
                f"{dr.detector_id}: decision is not UNREVIEWED"

    def test_id_prefix_format(self):
        """SF-1.5 R6: id must be ev_sf_ | ev_sn_ | ev_jira_"""
        for dr in [dr_d1(), dr_d2(), dr_d3(), dr_d4(), dr_d5(), dr_d6(), dr_d7()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            ev_id = evs[0]["id"]
            assert any(ev_id.startswith(p) for p in ("ev_sf_", "ev_sn_", "ev_jira_")), \
                f"{dr.detector_id}: invalid id prefix in '{ev_id}'"

    def test_id_never_ev_salesforce(self):
        """SF-1.5: ev_salesforce_ is the common mistake — must be ev_sf_ only."""
        for dr in [dr_d1(), dr_d2()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert not evs[0]["id"].startswith("ev_salesforce_"), \
                "id must use 'ev_sf_' not 'ev_salesforce_'"

    def test_ts_label_format(self):
        """SF-1.5: tsLabel must be DD Mon YYYY, HH:MM (UTC)."""
        import re
        pattern = r"^\d{2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}$"
        evs = build_evidence(dr_d2(), opp(), id_factory=make_factory())
        ts = evs[0]["tsLabel"]
        assert re.match(pattern, ts), f"tsLabel '{ts}' does not match DD Mon YYYY, HH:MM"

    def test_snippet_contains_digit(self):
        """SF-1.5 R1: snippet must contain at least one measurable number."""
        for dr in [dr_d1(), dr_d2(), dr_d3(), dr_d4(), dr_d5(), dr_d6(), dr_d7()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert re.search(r"\d", evs[0]["snippet"]), \
                f"{dr.detector_id}: snippet has no digits"

    def test_snippet_contains_threshold(self):
        """Snippet must reference the threshold that was exceeded."""
        evs = build_evidence(dr_d2(), opp(), id_factory=make_factory())
        assert str(dr_d2().threshold) in evs[0]["snippet"]

    def test_source_valid_enum(self):
        """SF-1.5 R2: source must be Salesforce | ServiceNow | Jira."""
        for dr in [dr_d1(), dr_d2(), dr_d3(), dr_d4(), dr_d5(), dr_d6(), dr_d7()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert evs[0]["source"] in ("Salesforce", "ServiceNow", "Jira"), \
                f"{dr.detector_id}: invalid source '{evs[0]['source']}'"

    def test_evidence_type_valid_enum(self):
        """SF-1.5 R3: evidenceType must be Metric | Config | Ticket."""
        for dr in [dr_d1(), dr_d2(), dr_d3(), dr_d4(), dr_d5(), dr_d6(), dr_d7()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert evs[0]["evidenceType"] in ("Metric", "Config", "Ticket"), \
                f"{dr.detector_id}: invalid evidenceType '{evs[0]['evidenceType']}'"


# ─────────────────────────────────────────────────────────────────────────────
# evidenceType correctness (SF-1.5)
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceTypeAssignment:
    def test_d1_is_config(self):
        """D1 is a metadata observation about flow configuration."""
        evs = build_evidence(dr_d1(), opp(), id_factory=make_factory())
        assert evs[0]["evidenceType"] == "Config"

    def test_d2_d3_d4_d6_are_metric(self):
        """D2, D3, D4, D6 produce computed measurements."""
        for dr in [dr_d2(), dr_d3(), dr_d4(), dr_d6()]:
            evs = build_evidence(dr, opp(), id_factory=make_factory())
            assert evs[0]["evidenceType"] == "Metric", \
                f"{dr.detector_id}: expected Metric"

    def test_d5_is_config(self):
        """D5 is a metadata observation about Named Credential references."""
        evs = build_evidence(dr_d5(), opp(), id_factory=make_factory())
        assert evs[0]["evidenceType"] == "Config"

    def test_d7_is_ticket(self):
        """D7 is a pattern found in individual records (cross-system references)."""
        evs = build_evidence(dr_d7(), opp(), id_factory=make_factory())
        assert evs[0]["evidenceType"] == "Ticket"


# ─────────────────────────────────────────────────────────────────────────────
# D7 three-system snippet
# ─────────────────────────────────────────────────────────────────────────────

class TestD7ThreeSystemSnippet:
    def test_d7_snippet_mentions_all_three_systems(self):
        evs = build_evidence(dr_d7(), opp(), id_factory=make_factory())
        snippet = evs[0]["snippet"]
        assert "75" in snippet    # sf_echo_count
        assert "300" in snippet   # sf_total_cases
        assert "80" in snippet    # sn_match_count
        assert "500" in snippet   # sn_total_incidents
        assert "62" in snippet    # jira_sf_label_count

    def test_d7_snippet_contains_patterns(self):
        evs = build_evidence(dr_d7(), opp(), id_factory=make_factory())
        assert "INC-" in evs[0]["snippet"] or "CS-" in evs[0]["snippet"]

    def test_d7_sf_only_still_works(self):
        """D7 with no SN or Jira data still produces valid evidence."""
        dr = DetectorResult(
            detector_id="CROSS_SYSTEM_ECHO", signal_source="salesforce",
            metric_value=0.25, threshold=0.15,
            raw_evidence={"sf_echo_count": 75, "sf_total_cases": 300,
                          "sf_echo_score": 0.25, "sn_match_count": 0,
                          "sn_total_incidents": 0, "sn_echo_score": 0.0,
                          "matched_patterns": ["INC-"]},
        )
        evs = build_evidence(dr, opp(), id_factory=make_factory())
        assert len(evs) == 1
        assert "75" in evs[0]["snippet"]


# ─────────────────────────────────────────────────────────────────────────────
# id_factory determinism
# ─────────────────────────────────────────────────────────────────────────────

class TestIdFactory:
    def test_deterministic_ids_with_factory(self):
        factory = make_factory("x")
        ev1 = build_evidence(dr_d2(), opp(), id_factory=factory)[0]
        ev2 = build_evidence(dr_d3(), opp(), id_factory=factory)[0]
        assert ev1["id"] == "ev_sf_x00001"
        assert ev2["id"] == "ev_sf_x00002"

    def test_random_ids_without_factory(self):
        ev1 = build_evidence(dr_d2(), opp())[0]
        ev2 = build_evidence(dr_d2(), opp())[0]
        # Different runs produce different IDs (probabilistically)
        # Just check format is valid
        assert re.match(r"^ev_sf_[0-9a-f]{6}$", ev1["id"]), f"Bad format: {ev1['id']}"

    def test_confidence_propagated_from_opportunity(self):
        evs_high = build_evidence(dr_d1(), opp("HIGH"), id_factory=make_factory())
        evs_low  = build_evidence(dr_d1(), opp("LOW"),  id_factory=make_factory())
        assert evs_high[0]["confidence"] == "HIGH"
        assert evs_low[0]["confidence"]  == "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# Permissive failure mode (SF-1.5)
# ─────────────────────────────────────────────────────────────────────────────

class TestPermissiveFailure:
    def test_unknown_detector_returns_empty(self):
        dr = DetectorResult(
            detector_id="UNKNOWN_DETECTOR", signal_source="salesforce",
            metric_value=1.0, threshold=0.5,
            raw_evidence={"count": 1},
        )
        result = build_evidence(dr, opp())
        assert result == []

    def test_r1_violation_returns_empty(self):
        """Snippet with no digits → R1 violation → empty list returned."""
        from backend.discovery.evidence_builder import _validate_evidence
        with pytest.raises(ValueError, match="measurable number"):
            _validate_evidence(
                "ev_sf_abc123", "01 Jan 2026, 09:00", "Salesforce", "Metric",
                "Elevated reassignment rate", "No numbers in this snippet",
                "MEDIUM"
            )

    def test_r6_violation_bad_prefix(self):
        from backend.discovery.evidence_builder import _validate_evidence
        with pytest.raises(ValueError, match="required format"):
            _validate_evidence(
                "ev_salesforce_abc123", "01 Jan 2026, 09:00", "Salesforce",
                "Metric", "Title with 123", "Snippet with 42 records", "MEDIUM"
            )

    def test_r2_violation_invalid_source(self):
        from backend.discovery.evidence_builder import _validate_evidence
        with pytest.raises(ValueError, match="not a valid source"):
            _validate_evidence(
                "ev_sf_abc123", "01 Jan 2026, 09:00", "AgentIQ",
                "Metric", "Title with 123", "Snippet with 42 records", "MEDIUM"
            )
