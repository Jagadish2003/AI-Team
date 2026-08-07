"""2.0-B2 T5 — the re-evaluation work list, the parts that need no database.

The store's interesting properties (upsert semantics, what a re-flag may and may
not change, clearing by the run that re-observed the finding) are SQL and live in
``tests/contract/test_entity_unmerge_contract.py``. Pinned here: the validation
that stops a useless flag being written, and the read shape a surface renders.
"""
from __future__ import annotations

import pytest

from app import finding_reevaluation as fr


def test_a_flag_must_be_scoped_to_an_org():
    with pytest.raises(fr.ReevaluationFlagError):
        fr.flag_findings("", ["identity-1"], reason=fr.REASON_ENTITY_UNMERGED)


def test_a_flag_must_record_why_re_evaluation_is_needed():
    """A flag with no reason is a work item nobody can action — and months later
    nobody can explain either."""
    with pytest.raises(fr.ReevaluationFlagError):
        fr.flag_findings("org_a", ["identity-1"], reason="")


def test_flagging_nothing_writes_nothing_and_reports_it():
    """Called with an empty identity set (the common case: an unmerge whose entity
    no finding referenced), it must be a clean no-op rather than an error."""
    report = fr.flag_findings("org_a", [], reason=fr.REASON_ENTITY_UNMERGED)
    assert report.total == 0
    assert report.to_dict() == {
        "flagged": 0, "refreshed": 0, "total": 0, "identities": []
    }


def test_blank_identities_are_dropped_before_any_write():
    report = fr.flag_findings("org_a", ["", "   ", None], reason=fr.REASON_ENTITY_UNMERGED)
    assert report.total == 0


def test_the_report_separates_new_flags_from_refreshed_ones():
    """One number would hide whether a second unmerge hit findings already waiting."""
    report = fr.FlagReport(flagged=2, refreshed=3, identities=("a", "b"))
    assert (report.total, report.to_dict()["refreshed"]) == (5, 3)


def test_clearing_without_an_org_or_identities_is_a_no_op():
    assert fr.clear_flags_for_run("", "run-1", ["identity-1"]) == []
    assert fr.clear_flags_for_run("org_a", "run-1", []) == []


def test_the_flag_read_shape_names_the_run_that_cleared_it():
    """"Re-evaluated" has to be a fact about a specific run, not a status word."""
    flag = fr.ReevaluationFlag(
        org_id="org_a",
        opportunity_identity="identity-1",
        status=fr.STATUS_CLEARED,
        reason=fr.REASON_ENTITY_UNMERGED,
        trigger_kind=fr.TRIGGER_ENTITY_UNMERGE,
        trigger_ref="unm_1",
        entity_ids=("e1", "e2"),
        flagged_run_id="run-1",
        flagged_by="analyst-1",
        cleared_run_id="run-2",
    )
    payload = flag.to_dict()
    assert payload["clearedRunId"] == "run-2"
    assert payload["flaggedRunId"] == "run-1"
    assert payload["entityIds"] == ["e1", "e2"]
    assert flag.is_pending is False


def test_a_pending_flag_reads_as_pending():
    flag = fr.ReevaluationFlag(
        org_id="org_a", opportunity_identity="i", status=fr.STATUS_PENDING,
        reason=fr.REASON_ENTITY_UNMERGED, trigger_kind=fr.TRIGGER_ENTITY_UNMERGE,
    )
    assert flag.is_pending is True
    assert flag.to_dict()["clearedRunId"] is None


@pytest.mark.parametrize(
    "stored,expected",
    [
        ('["a", "b"]', ["a", "b"]),
        (["a", "b"], ["a", "b"]),
        ("not json", []),
        ('{"a": 1}', []),
        (None, []),
        ('["a", "", null]', ["a"]),
    ],
)
def test_a_corrupt_entity_id_payload_degrades_rather_than_raising(stored, expected):
    """A flag is a work item; unreadable metadata on it must not break the queue."""
    assert fr._loads_list(stored) == expected
