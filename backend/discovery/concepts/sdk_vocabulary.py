"""2.0-B4 T6 — the published concept vocabulary: what 2.0-C3's partners build against.

B4's closing deliverable. T1 defined the concept set and its versioned contracts, T2
mapped the connectors and recorded the gaps, T3 proved a port preserves behaviour, T4
proved a concept-only detector crosses source families, T5 locked per-connector
conformance behind a CI gate. This module publishes the finished result as ONE
versioned artifact — the vocabulary a partner pack is authored against.

Why a separate publication rather than "read the registry"
---------------------------------------------------------
The registry is an INTERNAL structure. It is keyed for our convenience, it records our
implementation, and it changes shape when we refactor. A partner-facing vocabulary has
different obligations, and three of them decide this module's design:

**1. It must not hand out implementation.** 2.0-C3's governing constraint is that
partner packs are *declarative configuration, not arbitrary code* — no partner-supplied
code executes in a customer deployment. Publishing ``discovery.concepts.mappers.
servicenow:map_incident_work_item`` to a partner invites exactly the thing that
constraint forbids: importing our internals and calling them. So the published
vocabulary carries **capability, never a module path**. A partner learns that
ServiceNow supplies ``work_item`` and which fields will be empty; they never learn
which function does it. ``/api/concepts/conformance`` still exposes the mapper name,
because that surface answers an INTERNAL reviewer's question ("what code backs this
claim?") — the two audiences are different and are served differently on purpose.

**2. It must be pinnable.** A pack authored in March against seven concepts must be
able to say what it was authored against, so C1's compatibility declaration can refuse
it on a platform whose vocabulary has moved and C2's certification can record what was
reviewed. :func:`vocabulary_digest` is a deterministic hash over the published content
(sorted, no clock, no environment) — the same vocabulary always digests the same, and
any change a partner could observe changes it.

**3. It must be honest about availability.** Only ``supported`` concepts are published
as usable, and every field-level gap travels with them. A vocabulary that advertised
``declared`` concepts would send a partner to write a detector that runs, finds
nothing, and reports the emptiness as an answer — which is the failure AC5 exists to
prevent, arriving through the front door instead of the back.

What is deliberately NOT here
-----------------------------
No detector primitives. The composable primitive library (recurrence, ageing,
threshold-vs-baseline, concentration) is 2.0-C3's own deliverable; B4 supplies the
NOUNS a primitive operates on, and inventing a half-primitive here would give C3 an
API it has to break. :data:`SDK_HANDOFF` records that boundary explicitly rather than
leaving C3 to guess what it inherited.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple

try:
    from discovery.concepts import conformance as conf
    from discovery.concepts import model as m
    from discovery.concepts.contracts import (
        BREAKING_CHANGE_RULES, CONCEPT_SET_VERSION, CONTRACTS, get_contract,
    )
    from discovery.concepts.gaps import concept_gap_report
except ModuleNotFoundError:  # pragma: no cover - project-root execution
    from backend.discovery.concepts import conformance as conf
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.contracts import (
        BREAKING_CHANGE_RULES, CONCEPT_SET_VERSION, CONTRACTS, get_contract,
    )
    from backend.discovery.concepts.gaps import concept_gap_report


#: Version of the PUBLISHED VOCABULARY — the artifact's own shape, distinct from
#: ``CONCEPT_SET_VERSION`` (what is in the set) and from each contract's version (what a
#: concept requires). Three numbers because they break different things for a partner:
#: the set gaining a concept adds vocabulary, a contract gaining a required field
#: invalidates a mapping, and this changing means the DOCUMENT a partner reads has a
#: new shape and their tooling may need updating.
VOCABULARY_VERSION = 1

#: The closed vocabularies a partner may use as literal values in a declarative pack
#: manifest. Published with their VALUES, because a manifest naming a token outside
#: these sets must fail validation at authoring time rather than at run time — and a
#: partner cannot honour a closed set they cannot read.
PUBLISHED_VOCABULARIES: Tuple[str, ...] = (
    "WORK_ITEM_TYPES",
    "STATUS_CATEGORIES",
    "PRIORITY_LEVELS",
    "ACTOR_GROUP_TYPES",
    "ARTIFACT_TYPES",
    "CONTENT_TYPES",
    "TRANSITION_TYPES",
    "APPROVAL_DECISIONS",
    "APPROVAL_TYPES",
    "ASSIGNMENT_TYPES",
    "ENTITY_REFERENCE_TYPES",
)

#: What B4 hands 2.0-C3, and what it explicitly does not. Recorded here so the SDK
#: story inherits a stated boundary instead of an assumption.
SDK_HANDOFF: Dict[str, Tuple[str, ...]] = {
    "provided_by_b4": (
        "the normalised concept set and its closed vocabularies (the nouns a detector "
        "reads)",
        "a versioned mapping contract per concept, including the rules a field "
        "constraint cannot express",
        "per-connector availability, so a manifest can declare which concepts it "
        "requires and be refused when a customer's estate cannot supply them",
        "declared gaps at concept AND field level, so a partner knows what will be "
        "empty before they write the detector",
        "a deterministic digest a pack can pin, for C1 compatibility and C2 "
        "certification scope",
    ),
    "not_provided_by_b4": (
        "the detector primitive library (recurrence, ageing, threshold-vs-baseline, "
        "concentration, co-occurrence) — 2.0-C3's own deliverable",
        "the pack manifest schema and its validator — 2.0-C3",
        "packaging, signing and installation — 2.0-C3 with C1/C2",
        "any executable extension point: partner packs are declarative configuration, "
        "and B4 deliberately publishes no callable a partner could ship code against",
    ),
}

#: The stability promise, in the artifact, because a promise kept only in a design doc
#: is not a promise a partner can rely on.
STABILITY_CONTRACT: Tuple[str, ...] = (
    "A concept, once published, is not removed without a CONCEPT_SET_VERSION bump that "
    "states what replaces it.",
    "A value is never removed from a published closed vocabulary without bumping that "
    "concept's contract version — a pack may be emitting it.",
    "A field never becomes REQUIRED without bumping that concept's contract version; "
    "adding an optional field does not bump.",
    "Availability may IMPROVE without any bump (a connector gaining a mapper is "
    "additive). Availability being WITHDRAWN is a breaking change for any pack that "
    "required it, and is reported through the digest changing.",
    "A declared gap may be closed at any time; a gap being ADDED to a concept a "
    "connector already supported means that connector's mapping got narrower, and is "
    "the case a pinned digest is meant to catch.",
)


def _vocabulary_values() -> Dict[str, List[str]]:
    """Every published closed vocabulary, resolved to sorted values.

    Resolved off the model rather than restated, so the published tokens cannot drift
    from the ones construction actually validates against — the whole point of T1
    keeping the vocabularies in one place.
    """
    resolved: Dict[str, List[str]] = {}
    for name in PUBLISHED_VOCABULARIES:
        value = getattr(m, name, None)
        if not isinstance(value, frozenset):
            raise KeyError(
                f"{name!r} is published as a partner vocabulary but is not a closed set "
                f"on discovery.concepts.model — the publication and the model have "
                f"drifted, and a partner would be told to use tokens nothing validates"
            )
        resolved[name] = sorted(value)
    return resolved


def _published_field(field: Any) -> Dict[str, Any]:
    """One contract field, as a partner sees it."""
    return {
        "name": field.name,
        "required": field.required,
        "description": field.description,
        "vocabulary": field.vocabulary,
        "native_passthrough": field.native_passthrough,
    }


def _published_concept(concept: str) -> Dict[str, Any]:
    """One concept: its contract, and which connectors can actually supply it."""
    contract = get_contract(concept)
    report = concept_gap_report()[concept]

    sources: List[Dict[str, Any]] = []
    for entry in report["usable"]:
        sources.append({
            "connector_id": entry["connector_id"],
            "source_family": entry["source_family"],
            # NO mapper path — see the module docstring. A partner gets capability.
            "fields_never_populated": entry["fields_never_populated"],
            "fields_conditionally_populated": entry["fields_conditionally_populated"],
            "field_gaps": entry["field_gaps"],
        })

    unavailable = [
        {
            "connector_id": entry["connector_id"],
            "source_family": entry["source_family"],
            "status": entry["status"],
            "reason": entry["reason"],
        }
        for entry in report["unavailable"]
    ]

    return {
        "concept": concept,
        "contract_version": contract.version,
        "purpose": contract.purpose,
        "is_value_type": concept == m.CONCEPT_ENTITY_REFERENCE,
        "required_fields": list(contract.required_fields),
        "optional_fields": list(contract.optional_fields),
        "fields": [_published_field(f) for f in contract.fields],
        "rules": list(contract.rules),
        "available_from": sources,
        "available_from_ids": [s["connector_id"] for s in sources],
        "unavailable_from": unavailable,
    }


def publish_vocabulary() -> Dict[str, Any]:
    """The complete published vocabulary — the 2.0-C3 handoff artifact.

    Deterministic: derived only from the concept model, the contracts and the
    conformance registry, with no clock, no environment and no request context. Two
    calls on one build return equal documents, which is what lets
    :func:`vocabulary_digest` mean anything.
    """
    concepts = {c: _published_concept(c) for c in sorted(m.CONCEPT_SET)}
    return {
        "vocabulary_version": VOCABULARY_VERSION,
        "concept_set_version": CONCEPT_SET_VERSION,
        "contract_versions": {c: CONTRACTS[c].version for c in sorted(CONTRACTS)},
        "concepts": concepts,
        "vocabularies": _vocabulary_values(),
        "stability_contract": list(STABILITY_CONTRACT),
        "breaking_change_rules": list(BREAKING_CHANGE_RULES),
        "sdk_handoff": {k: list(v) for k, v in SDK_HANDOFF.items()},
        "availability": {
            c: concepts[c]["available_from_ids"] for c in sorted(m.CONCEPT_SET)
        },
        "source_families": sorted(
            {d.source_family for d in conf.CONFORMANCE.values()}
        ),
    }


def vocabulary_digest(published: Dict[str, Any] | None = None) -> str:
    """A stable content hash a pack manifest can pin.

    ``sha256`` over the canonical JSON form (sorted keys, no whitespace). Deterministic
    across processes and machines, so a pack authored against digest X can be checked
    against the running platform's vocabulary by comparing one string — which is what
    makes C1's "declares the normalised concepts it needs" enforceable rather than
    advisory.

    Any change a partner could OBSERVE moves the digest: a new concept, a changed
    required field, a vocabulary value added or removed, a connector gaining or losing
    availability, a gap appearing. Changes a partner cannot observe — a mapper
    rename, an internal refactor — deliberately do not, because a digest that churned
    on our refactors would train partners to ignore it.
    """
    document = publish_vocabulary() if published is None else published
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def concepts_available_from(connector_id: str) -> Tuple[str, ...]:
    """Concepts a partner may rely on from one connector today."""
    return conf.get_conformance(connector_id).supported


def sources_for_required_concepts(*required: str) -> Tuple[str, ...]:
    """Connectors that can supply EVERY concept a pack declares it needs.

    The read C1's compatibility check makes on behalf of a pack manifest. Empty means
    the pack cannot run anywhere in this deployment — which is a refusal with a reason,
    not a pack that installs and finds nothing.
    """
    for concept in required:
        if concept not in m.CONCEPT_SET:
            raise KeyError(
                f"{concept!r} is not a published concept; the vocabulary is "
                f"{sorted(m.CONCEPT_SET)}"
            )
    if not required:
        return ()
    return tuple(sorted(
        cid for cid, decl in conf.CONFORMANCE.items()
        if all(decl.position(c).conforms for c in required)
    ))


def unsupported_requirements(connector_id: str, *required: str) -> Tuple[str, ...]:
    """Which of a pack's required concepts this connector cannot supply.

    The other half of the refusal: C1 needs to NAME the unmet requirement, and
    "incompatible" without the missing concept is not an actionable reason.
    """
    decl = conf.get_conformance(connector_id)
    return tuple(sorted(
        c for c in required if c in m.CONCEPT_SET and not decl.position(c).conforms
    ))


__all__ = [
    "VOCABULARY_VERSION",
    "PUBLISHED_VOCABULARIES",
    "SDK_HANDOFF",
    "STABILITY_CONTRACT",
    "publish_vocabulary",
    "vocabulary_digest",
    "concepts_available_from",
    "sources_for_required_concepts",
    "unsupported_requirements",
]
