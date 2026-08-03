"""2.0-A2 T1 — the lifecycle state machine's rules.

Pure unit tests: the state machine takes its clock as an argument precisely so
these can run without a DB and without freezing time.

The load-bearing rule under test is the **non-inference rule**: the platform never
infers that an agent was deployed. If the platform could talk itself into an
``actioned`` state, T7 ("no outcome without action") becomes decorative — so
several tests here exist to prove that no path reaches ``actioned`` without a
human and an explicit date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.opportunity_lifecycle_states import (
    ACTOR_HUMAN,
    ACTOR_SYSTEM,
    ALL_STATES,
    HUMAN_REACHABLE_STATES,
    INITIAL_STATE,
    MEASURABLE_STATES,
    STATE_ACTIONED,
    STATE_DISMISSED,
    STATE_MEASURED,
    STATE_MONITORING,
    STATE_OPEN,
    STATE_STALLED,
    SYSTEM_REACHABLE_STATES,
    TRANSITIONS,
    LifecycleTransitionError,
    get_transition,
    is_measurable,
    legal_transitions_from,
    lifecycle_state_summary,
    parse_action_date,
    validate_transition,
)

TODAY = date(2026, 7, 29)


# --------------------------------------------------------------------------
# The non-inference rule — the constraint the whole story rests on.
# --------------------------------------------------------------------------


class TestNonInferenceRule:
    def test_actioned_is_not_system_reachable(self):
        """Enforced as DATA, not as a convention.

        A background caller physically cannot land on ``actioned`` because no
        system transition declares it as a target.
        """
        assert STATE_ACTIONED not in SYSTEM_REACHABLE_STATES
        assert STATE_ACTIONED in HUMAN_REACHABLE_STATES

    def test_no_transition_into_actioned_has_a_system_actor(self):
        into_actioned = [t for t in TRANSITIONS if t.to_state == STATE_ACTIONED]
        assert into_actioned, "there must be a way to record an action"
        for t in into_actioned:
            assert t.actor == ACTOR_HUMAN, (
                f"{t.from_state} -> actioned is declared for a {t.actor} actor — "
                "the platform must never infer that a change was deployed"
            )

    def test_every_transition_into_actioned_requires_a_date(self):
        for t in TRANSITIONS:
            if t.to_state == STATE_ACTIONED:
                assert t.requires_action_date, (
                    "recording an action without a date would fabricate the "
                    "before/after boundary measurement is computed from"
                )

    def test_a_system_actor_is_refused_with_a_named_reason(self):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(
                STATE_OPEN, STATE_ACTIONED, ACTOR_SYSTEM, action_date=TODAY, now=TODAY
            )
        message = str(excinfo.value)
        assert "system" in message
        assert "never infers" in message, (
            "the refusal must say WHY, not just that it is illegal"
        )

    def test_actioned_cannot_be_reached_without_a_date(self):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(STATE_OPEN, STATE_ACTIONED, ACTOR_HUMAN, now=TODAY)
        assert "action date is required" in str(excinfo.value)

    def test_no_state_is_measurable_without_an_action_having_been_recorded(self):
        """T7's gate: ``open`` and ``dismissed`` are never measurable."""
        assert not is_measurable(STATE_OPEN)
        assert not is_measurable(STATE_DISMISSED)
        for state in (STATE_ACTIONED, STATE_MONITORING, STATE_MEASURED, STATE_STALLED):
            assert is_measurable(state), state

    def test_measurable_states_are_exactly_the_post_action_states(self):
        assert MEASURABLE_STATES == {
            STATE_ACTIONED,
            STATE_MONITORING,
            STATE_MEASURED,
            STATE_STALLED,
        }


# --------------------------------------------------------------------------
# Action-date validation.
# --------------------------------------------------------------------------


