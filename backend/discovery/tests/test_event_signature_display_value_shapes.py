"""Regression: event-signature extraction across every LIVE ServiceNow shape.

The bug this pins was invisible to the whole existing suite. ServiceNow incident
queries run with ``sysparm_display_value=all``, so a multi-value column arrives
wrapped as ``{"value": ..., "display_value": ...}`` — but the offline fixtures
store plain scalars. ``extract_event_signatures`` tested only the list branch
via ``isinstance(raw, (list, tuple))``, which a Mapping fails, so on every LIVE
run each multi-value signature was silently dropped while every test passed.

Silent is the operative word: a dropped signature does not raise, it just means
an event↔incident recurrence never links, so the finding is emitted unlinked and
looks legitimately unlinked. These cases therefore assert the LIVE wire shapes
directly rather than going through a fixture.
"""
from __future__ import annotations

import pytest

from discovery.detectors.ops_recurrence_joins import extract_event_signatures
from discovery.ingest.servicenow import INCIDENT_EVENT_SIGNATURE_FIELDS

#: The real column name, taken from the ingestor so a rename cannot leave this
#: test asserting against a field nothing populates.
FIELD = INCIDENT_EVENT_SIGNATURE_FIELDS[0]

SIG_A = "1:" + "a" * 32
SIG_B = "1:" + "b" * 32


@pytest.mark.parametrize(
    "raw,expected",
    [
        # --- shapes that already worked (offline fixtures) --------------------
        pytest.param([SIG_A, SIG_B], {SIG_A, SIG_B}, id="plain-list"),
        pytest.param(SIG_A, {SIG_A}, id="plain-scalar"),
        # --- the display_value=all shapes that silently dropped --------------
        pytest.param(
            {"value": SIG_A, "display_value": SIG_A}, {SIG_A}, id="wrapped-scalar"
        ),
        pytest.param(
            {"value": f"{SIG_A},{SIG_B}", "display_value": f"{SIG_A}, {SIG_B}"},
            {SIG_A, SIG_B},
            id="wrapped-multi-value-csv",
        ),
        pytest.param(
            {"value": [SIG_A, SIG_B], "display_value": [SIG_A, SIG_B]},
            {SIG_A, SIG_B},
            id="wrapped-list",
        ),
        pytest.param(f"{SIG_A},{SIG_B}", {SIG_A, SIG_B}, id="bare-csv"),
    ],
)
def test_every_live_shape_yields_its_signatures(raw, expected):
    assert set(extract_event_signatures({FIELD: raw})) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="null"),
        pytest.param({"value": ""}, id="wrapped-empty"),
        pytest.param({"value": None, "display_value": None}, id="wrapped-nulls"),
        pytest.param({"value": "not a signature"}, id="wrapped-free-text"),
        pytest.param("INC0012345", id="incident-number"),
        pytest.param({"value": "1:tooshort"}, id="malformed-hex"),
        pytest.param({"value": "zz:" + "a" * 32}, id="non-numeric-version"),
    ],
)
def test_nothing_that_is_not_a_signature_is_accepted(raw):
    """Conservative-by-construction survives the unwrapping.

    Widening the shapes we READ must not widen what we ACCEPT — free text, an
    incident number and a malformed hash are still rejected, so an arbitrary
    field value can never be mistaken for a deterministic event link.
    """
    assert extract_event_signatures({FIELD: raw}) == ()


def test_mixed_wrapped_value_keeps_the_valid_signatures_only():
    """A malformed fragment drops itself, not its well-formed siblings."""
    raw = {"value": f"{SIG_A},garbage,{SIG_B}"}
    assert set(extract_event_signatures({FIELD: raw})) == {SIG_A, SIG_B}


def test_result_is_deduplicated_and_sorted():
    raw = {"value": f"{SIG_B},{SIG_A},{SIG_B}"}
    assert extract_event_signatures({FIELD: raw}) == (SIG_A, SIG_B)


def test_display_value_is_used_only_when_value_is_absent():
    """``value`` is the canonical stored form; ``display_value`` renders for humans.

    When both are present the raw value wins. When ServiceNow supplies only a
    display value we still read it rather than dropping the link.
    """
    assert set(extract_event_signatures({FIELD: {"display_value": SIG_A}})) == {SIG_A}
    both = {"value": SIG_A, "display_value": SIG_B}
    assert set(extract_event_signatures({FIELD: both})) == {SIG_A}
