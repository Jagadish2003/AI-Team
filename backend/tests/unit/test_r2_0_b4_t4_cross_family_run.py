"""2.0-B4 T4 (AT-813) — cross-family run proof (AC3 + AC5).

AC3: a detector written only against normalised concepts runs across at least three
     different source families without modification.
AC5: unmappable concepts are recorded as declared gaps, visible to pack authors —
     never silently approximated.

The proof drives ONE concept-only detector (`detect_open_work_item_backlog`) with
`WorkItem` concepts produced by T2's REAL registered mappers — ServiceNow (itsm), Jira
(engineering_tracker) and Salesforce (crm), three source families — over T2's own
golden sample records. The same detector, unchanged, produces a backlog finding per
family. AC5 rides on T2's real mappers: Jira assigns to an individual (an `actor_group`
gap) and a person-owned Salesforce case has no queue, so those work items carry no
group and the finding reports them as ungrouped rather than inventing one.

This deliberately uses T2's `discovery/concepts/mappers/` package (the sole
connector→concept layer) rather than a bespoke mapper: the detector is what T4 adds,
and it must run on the concepts the platform actually produces.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Importing the package registers every submodule's @maps mappers.
import discovery.concepts.mappers  # noqa: F401
from discovery.concepts.concept_detectors import (
    DETECTOR_ID,
    detect_open_work_item_backlog,
)
from discovery.concepts.conformance import declared_gaps
from discovery.concepts.mappers import get_mapper
from discovery.concepts.model import WorkItem

ORG = "acme"
SAMPLES_PATH = (
    Path(__file__).resolve().parents[2]
    / "discovery" / "tests" / "fixtures" / "concept_mapping_samples.json"
)

# The work-item sample records T2 ships per family. Each family is a different dialect;
# the SAME detector reads the concepts they map to. (Custom-status samples are omitted:
# T2's mappers correctly RAISE on an unmapped status, which is that layer's concern.)
WORK_ITEM_SAMPLES = {
    "servicenow": ["incident_open", "incident_cancelled"],
    "jira": ["issue_in_progress", "issue_done", "issue_wont_do"],
    "salesforce": ["case_queue_owned", "case_person_owned", "case_closed"],
}
FAMILIES = tuple(WORK_ITEM_SAMPLES)  # 3 source families — AC3 needs ≥3

# What each family's OPEN backlog looks like once mapped by T2's mappers (verified):
#   servicenow: 1 open, grouped ('Level 2 Payments')
#   jira:       1 open, UNGROUPED (actor_group gap — assignee is an individual)
#   salesforce: 2 open — 1 grouped ('Payments Queue') + 1 UNGROUPED (person-owned case)
EXPECTED = {
    "servicenow": {"open": 1, "ungrouped": 0, "groups": {"Level 2 Payments": 1}},
    "jira": {"open": 1, "ungrouped": 1, "groups": {}},
    "salesforce": {"open": 2, "ungrouped": 1, "groups": {"Payments Queue": 1}},
}


@pytest.fixture(scope="module")
def samples():
    return json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))


def _map_family(samples, family):
    """Map a family's work-item sample records via T2's registered work_item mapper."""
    fn = get_mapper(family, "work_item").fn
    return [fn(ORG, samples[family][key]) for key in WORK_ITEM_SAMPLES[family]]


@pytest.fixture(scope="module")
def per_family(samples):
    return {family: _map_family(samples, family) for family in FAMILIES}


@pytest.fixture(scope="module")
def mixed(per_family):
    return [wi for items in per_family.values() for wi in items]


# ── AC3 — one detector, ≥3 families, unmodified ─────────────────────────────────

class TestAC3RunsAcrossFamilies:

    def test_runs_across_at_least_three_families_in_one_call(self, mixed):
        results = detect_open_work_item_backlog(mixed, min_open=1)
        families = {r.signal_source for r in results}
        assert families == set(FAMILIES)
        assert len(families) >= 3
        assert all(r.detector_id == DETECTOR_ID for r in results)

    def test_every_family_reports_its_normalised_open_backlog(self, mixed):
        """The detector counts open work items per family purely from the normalised
        `is_open` — whatever dialect they arrived in."""
        by_source = {r.signal_source: r for r in detect_open_work_item_backlog(mixed, min_open=1)}
        for family, exp in EXPECTED.items():
            assert by_source[family].raw_evidence["open_count"] == exp["open"], family

    def test_the_same_detector_unchanged_gives_each_family_its_own_finding(self, per_family):
        """Running the SAME function per-family agrees with the mixed run — no
        cross-family state, no modification between families."""
        mixed = [wi for items in per_family.values() for wi in items]
        mixed_by_source = {r.signal_source: r for r in detect_open_work_item_backlog(mixed, min_open=1)}
        for family, items in per_family.items():
            solo = detect_open_work_item_backlog(items, min_open=1)
            assert len(solo) == 1, family
            assert solo[0].signal_source == family
            assert solo[0].raw_evidence == mixed_by_source[family].raw_evidence

    def test_results_are_deterministic_and_source_ordered(self, mixed):
        first = detect_open_work_item_backlog(mixed, min_open=1)
        second = detect_open_work_item_backlog(mixed, min_open=1)
        assert [r.raw_evidence for r in first] == [r.raw_evidence for r in second]
        assert [r.signal_source for r in first] == sorted(FAMILIES)

    def test_below_threshold_no_family_fires(self, mixed):
        assert detect_open_work_item_backlog(mixed, min_open=99) == []

    def test_detector_reads_only_concepts_not_raw_dicts(self, samples):
        raw = [samples["servicenow"]["incident_open"], samples["jira"]["issue_in_progress"]]
        assert detect_open_work_item_backlog(raw, min_open=1) == []

    def test_detector_module_has_no_per_family_branching(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "discovery" / "concepts" / "concept_detectors.py"
        ).read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        body = body.lower()
        for family in FAMILIES:
            assert family not in body, f"detector body references a source family: {family!r}"
        assert "source_system ==" not in body and "source_system==" not in body

    def test_everything_mapped_is_a_work_item_concept(self, mixed):
        assert mixed and all(isinstance(wi, WorkItem) for wi in mixed)


# ── AC5 — gaps recorded, visible, and never approximated ────────────────────────

class TestAC5GapsRecordedAndHonoured:

    def test_the_actor_group_gap_is_recorded_and_visible(self):
        """declared_gaps() is the surface pack authors read — Jira's actor_group gap is
        there with a reason."""
        jira_gaps = {c.concept: c for c in declared_gaps().get("jira", ())}
        assert "actor_group" in jira_gaps
        assert jira_gaps["actor_group"].reason.strip()

    def test_gap_family_work_items_carry_no_group(self, per_family):
        """AC5 core: a family with an actor_group gap never gets a group invented for it.
        Every Jira work item maps with assigned_group None."""
        assert per_family["jira"], "no jira work items mapped"
        assert all(wi.assigned_group is None for wi in per_family["jira"])

    def test_person_owned_salesforce_case_is_not_given_a_group(self, per_family):
        """T2's Salesforce mapper leaves assigned_group None for a person-owned case —
        it does not synthesise a group from the user's name."""
        person_owned = [
            wi for wi in per_family["salesforce"]
            if wi.is_open and wi.assigned_group is None
        ]
        assert person_owned, "expected a person-owned (ungrouped) open Salesforce case"

    def test_the_gap_is_visible_in_the_detector_output_not_approximated(self, mixed):
        """The finding reports gap-family open items as ungrouped with an empty group
        breakdown — a visible fact, never smoothed into an invented group."""
        by_source = {r.signal_source: r for r in detect_open_work_item_backlog(mixed, min_open=1)}
        assert by_source["jira"].raw_evidence["groups"] == {}
        assert by_source["jira"].raw_evidence["ungrouped_count"] == EXPECTED["jira"]["open"]
        assert by_source["salesforce"].raw_evidence["groups"] == EXPECTED["salesforce"]["groups"]
        assert by_source["salesforce"].raw_evidence["ungrouped_count"] == EXPECTED["salesforce"]["ungrouped"]
        assert by_source["servicenow"].raw_evidence["ungrouped_count"] == 0

    def test_every_recorded_gap_carries_a_reason(self):
        for family, positions in declared_gaps().items():
            for pos in positions:
                assert pos.reason.strip(), f"{family}.{pos.concept}: gap has no reason"

    def test_cancelled_is_not_counted_as_open_backlog(self, samples):
        """An abandoned item (ServiceNow cancelled, Jira 'Won't Do') normalises to
        cancelled and is excluded from the open backlog — never counted as work."""
        sn_cancelled = get_mapper("servicenow", "work_item").fn(
            ORG, samples["servicenow"]["incident_cancelled"]
        )
        jira_wont_do = get_mapper("jira", "work_item").fn(
            ORG, samples["jira"]["issue_wont_do"]
        )
        assert sn_cancelled.status_category == "cancelled" and sn_cancelled.is_open is False
        assert jira_wont_do.status_category == "cancelled" and jira_wont_do.is_open is False
