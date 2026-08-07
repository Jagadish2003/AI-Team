"""2.0-A2 T1 — the opportunity lifecycle state machine, expressed as data.

This module answers *"has the customer done something about this finding, and are
we now watching?"* — a different question from the review ``decision`` field
(``APPROVED`` / ``REJECTED`` / ``UNREVIEWED``), which answers *"does the analyst
agree this is real?"*.

**The two axes are orthogonal and must never be collapsed.** An ``APPROVED``
opportunity may sit at ``open`` for months; a ``dismissed`` one was never
actioned at all. Nothing in this module reads or writes ``decision``.

**The non-inference rule — the constraint the whole story rests on.** The
platform NEVER infers that an agent was deployed. There is no heuristic, no
"signals improved so they must have shipped something", and no auto-promotion out
of ``open``. The move to ``actioned`` is always an explicit human act carrying an
explicit date, and that date is the pivot every later measurement hangs from.
Enforced structurally here rather than by convention:

* ``actioned`` is reachable only by an ``ACTOR_HUMAN`` transition;
* that transition declares ``requires_action_date=True``, and
  :func:`validate_transition` refuses it without one — there is no default;
* a future-dated or unparseable action date is a validation error, never coerced;
* :data:`SYSTEM_REACHABLE_STATES` deliberately excludes ``actioned``, so a
  background caller physically cannot land there.

Legality lives in one explicit table (:data:`TRANSITIONS`) rather than scattered
``if`` checks, so the legal set is auditable by reading a list, and an illegal
transition is refused with a NAMED reason rather than a bare False.

Pure: no DB, no ``app`` import beyond typing, and no clock read except the
caller-supplied ``now`` used to reject future dates (injectable, so the rules are
testable without freezing time).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple

#: Bumped when the state set or the legal-transition table changes shape.
LIFECYCLE_SCHEMA_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------

#: The finding is surfaced and nothing has been recorded against it.
STATE_OPEN = "open"
#: A human recorded that an agent/change was deployed, WITH a date.
STATE_ACTIONED = "actioned"
#: A post-action run has landed; the signals are being re-measured.
STATE_MONITORING = "monitoring"
#: A comparable window has been covered and a measurement exists.
STATE_MEASURED = "measured"
#: Analyst-driven: this finding is not being pursued.
STATE_DISMISSED = "dismissed"
#: Actioned, but subsequent runs cannot produce a comparable measurement
#: (source disconnected, pack disabled, insufficient runs). It exists so the
#: portfolio view never has to choose between lying and hiding.
STATE_STALLED = "stalled"

ALL_STATES: Tuple[str, ...] = (
    STATE_OPEN,
    STATE_ACTIONED,
    STATE_MONITORING,
    STATE_MEASURED,
    STATE_DISMISSED,
    STATE_STALLED,
)

#: The state every newly-tracked opportunity starts in.
INITIAL_STATE = STATE_OPEN

#: States that mean "a human recorded an action, so measurement is permitted".
#: This is the set 2.0-A2 T7 ("no outcome without action") gates on — an
#: opportunity outside it must never receive an outcome measurement, however
#: much its signals move.
MEASURABLE_STATES: FrozenSet[str] = frozenset(
    {STATE_ACTIONED, STATE_MONITORING, STATE_MEASURED, STATE_STALLED}
)

#: Parked/terminal states — no system move originates from ``dismissed``.
TERMINAL_STATES: FrozenSet[str] = frozenset({STATE_DISMISSED})

# --------------------------------------------------------------------------
# Actors
# --------------------------------------------------------------------------

#: A person, via the authenticated API. The only actor that may record an action.
ACTOR_HUMAN = "human"
#: The platform itself, as runs land. May advance monitoring/measurement, and may
#: NEVER decide that something was deployed.
ACTOR_SYSTEM = "system"

ALL_ACTORS: Tuple[str, ...] = (ACTOR_HUMAN, ACTOR_SYSTEM)


# --------------------------------------------------------------------------
# The legal-transition table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """One legal move, and everything true about it.

    ``reason`` is the plain-language description used in history and in the
    refusal message for an illegal move, so a caller never receives a bare
    "invalid transition" with no explanation of what was expected.
    """

    from_state: str
    to_state: str
    actor: str
    #: Only the action-recording transition sets this.
    requires_action_date: bool = False
    #: True when the move undoes a human record and must clear the action date.
    clears_action_date: bool = False
    reason: str = ""

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.from_state, self.to_state, self.actor)


#: EVERY legal transition. Adding a move means adding a row here — there is no
#: other place legality is decided.
TRANSITIONS: Tuple[Transition, ...] = (
    # ---- Recording an action: the one human move that starts measurement ----
    Transition(
        STATE_OPEN,
        STATE_ACTIONED,
        ACTOR_HUMAN,
        requires_action_date=True,
        reason="an analyst recorded that a change was deployed, with its date",
    ),
    # ---- System progression once an action exists ----
    Transition(
        STATE_ACTIONED,
        STATE_MONITORING,
        ACTOR_SYSTEM,
        reason="a run landed after the recorded action date, so re-measurement began",
    ),
    Transition(
        STATE_MONITORING,
        STATE_MEASURED,
        ACTOR_SYSTEM,
        reason="a comparable window has been covered, so a measurement exists",
    ),
    Transition(
        STATE_MEASURED,
        STATE_MONITORING,
        ACTOR_SYSTEM,
        reason="a later run extended the series, so monitoring continues",
    ),
    # ---- Honest degradation ----
    Transition(
        STATE_ACTIONED,
        STATE_STALLED,
        ACTOR_SYSTEM,
        reason="actioned, but no comparable measurement can be produced",
    ),
    Transition(
        STATE_MONITORING,
        STATE_STALLED,
        ACTOR_SYSTEM,
        reason="monitoring, but no comparable measurement can be produced",
    ),
    Transition(
        STATE_STALLED,
        STATE_MONITORING,
        ACTOR_SYSTEM,
        reason="comparability was restored, so monitoring resumed",
    ),
    # ---- Analyst dismissal, from any non-dismissed state ----
    Transition(
        STATE_OPEN, STATE_DISMISSED, ACTOR_HUMAN, reason="an analyst dismissed the finding"
    ),
    Transition(
        STATE_ACTIONED,
        STATE_DISMISSED,
        ACTOR_HUMAN,
        reason="an analyst dismissed the finding",
    ),
    Transition(
        STATE_MONITORING,
        STATE_DISMISSED,
        ACTOR_HUMAN,
        reason="an analyst dismissed the finding",
    ),
    Transition(
        STATE_MEASURED,
        STATE_DISMISSED,
        ACTOR_HUMAN,
        reason="an analyst dismissed the finding",
    ),
    Transition(
        STATE_STALLED,
        STATE_DISMISSED,
        ACTOR_HUMAN,
        reason="an analyst dismissed the finding",
    ),
    # ---- Reversibility: an analyst who actioned the wrong opportunity must be
    # able to unwind it. The unwind is a forward transition recorded in history,
    # never a silent rewrite of the row that made the mistake.
    Transition(
        STATE_ACTIONED,
        STATE_OPEN,
        ACTOR_HUMAN,
        clears_action_date=True,
        reason="an analyst unwound the recorded action",
    ),
    Transition(
        STATE_MONITORING,
        STATE_OPEN,
        ACTOR_HUMAN,
        clears_action_date=True,
        reason="an analyst unwound the recorded action",
    ),
    Transition(
        STATE_MEASURED,
        STATE_OPEN,
        ACTOR_HUMAN,
        clears_action_date=True,
        reason="an analyst unwound the recorded action",
    ),
    Transition(
        STATE_STALLED,
        STATE_OPEN,
        ACTOR_HUMAN,
        clears_action_date=True,
        reason="an analyst unwound the recorded action",
    ),
    Transition(
        STATE_DISMISSED,
        STATE_OPEN,
        ACTOR_HUMAN,
        clears_action_date=True,
        reason="an analyst reopened a dismissed finding",
    ),
)

_TRANSITION_INDEX: Dict[Tuple[str, str, str], Transition] = {
    t.key: t for t in TRANSITIONS
}

#: Every state a SYSTEM actor can move something INTO. ``actioned`` is absent by
#: construction — that is the non-inference rule, enforced as data.
SYSTEM_REACHABLE_STATES: FrozenSet[str] = frozenset(
    t.to_state for t in TRANSITIONS if t.actor == ACTOR_SYSTEM
)

#: Every state a HUMAN actor can move something into.
HUMAN_REACHABLE_STATES: FrozenSet[str] = frozenset(
    t.to_state for t in TRANSITIONS if t.actor == ACTOR_HUMAN
)


class LifecycleTransitionError(ValueError):
    """An illegal or invalid transition, carrying a named reason.

    A caller always learns WHY: which move was attempted, and what the rule is.
    """


def get_transition(
    from_state: str, to_state: str, actor: str
) -> Optional[Transition]:
    """The declared transition, or ``None`` when the move is not legal."""
    return _TRANSITION_INDEX.get((from_state, to_state, actor))


def legal_transitions_from(
    from_state: str, actor: Optional[str] = None
) -> Tuple[Transition, ...]:
    """Every legal move out of ``from_state``, optionally for one actor only.

    Used to render "what can I do next" without duplicating the table.
    """
    return tuple(
        t
        for t in TRANSITIONS
        if t.from_state == from_state and (actor is None or t.actor == actor)
    )


def is_measurable(state: str) -> bool:
    """True when a measurement is permitted for this state (T7's gate).

    An opportunity that has never been actioned is not measurable, however much
    its signals move — so this is the one predicate T3/T7 should consult rather
    than testing states inline.
    """
    return state in MEASURABLE_STATES


# --------------------------------------------------------------------------
# Action-date validation
# --------------------------------------------------------------------------


def parse_action_date(value: object, *, now: Optional[date] = None) -> date:
    """Validate and parse a human-supplied action date.

    Rules, all of them errors rather than defaults:

    * missing/blank → error. The date is never defaulted to "today", because a
      defaulted pivot silently fabricates the before/after boundary every later
      measurement is computed from.
    * unparseable → error.
    * in the future → error. An action cannot have been taken tomorrow, and a
      future pivot would make every subsequent run look "post-action".

    Accepts an ISO date (``2026-07-28``) or an ISO datetime, from which the date
    is taken. ``now`` is injectable so the future check is testable.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise LifecycleTransitionError(
            "action date is required: recording an action without its date would "
            "fabricate the before/after boundary that every later measurement "
            "is computed from"
        )

    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError as exc:
                raise LifecycleTransitionError(
                    f"action date {text!r} is not a valid ISO date (expected "
                    "YYYY-MM-DD)"
                ) from exc

    today = now or datetime.now(timezone.utc).date()
    if parsed > today:
        raise LifecycleTransitionError(
            f"action date {parsed.isoformat()} is in the future: an action "
            "cannot have been taken after today"
        )
    return parsed


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedTransition:
    """A transition that passed every rule, with its resolved side effects."""

    transition: Transition
    #: The parsed action date to store; ``None`` when the move does not set one.
    action_date: Optional[date] = None
    #: True when the move must clear any stored action date.
    clear_action_date: bool = False

    @property
    def to_state(self) -> str:
        return self.transition.to_state

    @property
    def reason(self) -> str:
        return self.transition.reason


def validate_transition(
    from_state: str,
    to_state: str,
    actor: str,
    *,
    action_date: object = None,
    now: Optional[date] = None,
) -> ValidatedTransition:
    """Validate one move, or raise :class:`LifecycleTransitionError` with a reason.

    The single gate every transition — human or system — passes through. Callers
    never decide legality themselves.
    """
    if from_state not in ALL_STATES:
        raise LifecycleTransitionError(f"unknown current state {from_state!r}")
    if to_state not in ALL_STATES:
        raise LifecycleTransitionError(f"unknown target state {to_state!r}")
    if actor not in ALL_ACTORS:
        raise LifecycleTransitionError(f"unknown actor {actor!r}")

    if from_state == to_state:
        raise LifecycleTransitionError(
            f"{from_state!r} is already the current state; a no-op transition is "
            "refused so history never records a move that did not happen"
        )

    transition = get_transition(from_state, to_state, actor)
    if transition is None:
        # Named refusal: say what was attempted and what is actually available.
        available = sorted(
            {t.to_state for t in legal_transitions_from(from_state, actor)}
        )
        by_other_actor = sorted(
            {
                t.to_state
                for t in legal_transitions_from(from_state)
                if t.actor != actor and t.to_state == to_state
            }
        )
        detail = (
            f"a {actor} actor cannot move {from_state!r} -> {to_state!r}. "
            f"Legal {actor} targets from {from_state!r}: "
            f"{', '.join(available) if available else 'none'}"
        )
        if by_other_actor:
            other = ACTOR_SYSTEM if actor == ACTOR_HUMAN else ACTOR_HUMAN
            detail += (
                f". {to_state!r} is reachable only by a {other} actor"
                + (
                    " — the platform never infers that a change was deployed"
                    if to_state == STATE_ACTIONED
                    else ""
                )
            )
        raise LifecycleTransitionError(detail)

    parsed_date: Optional[date] = None
    if transition.requires_action_date:
        parsed_date = parse_action_date(action_date, now=now)
    elif action_date not in (None, ""):
        raise LifecycleTransitionError(
            f"{from_state!r} -> {to_state!r} does not take an action date; "
            "only recording an action does"
        )

    return ValidatedTransition(
        transition=transition,
        action_date=parsed_date,
        clear_action_date=transition.clears_action_date,
    )


def lifecycle_state_summary() -> Dict[str, object]:
    """JSON-serialisable description of the machine — the audit surface.

    Lets a reviewer (or a UI) see the whole legal set without reading Python.
    """
    return {
        "schemaVersion": LIFECYCLE_SCHEMA_VERSION,
        "states": list(ALL_STATES),
        "initialState": INITIAL_STATE,
        "measurableStates": sorted(MEASURABLE_STATES),
        "terminalStates": sorted(TERMINAL_STATES),
        "systemReachableStates": sorted(SYSTEM_REACHABLE_STATES),
        "humanReachableStates": sorted(HUMAN_REACHABLE_STATES),
        "transitions": [
            {
                "from": t.from_state,
                "to": t.to_state,
                "actor": t.actor,
                "requiresActionDate": t.requires_action_date,
                "clearsActionDate": t.clears_action_date,
                "reason": t.reason,
            }
            for t in TRANSITIONS
        ],
    }


__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "STATE_OPEN",
    "STATE_ACTIONED",
    "STATE_MONITORING",
    "STATE_MEASURED",
    "STATE_DISMISSED",
    "STATE_STALLED",
    "ALL_STATES",
    "INITIAL_STATE",
    "MEASURABLE_STATES",
    "TERMINAL_STATES",
    "ACTOR_HUMAN",
    "ACTOR_SYSTEM",
    "ALL_ACTORS",
    "SYSTEM_REACHABLE_STATES",
    "HUMAN_REACHABLE_STATES",
    "TRANSITIONS",
    "Transition",
    "ValidatedTransition",
    "LifecycleTransitionError",
    "get_transition",
    "legal_transitions_from",
    "is_measurable",
    "lifecycle_state_summary",
    "parse_action_date",
    "validate_transition",
]
