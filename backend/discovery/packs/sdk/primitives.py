"""Detector primitive vocabulary and parameter contracts — 2.0-C3 T1 (AT-836).

Scope note — read this first
---------------------------
This module declares the **vocabulary** a pack manifest composes detectors from:
the stable primitive ids, each primitive's parameter contract, its concept arity,
and the evidence/corroboration semantics a manifest author inherits rather than
re-implements. It is what makes ``detectors`` in a manifest checkable at all —
"composed primitives with parameters" is meaningless without a contract to
validate the parameters against.

It deliberately contains **no detector implementation**. Binding each primitive id
to executable platform detector machinery is the separate primitive-library task
(2.0-C3 §2); when that lands it implements against these ids and contracts rather
than declaring a second, drifting set. One vocabulary, two readers — the same
discipline ``certification_criteria.py`` applies to the review checklist.

Why parameters are contracts, not free-form JSON
------------------------------------------------
2.0-C3's governing constraint is that a partner pack is declarative configuration,
not code. The only way a *declarative* detector stays honest is if every knob it
can turn is enumerated, typed, and bounded here: an unknown parameter is a typo
that would otherwise silently do nothing, and an unbounded one (``max_depth:
40``) is an unbounded traversal shipped into a customer deployment under the guise
of configuration. Bounds are part of the security posture, not ergonomics.

Dependency-free (no ``app`` import, no I/O), matching the rest of
``discovery/packs``, so authoring tooling can read it offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Version of the primitive library's *contract surface*. A manifest may declare
#: the version it was authored against; adding a primitive or an optional
#: parameter is a minor bump, changing or removing one is a major bump.
PRIMITIVE_LIBRARY_VERSION = "1.0.0"

# ── Parameter kinds ───────────────────────────────────────────────────────────

KIND_INTEGER = "integer"
KIND_NUMBER = "number"
KIND_BOOLEAN = "boolean"
KIND_ENUM = "enum"

PARAMETER_KINDS = (KIND_INTEGER, KIND_NUMBER, KIND_BOOLEAN, KIND_ENUM)


@dataclass(frozen=True)
class ParameterSpec:
    """One parameter of one primitive.

    ``name``        the key an author writes under ``parameters``.
    ``kind``        one of :data:`PARAMETER_KINDS`.
    ``required``    when True the manifest must supply it — there is no implicit
                    default, because a silently-defaulted firing threshold is a
                    detector nobody reviewed.
    ``default``     value applied when an optional parameter is omitted.
    ``minimum`` /   inclusive bounds for numeric kinds. Present on every numeric
    ``maximum``     parameter that could otherwise express an unbounded scan.
    ``choices``     the closed value set for ``enum``.
    """

    name: str
    kind: str
    description: str
    required: bool = False
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "required": self.required,
        }
        if self.default is not None:
            out["default"] = self.default
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.choices:
            out["choices"] = list(self.choices)
        return out


@dataclass(frozen=True)
class PrimitiveSpec:
    """One composable detector primitive.

    ``concept_arity`` is ``(minimum, maximum)`` normalised concepts the detector
    must bind — ``maximum`` ``None`` means "any number". Co-occurrence needs two
    by definition; a recurrence over two unrelated concepts is not a recurrence.
    """

    primitive_id: str
    label: str
    description: str
    parameters: Tuple[ParameterSpec, ...]
    evidence_semantics: str
    corroboration_semantics: str
    concept_arity: Tuple[int, Optional[int]] = (1, 1)

    @property
    def parameter_names(self) -> List[str]:
        return [spec.name for spec in self.parameters]

    def parameter(self, name: str) -> Optional[ParameterSpec]:
        for spec in self.parameters:
            if spec.name == name:
                return spec
        return None

    def to_dict(self) -> Dict[str, Any]:
        minimum, maximum = self.concept_arity
        return {
            "primitiveId": self.primitive_id,
            "label": self.label,
            "description": self.description,
            "parameters": [spec.to_dict() for spec in self.parameters],
            "evidenceSemantics": self.evidence_semantics,
            "corroborationSemantics": self.corroboration_semantics,
            "conceptArity": {"minimum": minimum, "maximum": maximum},
        }


def _p(*specs: ParameterSpec) -> Tuple[ParameterSpec, ...]:
    return tuple(specs)


# Shared evidence wording. Every primitive emits the four-part contract — that is
# the point of composing from primitives rather than writing a detector: the
# author inherits evidence, confidence, corroboration status, and source trace.
_FOUR_PART = (
    "Emits the four-part finding contract: the contributing records as evidence, "
    "a confidence level derived from source count and agreement, an explicit "
    "corroboration status, and a source trace to every originating record."
)


#: The documented, versioned primitive set a manifest composes detectors from.
PRIMITIVE_LIBRARY: Dict[str, PrimitiveSpec] = {}


def _register(spec: PrimitiveSpec) -> PrimitiveSpec:
    PRIMITIVE_LIBRARY[spec.primitive_id] = spec
    return spec


_register(
    PrimitiveSpec(
        primitive_id="recurrence",
        label="Recurrence",
        description=(
            "The same normalised fact recurring above a count within a window — "
            "the shape behind 'this is handled manually again and again'."
        ),
        parameters=_p(
            ParameterSpec(
                "min_occurrences",
                KIND_INTEGER,
                "Occurrences within the window before the detector fires.",
                required=True,
                minimum=2,
                maximum=10_000,
            ),
            ParameterSpec(
                "window_days",
                KIND_INTEGER,
                "Rolling observation window, in days.",
                required=True,
                minimum=1,
                maximum=365,
            ),
            ParameterSpec(
                "group_by",
                KIND_ENUM,
                "What counts as 'the same' occurrence.",
                default="signature",
                choices=("signature", "artifact", "actor_group", "entity_reference"),
            ),
            ParameterSpec(
                "min_distinct_actor_groups",
                KIND_INTEGER,
                "Require the recurrence to span at least this many actor groups.",
                minimum=1,
                maximum=50,
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "Single-source recurrence is capped at MEDIUM; agreement from a second "
            "source system elevates to HIGH."
        ),
    )
)

_register(
    PrimitiveSpec(
        primitive_id="threshold_vs_baseline",
        label="Threshold vs baseline",
        description=(
            "A measured quantity departing from its own observed baseline by more "
            "than a proportion — never an absolute number picked by hand."
        ),
        parameters=_p(
            ParameterSpec(
                "metric",
                KIND_ENUM,
                "The normalised measure compared against its baseline.",
                required=True,
                choices=(
                    "volume",
                    "age_days",
                    "time_to_resolve_minutes",
                    "reassignment_hops",
                    "backlog_depth",
                ),
            ),
            ParameterSpec(
                "departure_pct",
                KIND_NUMBER,
                "Fractional departure from baseline before firing (0.25 = 25%).",
                required=True,
                minimum=0.01,
                maximum=10.0,
            ),
            ParameterSpec(
                "direction",
                KIND_ENUM,
                "Which direction of departure is a finding.",
                default="above",
                choices=("above", "below", "either"),
            ),
            ParameterSpec(
                "min_baseline_runs",
                KIND_INTEGER,
                "Prior runs required before a baseline is trusted.",
                default=3,
                minimum=1,
                maximum=100,
            ),
            ParameterSpec(
                "window_days",
                KIND_INTEGER,
                "Comparison window, in days.",
                default=30,
                minimum=1,
                maximum=365,
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "A departure observed in one source stays MEDIUM until a second source "
            "agrees within the correlation window."
        ),
    )
)

_register(
    PrimitiveSpec(
        primitive_id="ageing",
        label="Ageing",
        description=(
            "Work items sitting in a state longer than a threshold — queue ageing, "
            "stalled approvals, deferral drift."
        ),
        parameters=_p(
            ParameterSpec(
                "min_age_days",
                KIND_INTEGER,
                "Age threshold, in days, before an item counts as aged.",
                required=True,
                minimum=1,
                maximum=3650,
            ),
            ParameterSpec(
                "min_items",
                KIND_INTEGER,
                "Aged items required before the detector fires (an aggregation "
                "floor — one aged item is a record, not a finding).",
                default=3,
                minimum=1,
                maximum=10_000,
            ),
            ParameterSpec(
                "age_from",
                KIND_ENUM,
                "Which timestamp the age is measured from.",
                default="opened_at",
                choices=("opened_at", "last_state_change_at", "due_at"),
            ),
            ParameterSpec(
                "state_scope",
                KIND_ENUM,
                "Which items are in scope for ageing.",
                default="open",
                choices=("open", "unresolved", "any"),
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "Ageing is observed directly from records, so corroboration status "
            "reports the source systems that agree on the aged population."
        ),
    )
)

_register(
    PrimitiveSpec(
        primitive_id="oscillation",
        label="Oscillation",
        description=(
            "Repeated back-and-forth transitions — reassignment ping-pong between "
            "groups, state flapping, ownership churn."
        ),
        parameters=_p(
            ParameterSpec(
                "min_hops",
                KIND_INTEGER,
                "Transitions on one item before it counts as oscillating.",
                required=True,
                minimum=2,
                maximum=100,
            ),
            ParameterSpec(
                "transition_kind",
                KIND_ENUM,
                "Which transition is counted.",
                default="assignment",
                choices=("assignment", "state", "ownership"),
            ),
            ParameterSpec(
                "window_days",
                KIND_INTEGER,
                "Observation window, in days.",
                default=30,
                minimum=1,
                maximum=365,
            ),
            ParameterSpec(
                "min_distinct_participants",
                KIND_INTEGER,
                "Distinct actor groups the oscillation must span.",
                default=2,
                minimum=2,
                maximum=50,
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "Oscillation is described at group level only; participants are actor "
            "groups and queues, never individuals."
        ),
    )
)

_register(
    PrimitiveSpec(
        primitive_id="concentration_traversal",
        label="Concentration / traversal (depth-bounded)",
        description=(
            "Work concentrating on a shared entity reached by bounded traversal of "
            "the entity graph — stated as concentration, never as causation."
        ),
        parameters=_p(
            ParameterSpec(
                "max_depth",
                KIND_INTEGER,
                "Traversal depth bound. Hard-capped: an unbounded traversal is a "
                "full graph walk, not a detector.",
                required=True,
                minimum=1,
                maximum=3,
            ),
            ParameterSpec(
                "min_dependents",
                KIND_INTEGER,
                "Distinct dependents that must concentrate on the anchor.",
                required=True,
                minimum=2,
                maximum=1_000,
            ),
            ParameterSpec(
                "anchor",
                KIND_ENUM,
                "What the concentration is measured against.",
                default="entity_reference",
                choices=("entity_reference", "artifact", "actor_group"),
            ),
            ParameterSpec(
                "window_days",
                KIND_INTEGER,
                "Observation window, in days.",
                default=30,
                minimum=1,
                maximum=365,
            ),
            ParameterSpec(
                "require_corroboration",
                KIND_BOOLEAN,
                "Require a second source to agree before emitting.",
                default=False,
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "Wording is concentration-shaped ('work concentrates on...'). The "
            "primitive never asserts causation — causality is the causal engine's."
        ),
        concept_arity=(1, None),
    )
)

_register(
    PrimitiveSpec(
        primitive_id="co_occurrence_window",
        label="Co-occurrence within window",
        description=(
            "Two normalised concepts occurring together inside a bounded "
            "correlation window — the only honest form of a cross-stream join."
        ),
        parameters=_p(
            ParameterSpec(
                "window_minutes",
                KIND_INTEGER,
                "Correlation window. A join outside it is a coincidence and is "
                "recorded as rejected, never as agreement.",
                required=True,
                minimum=1,
                maximum=10_080,
            ),
            ParameterSpec(
                "min_pairs",
                KIND_INTEGER,
                "Co-occurring pairs required before the detector fires.",
                default=2,
                minimum=1,
                maximum=10_000,
            ),
            ParameterSpec(
                "ordering",
                KIND_ENUM,
                "Whether ordering within the window matters.",
                default="either",
                choices=("either", "first_before_second"),
            ),
        ),
        evidence_semantics=_FOUR_PART,
        corroboration_semantics=(
            "The join type and the window used are recorded on the claim, on "
            "success and on rejection, so a coincidence never inflates confidence."
        ),
        concept_arity=(2, 2),
    )
)


# ── Public API ────────────────────────────────────────────────────────────────


def get_primitive(primitive_id: str) -> Optional[PrimitiveSpec]:
    """The spec for a primitive, or ``None`` if the library has no such id."""
    return PRIMITIVE_LIBRARY.get(str(primitive_id or "").strip())


def is_known_primitive(primitive_id: str) -> bool:
    """True when the id is in the primitive library."""
    return str(primitive_id or "").strip() in PRIMITIVE_LIBRARY


def primitive_ids() -> List[str]:
    """Every primitive id, sorted — the closed set a manifest may reference."""
    return sorted(PRIMITIVE_LIBRARY)


def describe_primitive(primitive_id: str) -> str:
    """Human-readable label for a primitive, used verbatim in refusal messages."""
    spec = get_primitive(primitive_id)
    return f"{primitive_id} ({spec.label})" if spec else str(primitive_id)


def primitive_catalog() -> Dict[str, Any]:
    """JSON-serialisable primitive reference — the authoring toolkit's source."""
    return {
        "primitiveLibraryVersion": PRIMITIVE_LIBRARY_VERSION,
        "primitives": [
            PRIMITIVE_LIBRARY[pid].to_dict() for pid in primitive_ids()
        ],
    }


__all__ = [
    "KIND_BOOLEAN",
    "KIND_ENUM",
    "KIND_INTEGER",
    "KIND_NUMBER",
    "PARAMETER_KINDS",
    "PRIMITIVE_LIBRARY",
    "PRIMITIVE_LIBRARY_VERSION",
    "ParameterSpec",
    "PrimitiveSpec",
    "describe_primitive",
    "get_primitive",
    "is_known_primitive",
    "primitive_catalog",
    "primitive_ids",
]