class TestActionDate:
    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_missing_date_is_an_error_never_a_default(self, missing):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            parse_action_date(missing, now=TODAY)
        assert "required" in str(excinfo.value)

    def test_future_date_is_refused(self):
        tomorrow = TODAY + timedelta(days=1)
        with pytest.raises(LifecycleTransitionError) as excinfo:
            parse_action_date(tomorrow.isoformat(), now=TODAY)
        assert "future" in str(excinfo.value)

    def test_today_is_accepted(self):
        assert parse_action_date(TODAY.isoformat(), now=TODAY) == TODAY

    def test_past_date_is_accepted(self):
        past = TODAY - timedelta(days=45)
        assert parse_action_date(past.isoformat(), now=TODAY) == past

    @pytest.mark.parametrize(
        "bad", ["not-a-date", "29/07/2026", "2026-13-01", "yesterday", "2026-07"]
    )
    def test_unparseable_date_is_refused_with_the_expected_format(self, bad):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            parse_action_date(bad, now=TODAY)
        assert "YYYY-MM-DD" in str(excinfo.value)

    def test_iso_datetime_is_accepted_and_reduced_to_its_date(self):
        assert parse_action_date("2026-07-01T13:45:00Z", now=TODAY) == date(2026, 7, 1)

    def test_date_and_datetime_objects_are_accepted(self):
        assert parse_action_date(date(2026, 7, 1), now=TODAY) == date(2026, 7, 1)
        assert parse_action_date(
            datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc), now=TODAY
        ) == date(2026, 7, 1)

    def test_a_date_is_refused_on_a_transition_that_does_not_take_one(self):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(
                STATE_ACTIONED,
                STATE_MONITORING,
                ACTOR_SYSTEM,
                action_date=TODAY,
                now=TODAY,
            )
        assert "does not take an action date" in str(excinfo.value)


# --------------------------------------------------------------------------
# Legality.
# --------------------------------------------------------------------------


class TestLegalTransitions:
    def test_the_documented_happy_path_is_legal(self):
        assert validate_transition(
            STATE_OPEN, STATE_ACTIONED, ACTOR_HUMAN, action_date=TODAY, now=TODAY
        ).to_state == STATE_ACTIONED
        assert validate_transition(
            STATE_ACTIONED, STATE_MONITORING, ACTOR_SYSTEM
        ).to_state == STATE_MONITORING
        assert validate_transition(
            STATE_MONITORING, STATE_MEASURED, ACTOR_SYSTEM
        ).to_state == STATE_MEASURED

    def test_monitoring_is_the_systems_own_move(self):
        assert get_transition(STATE_ACTIONED, STATE_MONITORING, ACTOR_SYSTEM) is not None
        assert get_transition(STATE_ACTIONED, STATE_MONITORING, ACTOR_HUMAN) is None

    @pytest.mark.parametrize(
        "from_state",
        [STATE_OPEN, STATE_ACTIONED, STATE_MONITORING, STATE_MEASURED, STATE_STALLED],
    )
    def test_dismissed_is_analyst_driven_from_any_state(self, from_state):
        assert (
            validate_transition(from_state, STATE_DISMISSED, ACTOR_HUMAN).to_state
            == STATE_DISMISSED
        )

    @pytest.mark.parametrize(
        "from_state", [STATE_ACTIONED, STATE_MONITORING, STATE_STALLED]
    )
    def test_stalled_is_reachable_only_for_actioned_work(self, from_state):
        """An opportunity that was never actioned cannot be stalled.

        ``stalled`` means "actioned, but not measurable" — applying it to an
        untouched finding would misrepresent it in the portfolio.
        """
        if from_state == STATE_STALLED:
            return
        assert get_transition(from_state, STATE_STALLED, ACTOR_SYSTEM) is not None
        assert get_transition(STATE_OPEN, STATE_STALLED, ACTOR_SYSTEM) is None

    @pytest.mark.parametrize(
        "from_state, to_state, actor",
        [
            (STATE_OPEN, STATE_MONITORING, ACTOR_SYSTEM),
            (STATE_OPEN, STATE_MEASURED, ACTOR_SYSTEM),
            (STATE_OPEN, STATE_STALLED, ACTOR_SYSTEM),
            (STATE_DISMISSED, STATE_ACTIONED, ACTOR_HUMAN),
            (STATE_DISMISSED, STATE_MONITORING, ACTOR_SYSTEM),
            (STATE_MEASURED, STATE_ACTIONED, ACTOR_HUMAN),
        ],
    )
    def test_illegal_transitions_are_refused(self, from_state, to_state, actor):
        with pytest.raises(LifecycleTransitionError):
            validate_transition(from_state, to_state, actor, now=TODAY)

    def test_a_refusal_names_the_legal_alternatives(self):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(STATE_OPEN, STATE_MEASURED, ACTOR_SYSTEM, now=TODAY)
        message = str(excinfo.value)
        assert "open" in message and "measured" in message
        assert "Legal system targets" in message

    def test_a_no_op_transition_is_refused(self):
        """History must never record a move that did not happen."""
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(STATE_OPEN, STATE_OPEN, ACTOR_HUMAN, now=TODAY)
        assert "already the current state" in str(excinfo.value)

    @pytest.mark.parametrize(
        "from_state, to_state, actor",
        [("nonsense", STATE_OPEN, ACTOR_HUMAN), (STATE_OPEN, "nonsense", ACTOR_HUMAN),
         (STATE_OPEN, STATE_DISMISSED, "robot")],
    )
    def test_unknown_states_and_actors_are_refused(self, from_state, to_state, actor):
        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(from_state, to_state, actor, now=TODAY)
        assert "unknown" in str(excinfo.value)


