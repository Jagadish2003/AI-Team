"""Unit tests for T4 — parse_causal_output() and is_generic_falsifiability().

Covers:
  - Absent cause_chain → no_falsifiability rejection
  - Absent falsifiability_condition → no_falsifiability rejection
  - Empty falsifiability_condition → no_falsifiability rejection
  - All-empty steps after filtering → empty_cause_chain rejection
  - >5 steps truncated with warning, not rejected
  - Generic falsifiability condition → generic_falsifiability rejection
  - Hallucination guard removes unverifiable names; < 2 steps → hallucination_in_cause_chain
  - All checks pass → clean dict returned
  - causal.hypothesis_rejected telemetry fired with correct reason
  - Non-dict llm_response → no_falsifiability rejection
  - Unexpected exception in inner logic → no_falsifiability rejection, no propagation
"""
from __future__ import annotations

import pytest

from app.causal_engine import (
    CausalContext,
    EntityNode,
    GraphNeighbourhood,
    is_generic_falsifiability,
    parse_causal_output,
    _extract_proper_noun_tokens,
    _apply_hallucination_guard,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_ORG = "test_org"
_RUN = "run-001"
_OPP = "opp-001"

_GOOD_FC = (
    "If covenant review completion rate does not improve within 90 days "
    "when loan origination volume returns to the 90-day baseline, "
    "the capacity hypothesis is incorrect."
)

_GOOD_STEPS = [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "Commercial Credit team capacity was not scaled: two analysts own 7 of 11 overdue reviews [OBSERVED].",
    "Covenant review queue backed up as loans awaited Credit Review clearance [OBSERVED: avg 23 days].",
]


def _make_context(display_names: list[str] | None = None) -> CausalContext:
    """Build a minimal CausalContext with specified entity display names."""
    names = display_names or ["Sarah Chen", "Marcus Webb", "Commercial Credit"]
    entities = [
        EntityNode(
            entity_id=f"e{i}",
            entity_type="process",
            display_name=name,
            resolution_status="resolved",
            org_id=_ORG,
        )
        for i, name in enumerate(names)
    ]
    nb = GraphNeighbourhood(entities=entities, edges=[])
    return CausalContext(graph_context=nb, dependency_paths=[], temporal_support={})


def _parse(response: dict, *, ctx=None) -> dict | None:
    return parse_causal_output(
        response,
        org_id=_ORG,
        run_id=_RUN,
        opportunity_id=_OPP,
        causal_context=ctx,
    )


def _capture_rejection(monkeypatch) -> list[dict]:
    """Monkeypatch record_event and return a list of captured payloads."""
    captured: list[dict] = []

    def fake_record_event(event_type: str, payload: dict | None = None) -> None:
        if event_type == "causal.hypothesis_rejected":
            captured.append(payload or {})

    monkeypatch.setattr("app.causal_engine.record_event", fake_record_event, raising=False)

    # Also patch via the telemetry import path used inside _reject()
    try:
        import app.telemetry as tel
        monkeypatch.setattr(tel, "record_event", fake_record_event)
    except Exception:
        pass

    return captured


# ─────────────────────────────────────────────────────────────────────────────
# is_generic_falsifiability tests
# ─────────────────────────────────────────────────────────────────────────────

def test_generic_too_short():
    assert is_generic_falsifiability("If wrong.") is True


def test_generic_known_phrase():
    assert is_generic_falsifiability("If this is wrong then the hypothesis fails.") is True


def test_generic_future_data_phrase():
    assert is_generic_falsifiability("If future data contradicts this hypothesis then it is incorrect.") is True


def test_generic_no_measurable_qualifier():
    # Long enough, no generic phrase, but no numbers / metrics / time period
    assert is_generic_falsifiability(
        "If the situation does not improve after we review the analysis carefully."
    ) is True


def test_not_generic_has_percentage():
    assert is_generic_falsifiability(
        "If covenant review rate does not improve by 30% after the pilot, the hypothesis is wrong."
    ) is False


def test_not_generic_has_day_period():
    assert is_generic_falsifiability(
        "If SLA breach rate does not decline within 90 days of backlog reduction, the hypothesis fails."
    ) is False


def test_not_generic_has_number():
    assert is_generic_falsifiability(
        "If completion time does not drop below 7 reviews per analyst per week, the hypothesis is wrong."
    ) is False


def test_not_generic_has_entity_name_in_context():
    """A condition naming a real entity is non-generic even without a number."""
    ctx = _make_context(["Sarah Chen"])
    # No number but names Sarah Chen — escape hatch
    assert is_generic_falsifiability(
        "If Sarah Chen's caseload does not decrease after reassignment, the bottleneck lies elsewhere.",
        ctx,
    ) is False


def test_empty_string_is_generic():
    assert is_generic_falsifiability("") is True


def test_none_like_whitespace_is_generic():
    assert is_generic_falsifiability("   ") is True


# ─────────────────────────────────────────────────────────────────────────────
# _extract_proper_noun_tokens tests
# ─────────────────────────────────────────────────────────────────────────────

def test_extracts_two_word_name():
    assert "Sarah Chen" in _extract_proper_noun_tokens(
        "Sarah Chen owns 4 overdue covenant reviews."
    )


def test_extracts_three_word_name():
    assert "Commercial Credit Team" in _extract_proper_noun_tokens(
        "Commercial Credit Team has the highest backlog."
    )


def test_single_capitalised_word_excluded():
    # "Loan" at start of sentence is a single capitalised word — not extracted
    tokens = _extract_proper_noun_tokens("Loan origination rose 40% above baseline.")
    assert tokens == set()


def test_sentence_opener_excluded():
    tokens = _extract_proper_noun_tokens("The Commercial Credit system is overloaded.")
    # "The" is an opener, "Commercial Credit" should still be extracted if ≥2 words
    assert "Commercial Credit" in tokens


# ─────────────────────────────────────────────────────────────────────────────
# parse_causal_output — rejection branches
# ─────────────────────────────────────────────────────────────────────────────

def test_rejects_absent_cause_chain(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({"falsifiability_condition": _GOOD_FC})
    assert result is None
    assert any(p.get("reason") == "no_falsifiability" for p in captured)


def test_rejects_absent_falsifiability_condition(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({"cause_chain": _GOOD_STEPS})
    assert result is None
    assert any(p.get("reason") == "no_falsifiability" for p in captured)


def test_rejects_empty_falsifiability_condition(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({"cause_chain": _GOOD_STEPS, "falsifiability_condition": "   "})
    assert result is None
    assert any(p.get("reason") == "no_falsifiability" for p in captured)


def test_rejects_empty_list_cause_chain(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({"cause_chain": [], "falsifiability_condition": _GOOD_FC})
    assert result is None
    assert any(p.get("reason") == "no_falsifiability" for p in captured)


def test_rejects_all_empty_steps(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({"cause_chain": ["", "  ", "\t"], "falsifiability_condition": _GOOD_FC})
    assert result is None
    assert any(p.get("reason") == "empty_cause_chain" for p in captured)


def test_rejects_generic_falsifiability(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = _parse({
        "cause_chain": _GOOD_STEPS,
        "falsifiability_condition": "If this is wrong then it is incorrect.",
    })
    assert result is None
    assert any(p.get("reason") == "generic_falsifiability" for p in captured)


def test_rejects_non_dict_response(monkeypatch):
    captured = _capture_rejection(monkeypatch)
    result = parse_causal_output(
        "not a dict",  # type: ignore[arg-type]
        org_id=_ORG, run_id=_RUN, opportunity_id=_OPP, causal_context=None,
    )
    assert result is None
    assert any(p.get("reason") == "no_falsifiability" for p in captured)


def test_rejects_hallucination_below_two_steps(monkeypatch):
    """Guard removes steps with unknown entity names; < 2 remain → rejection."""
    captured = _capture_rejection(monkeypatch)
    ctx = _make_context(["Known Entity"])
    # Both steps mention "Alice Johnson" who is NOT in the context
    result = _parse({
        "cause_chain": [
            "Alice Johnson owns all overdue reviews.",
            "Alice Johnson caused the backlog.",
        ],
        "falsifiability_condition": _GOOD_FC,
    }, ctx=ctx)
    assert result is None
    assert any(p.get("reason") == "hallucination_in_cause_chain" for p in captured)


# ─────────────────────────────────────────────────────────────────────────────
# parse_causal_output — truncation and success paths
# ─────────────────────────────────────────────────────────────────────────────

def test_truncates_over_five_steps_with_warning(monkeypatch, caplog):
    """7 steps truncated to 5 — no rejection, warning logged."""
    import logging
    captured = _capture_rejection(monkeypatch)
    seven_steps = [f"Step {i}: loan origination volume rose 40% above baseline." for i in range(7)]
    with caplog.at_level(logging.WARNING, logger="app.causal_engine"):
        result = _parse({
            "cause_chain": seven_steps,
            "falsifiability_condition": _GOOD_FC,
        })
    assert result is not None, "Should not reject for >5 steps"
    assert len(result["cause_chain"]) == 5
    assert not captured, "No rejection event should be fired for truncation"
    assert any("truncating" in m.lower() for m in caplog.messages)


def test_success_returns_clean_dict():
    """Happy path: all checks pass, clean dict returned."""
    result = _parse({
        "cause_chain": _GOOD_STEPS,
        "falsifiability_condition": _GOOD_FC,
    })
    assert result is not None
    assert "cause_chain" in result
    assert "falsifiability_condition" in result
    assert isinstance(result["cause_chain"], list)
    assert len(result["cause_chain"]) >= 1
    assert result["falsifiability_condition"] == _GOOD_FC


def test_success_filters_empty_steps_within_chain():
    """Empty strings in a mixed list are filtered; valid steps survive."""
    result = _parse({
        "cause_chain": ["", _GOOD_STEPS[0], "  ", _GOOD_STEPS[1]],
        "falsifiability_condition": _GOOD_FC,
    })
    assert result is not None
    for step in result["cause_chain"]:
        assert step.strip() != ""


def test_causal_context_none_skips_hallucination_guard():
    """When causal_context is None the guard is skipped entirely."""
    result = _parse({
        "cause_chain": [
            "Unknown Person ABC caused the delay.",
            "Unknown Person XYZ owns 10 overdue reviews — a 40% increase.",
        ],
        "falsifiability_condition": _GOOD_FC,
    }, ctx=None)
    assert result is not None, "Guard should be skipped when context is None"


def test_rejection_payload_includes_required_fields(monkeypatch):
    """Rejection payloads always carry org_id, run_id, opportunity_id, reason."""
    captured = _capture_rejection(monkeypatch)
    _parse({"cause_chain": _GOOD_STEPS, "falsifiability_condition": "  "})
    assert captured
    payload = captured[0]
    assert payload.get("org_id") == _ORG
    assert payload.get("run_id") == _RUN
    assert payload.get("opportunity_id") == _OPP
    assert "reason" in payload


def test_no_exception_escapes_on_bad_input(monkeypatch):
    """No exception should escape parse_causal_output regardless of input."""
    _capture_rejection(monkeypatch)
    # Deliberately weird inputs
    for bad in [None, 42, [], object()]:
        result = parse_causal_output(
            bad,  # type: ignore[arg-type]
            org_id=_ORG, run_id=_RUN, opportunity_id=_OPP, causal_context=None,
        )
        assert result is None  # should degrade gracefully


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry registration
# ─────────────────────────────────────────────────────────────────────────────

def test_causal_events_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES
    assert "causal.hypothesis_rejected" in REGISTERED_EVENT_TYPES
    assert "causal.hypothesis_generated" in REGISTERED_EVENT_TYPES
