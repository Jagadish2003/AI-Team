"""A discovery run must ingest the SELECTED systems, not every connected one.

The Integration Hub and a run's scope are different things. A customer connects
every source they own there — that is the point of it — and then scopes an
individual run in Stack Builder. Before this, ``_run_trackb_and_persist``
replaced the selection outright:

    if live_systems:
        systems = live_systems      # the selection is discarded

so a run scoped to five systems ingested all of them, and the unselected sources'
signals reached detection, scoring and corroboration unasked. The customer had no
way to narrow a run short of disconnecting connectors in the Hub.
"""
from __future__ import annotations

from app.routes_sprint4_t1 import _selected_live_systems


LIVE = [
    "salesforce", "servicenow", "jira", "confluence", "sharepoint",
    "slack", "teams", "github", "aws_events",
]


def test_run_is_narrowed_to_the_selected_systems():
    selected = ["salesforce_sc", "servicenow", "jira", "confluence", "sharepoint"]
    assert _selected_live_systems(selected, LIVE) == [
        "salesforce", "servicenow", "jira", "confluence", "sharepoint",
    ]


def test_connected_but_unselected_systems_are_excluded():
    selected = ["salesforce_sc", "servicenow"]
    result = _selected_live_systems(selected, LIVE)
    for excluded in ("slack", "teams", "github", "aws_events", "confluence", "sharepoint"):
        assert excluded not in result


def test_salesforce_product_ids_map_to_the_salesforce_connector():
    """Stack Builder records per-PRODUCT ids; resolve_live_systems returns CONNECTOR
    ids, because one Salesforce connection serves every Salesforce product. A plain
    set intersection would drop the system of record from every run that selected a
    specific product — a worse failure than the one being fixed."""
    for product in ("salesforce_sc", "salesforce_fsc", "salesforce_ncino"):
        assert _selected_live_systems([product], LIVE) == ["salesforce"]


def test_selecting_the_connector_family_also_matches():
    assert _selected_live_systems(["salesforce"], LIVE) == ["salesforce"]


def test_matching_is_case_and_whitespace_tolerant():
    assert _selected_live_systems([" SalesForce_SC "], LIVE) == ["salesforce"]


def test_no_overlap_returns_empty_so_the_caller_can_fall_back():
    """Not a match, and deliberately not guessed into one. The caller falls back to
    the authenticated set: a run with NO sources would report 'no findings', which
    reads as a clean estate rather than a misconfiguration."""
    assert _selected_live_systems(["sqlserver"], LIVE) == []


def test_empty_selection_returns_empty():
    assert _selected_live_systems([], LIVE) == []
    assert _selected_live_systems(["salesforce"], []) == []


def test_live_order_is_preserved():
    selected = ["sharepoint", "salesforce_sc", "jira"]
    assert _selected_live_systems(selected, LIVE) == ["salesforce", "jira", "sharepoint"]


def test_unrelated_prefix_does_not_match():
    """'jira' must not match 'jira_service_desk_extra' unless that id was selected,
    and a shared word is not a prefix match."""
    assert _selected_live_systems(["jiralite"], ["jira"]) == []
    assert _selected_live_systems(["sales"], ["salesforce"]) == []