# --------------------------------------------------------------------------
# Reversibility.
# --------------------------------------------------------------------------


class TestReversibility:
    @pytest.mark.parametrize(
        "from_state",
        [STATE_ACTIONED, STATE_MONITORING, STATE_MEASURED, STATE_STALLED, STATE_DISMISSED],
    )
    def test_an_analyst_can_unwind_back_to_open(self, from_state):
        """An analyst who actioned the wrong opportunity must be able to undo it."""
        validated = validate_transition(from_state, STATE_OPEN, ACTOR_HUMAN, now=TODAY)
        assert validated.to_state == STATE_OPEN

    @pytest.mark.parametrize(
        "from_state", [STATE_ACTIONED, STATE_MONITORING, STATE_MEASURED, STATE_STALLED]
    )
    def test_the_unwind_clears_the_action_date(self, from_state):
        """The wrong pivot must not survive the unwind."""
        validated = validate_transition(from_state, STATE_OPEN, ACTOR_HUMAN, now=TODAY)
        assert validated.clear_action_date is True
        assert validated.action_date is None

    def test_a_system_actor_cannot_unwind_a_human_record(self):
        with pytest.raises(LifecycleTransitionError):
            validate_transition(STATE_ACTIONED, STATE_OPEN, ACTOR_SYSTEM, now=TODAY)

    def test_reopening_after_action_requires_the_date_again(self):
        """Re-actioning after an unwind is a fresh human act with a fresh date."""
        validate_transition(STATE_ACTIONED, STATE_OPEN, ACTOR_HUMAN, now=TODAY)
        with pytest.raises(LifecycleTransitionError):
            validate_transition(STATE_OPEN, STATE_ACTIONED, ACTOR_HUMAN, now=TODAY)


# --------------------------------------------------------------------------
# Table integrity — the machine is data, so its shape is testable.
# --------------------------------------------------------------------------


class TestTableIntegrity:
    def test_the_initial_state_is_open(self):
        assert INITIAL_STATE == STATE_OPEN

    def test_every_transition_references_known_states_and_actors(self):
        for t in TRANSITIONS:
            assert t.from_state in ALL_STATES, t
            assert t.to_state in ALL_STATES, t
            assert t.actor in (ACTOR_HUMAN, ACTOR_SYSTEM), t

    def test_no_duplicate_transition_rows(self):
        keys = [t.key for t in TRANSITIONS]
        assert len(keys) == len(set(keys)), "a duplicated row makes legality ambiguous"

    def test_every_transition_carries_a_reason(self):
        """A refusal or a history row without a reason is not auditable."""
        for t in TRANSITIONS:
            assert t.reason.strip(), t

    def test_no_transition_is_a_self_loop(self):
        for t in TRANSITIONS:
            assert t.from_state != t.to_state, t

    def test_every_state_except_the_initial_one_is_reachable(self):
        reachable = {t.to_state for t in TRANSITIONS}
        for state in ALL_STATES:
            if state == INITIAL_STATE:
                continue
            assert state in reachable, f"{state} is declared but unreachable"

    def test_only_the_action_transition_requires_a_date(self):
        for t in TRANSITIONS:
            if t.requires_action_date:
                assert t.to_state == STATE_ACTIONED, t

    def test_only_human_transitions_clear_the_action_date(self):
        for t in TRANSITIONS:
            if t.clears_action_date:
                assert t.actor == ACTOR_HUMAN, t
                assert t.to_state == STATE_OPEN, t

    def test_legal_transitions_from_is_consistent_with_the_table(self):
        for state in ALL_STATES:
            expected = {t for t in TRANSITIONS if t.from_state == state}
            assert set(legal_transitions_from(state)) == expected

    def test_dismissed_only_leads_back_to_open(self):
        """Parked, not a black hole: reopening is the one way out."""
        out = {t.to_state for t in legal_transitions_from(STATE_DISMISSED)}
        assert out == {STATE_OPEN}

    def test_summary_is_json_shaped_and_complete(self):
        summary = lifecycle_state_summary()
        assert summary["initialState"] == STATE_OPEN
        assert set(summary["states"]) == set(ALL_STATES)
        assert len(summary["transitions"]) == len(TRANSITIONS)
        assert STATE_ACTIONED not in summary["systemReachableStates"]
