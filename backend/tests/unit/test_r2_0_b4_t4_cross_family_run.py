"""2.0-B4 T4 (AT-813) — cross-family run proof (AC3 + AC5).

AC3: a concept-only detector runs across ≥3 source families without modification.
AC5: unmappable concepts are recorded as declared gaps, visible to pack authors —
     never silently approximated.

The proof feeds ONE detector (`detect_open_work_item_backlog`) `WorkItem`s mapped from
four dialects — ServiceNow (itsm), Jira (engineering_tracker), Salesforce (crm) and
GitHub (code) — and shows the same function produces a finding per family, with an
IDENTICAL open count (dialect-blind). AC5 shows through the same run: Jira and GitHub
declare an `actor_group` gap, so their work items carry no group and the finding reports
them as ungrouped rather than inventing one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from discovery.concepts.concept_detectors import (
    DEFAULT_MIN_OPEN,
    DETECTOR_ID,
    detect_open_work_item_backlog,
)
from discovery.concepts.conformance import declared_gaps, get_conformance
from discovery.concepts.mappers import (
    map_github_issues,
    map_jira_work_items,
    map_salesforce_cases,
    map_servicenow_work_items,
)
from discovery.concepts.model import WorkItem

ORG = "acme"

# Four dialects, each carrying the SAME logical set: 4 open + 1 not-open. If the
# concept model works, the detector sees "4 open" for every one, whatever the words.
SERVICENOW = [
    {"sys_id": "s1", "number": "INC001", "state": "New", "sys_class_name": "incident", "assignment_group": "Network Ops", "opened_at": "2026-01-01"},
    {"sys_id": "s2", "number": "INC002", "state": "In Progress", "sys_class_name": "incident", "assignment_group": "Network Ops", "opened_at": "2026-01-02"},
    {"sys_id": "s3", "number": "INC003", "state": "On Hold", "sys_class_name": "incident", "assignment_group": "Network Ops", "opened_at": "2026-01-03"},
    {"sys_id": "s4", "number": "INC004", "state": "In Progress", "sys_class_name": "incident", "assignment_group": "Network Ops", "opened_at": "2026-01-04"},
    {"sys_id": "s5", "number": "INC005", "state": "Closed", "sys_class_name": "incident", "assignment_group": "Network Ops", "opened_at": "2026-01-05"},
]
JIRA = [
    {"key": "PAY-1", "fields": {"status": {"name": "To Do"}, "issuetype": {"name": "Bug"}, "assignee": {"displayName": "Alice"}, "created": "2026-01-01"}},
    {"key": "PAY-2", "fields": {"status": {"name": "In Progress"}, "issuetype": {"name": "Story"}, "assignee": {"displayName": "Bob"}, "created": "2026-01-02"}},
    {"key": "PAY-3", "fields": {"status": {"name": "Blocked"}, "issuetype": {"name": "Bug"}, "assignee": {"displayName": "Alice"}, "created": "2026-01-03"}},
    {"key": "PAY-4", "fields": {"status": {"name": "In Progress"}, "issuetype": {"name": "Task"}, "assignee": {"displayName": "Carol"}, "created": "2026-01-04"}},
    {"key": "PAY-5", "fields": {"status": {"name": "Done"}, "issuetype": {"name": "Bug"}, "assignee": {"displayName": "Bob"}, "created": "2026-01-05"}},
]
SALESFORCE = [
    {"Id": "500a", "CaseNumber": "00001", "Status": "New", "Type": "Problem", "OwnerGroup": "Tier 2 Support", "CreatedDate": "2026-01-01"},
    {"Id": "500b", "CaseNumber": "00002", "Status": "Working", "Type": "Problem", "OwnerGroup": "Tier 2 Support", "CreatedDate": "2026-01-02"},
    {"Id": "500c", "CaseNumber": "00003", "Status": "On Hold", "Type": "Question", "OwnerGroup": "Tier 2 Support", "CreatedDate": "2026-01-03"},
    {"Id": "500d", "CaseNumber": "00004", "Status": "Working", "Type": "Problem", "OwnerGroup": "Tier 2 Support", "CreatedDate": "2026-01-04"},
    {"Id": "500e", "CaseNumber": "00005", "Status": "Closed", "Type": "Problem", "OwnerGroup": "Tier 2 Support", "CreatedDate": "2026-01-05"},
]
GITHUB = [
    {"number": 1, "state": "open", "assignees": [{"login": "dev1"}], "created_at": "2026-01-01"},
    {"number": 2, "state": "open", "assignees": [{"login": "dev2"}], "created_at": "2026-01-02"},
    {"number": 3, "state": "open", "assignees": [{"login": "dev1"}], "created_at": "2026-01-03"},
    {"number": 4, "state": "open", "assignees": [{"login": "dev3"}], "created_at": "2026-01-04"},
    {"number": 5, "state": "closed", "state_reason": "completed", "assignees": [{"login": "dev2"}], "created_at": "2026-01-05"},
]

FAMILIES = {
    "servicenow": lambda: map_servicenow_work_items(SERVICENOW, org_id=ORG),
    "jira": lambda: map_jira_work_items(JIRA, org_id=ORG),
    "salesforce": lambda: map_salesforce_cases(SALESFORCE, org_id=ORG),
    "github": lambda: map_github_issues(GITHUB, org_id=ORG),
}
GROUP_SUPPORTED = {"servicenow", "salesforce"}   # actor_group declared
GROUP_GAP = {"jira", "github"}                    # actor_group gap


@pytest.fixture(scope="module")
def per_family():
    return {name: build() for name, build in FAMILIES.items()}


@pytest.fixture(scope="module")
def mixed(per_family):
    return [wi for items in per_family.values() for wi in items]


# ── AC3 — one detector, ≥3 families, unmodified ─────────────────────────────────

class TestAC3RunsAcrossFamilies:

    def test_runs_across_at_least_three_families_in_one_call(self, mixed):
        results = detect_open_work_item_backlog(mixed)
        families = {r.signal_source for r in results}
        assert families == set(FAMILIES), families
        assert len(families) >= 3
        assert all(r.detector_id == DETECTOR_ID for r in results)

    def test_the_open_count_is_dialect_blind(self, mixed):
        """The whole point: four different dialects, one normalised count. Each family
        expressed the same 4-open/1-closed set; the detector sees 4 for every one."""
        results = detect_open_work_item_backlog(mixed)
        counts = {r.signal_source: r.raw_evidence["open_count"] for r in results}
        assert set(counts.values()) == {4}, counts

    def test_the_same_detector_unchanged_gives_each_family_its_own_finding(self, per_family, mixed):
        """Running the SAME function per-family and on the mixed stream agree — the
        detector carries no cross-family state and is not modified between families."""
        mixed_by_source = {r.signal_source: r for r in detect_open_work_item_backlog(mixed)}
        for family, items in per_family.items():
            solo = detect_open_work_item_backlog(items)
            assert len(solo) == 1, family
            assert solo[0].raw_evidence == mixed_by_source[family].raw_evidence
            assert solo[0].signal_source == family

    def test_results_are_deterministic_and_ordered(self, mixed):
        first = detect_open_work_item_backlog(mixed)
        second = detect_open_work_item_backlog(mixed)
        assert [r.raw_evidence for r in first] == [r.raw_evidence for r in second]
        assert [r.signal_source for r in first] == sorted(FAMILIES)

    def test_below_threshold_a_family_does_not_fire(self, per_family):
        """Negative control: raise the bar above the backlog and every family goes quiet
        — the detector really is thresholding the normalised count, not always firing."""
        results = detect_open_work_item_backlog(per_family["jira"], min_open=99)
        assert results == []

    def test_detector_reads_only_concepts_not_raw_dicts(self):
        """Handed the raw connector dicts, the detector emits nothing — it responds only
        to WorkItem concept instances."""
        assert detect_open_work_item_backlog(SERVICENOW) == []
        assert detect_open_work_item_backlog(JIRA + GITHUB) == []

    def test_detector_module_has_no_per_family_branching(self):
        """A detector 'written only against concepts' must not name a source family in
        its executable body."""
        src = (
            Path(__file__).resolve().parents[2]
            / "discovery" / "concepts" / "concept_detectors.py"
        ).read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        body = body.lower()
        for family in FAMILIES:
            assert family not in body, f"detector body references a source family: {family!r}"
        assert "source_system ==" not in body and "source_system==" not in body

    def test_every_mapped_signal_is_a_work_item_concept(self, mixed):
        assert mixed and all(isinstance(wi, WorkItem) for wi in mixed)


# ── AC5 — gaps recorded, visible, and never approximated ────────────────────────

class TestAC5GapsRecordedAndHonoured:

    def test_the_actor_group_gap_is_recorded_and_visible(self):
        """declared_gaps() is the surface pack authors read — the Jira/GitHub
        actor_group gaps are there, each with a reason."""
        gaps = declared_gaps()
        for family in GROUP_GAP:
            concepts_in_gap = {c.concept: c for c in gaps.get(family, ())}
            assert "actor_group" in concepts_in_gap, f"{family}: actor_group gap not recorded"
            assert concepts_in_gap["actor_group"].reason.strip(), f"{family}: gap has no reason"

    def test_gap_families_never_synthesise_a_group(self, per_family):
        """AC5's core: a family that assigns to individuals must NOT get a group invented
        for it. Every Jira/GitHub work item has assigned_group None."""
        for family in GROUP_GAP:
            assert all(wi.assigned_group is None for wi in per_family[family]), family

    def test_group_families_do_carry_a_group_reference(self, per_family):
        for family in GROUP_SUPPORTED:
            groups = {wi.assigned_group.display_name for wi in per_family[family]}
            assert groups and None not in groups, family

    def test_the_gap_is_visible_in_the_detector_output_not_approximated(self, mixed):
        """The finding for a gap family reports its open items as ungrouped with an empty
        group breakdown; a supported family reports them under their group. The gap shows
        as a fact in the output, never smoothed over."""
        by_source = {r.signal_source: r for r in detect_open_work_item_backlog(mixed)}
        for family in GROUP_GAP:
            ev = by_source[family].raw_evidence
            assert ev["groups"] == {}, family
            assert ev["ungrouped_count"] == ev["open_count"], family
        for family in GROUP_SUPPORTED:
            ev = by_source[family].raw_evidence
            assert ev["groups"] and ev["ungrouped_count"] == 0, family

    def test_gap_and_not_applicable_reasons_exist_registry_wide(self):
        """Every recorded gap across every connector carries a reason — a gap with no
        reason is indistinguishable from an oversight."""
        for family, positions in declared_gaps().items():
            for pos in positions:
                assert pos.reason.strip(), f"{family}.{pos.concept}: gap has no reason"

    def test_native_status_is_preserved_for_traceback(self, per_family):
        """Normalising status must not destroy the source's own value (it is what a
        gap/appeal would be checked against)."""
        for family, items in per_family.items():
            assert all(wi.native_status for wi in items), family

    def test_cancelled_is_not_treated_as_closed(self):
        """A GitHub issue closed as 'not planned' normalises to cancelled, not closed —
        so it is not counted as completed work anywhere downstream."""
        wi = map_github_issues(
            [{"number": 9, "state": "closed", "state_reason": "not_planned", "assignees": []}],
            org_id=ORG,
        )[0]
        assert wi.status_category == "cancelled"
        assert wi.is_open is False
