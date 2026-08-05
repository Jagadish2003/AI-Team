"""2.0-B4 T2 — the declared-gap surface (AC5).

AC5: *"Unmappable connector concepts are recorded as declared gaps, visible to pack
authors — never silently approximated."*

Three clauses, three different jobs, and this module is where the second and third are
discharged. ``conformance.py`` RECORDS the gaps; this module makes them VISIBLE in the
shape a pack author actually needs, and gives the "never approximated" clause teeth
that outlast a code review.

Why the report is inverted (concept-first, not connector-first)
--------------------------------------------------------------
``conformance.CONFORMANCE`` is keyed by connector, which is the right shape for the
question a connector author asks ("what do I still owe?"). A pack author asks the
opposite question: *"I want to write a queue-ageing detector — which sources can carry
it, and what will be missing when it runs?"* Answering that from a connector-keyed
registry means reading thirteen entries and inverting them by hand, which is exactly
the kind of chore that ends with someone assuming a field is present. So
:func:`concept_gap_report` inverts it once, here.

The "never silently approximated" clause
----------------------------------------
A recorded gap only helps if the code cannot quietly contradict it. Two mechanisms:

* :func:`assert_no_approximation` checks a PRODUCED concept against its connector's
  declaration and raises if a field declared ``absent`` came back populated. A mapper
  that starts filling in a field it declared missing — the exact drift AC5 is about —
  fails at the point it happens rather than at the point a customer notices.
* ``test_r2_0_b4_t2_connector_mapping.py`` runs every mapper over golden fixtures and
  applies that check to each output, so the guard is exercised by CI rather than only
  available to a caller who remembers it.

The asymmetry is deliberate. A declared-``absent`` field that IS populated is a broken
promise and raises. A declared-``partial`` field that is empty for a given record is
the documented condition doing its job, so it never raises — which is why the two kinds
exist rather than one "maybe missing" flag.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

try:
    from discovery.concepts import conformance as conf
    from discovery.concepts import model as m
    from discovery.concepts.contracts import CONCEPT_SET_VERSION, get_contract
    from discovery.concepts.mappers import MAPPERS, mapped_concepts
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import conformance as conf
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.contracts import CONCEPT_SET_VERSION, get_contract
    from backend.discovery.concepts.mappers import MAPPERS, mapped_concepts


class ApproximationError(AssertionError):
    """A mapper populated a field its connector declared it cannot populate.

    An ``AssertionError`` rather than a ``ValueError`` because this is a broken
    invariant in our own code, not bad input: by the time it raises, the registry and
    the mapper are already contradicting each other.
    """


def concept_gap_report() -> Dict[str, Any]:
    """Per CONCEPT: who supports it, who cannot, and what is missing where.

    The pack-author view. ``usable`` is the list a detector author can actually build
    against — connectors whose status is ``supported`` — and it deliberately excludes
    ``declared``: a detector pointed at a connector with no mapper would run, find
    nothing, and report an empty result as an answer.
    """
    report: Dict[str, Any] = {}
    for concept in sorted(m.CONCEPT_SET):
        contract = get_contract(concept)
        usable: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, Any]] = []
        for connector_id, decl in sorted(conf.CONFORMANCE.items()):
            position = decl.position(concept)
            entry = {
                "connector_id": connector_id,
                "source_family": decl.source_family,
                "status": position.status,
                "reason": position.reason,
            }
            if position.conforms:
                entry["mapper"] = position.mapper
                entry["field_gaps"] = [g.to_dict() for g in position.field_gaps]
                entry["fields_never_populated"] = [
                    g.field for g in position.field_gaps if g.kind == conf.GAP_ABSENT
                ]
                entry["fields_conditionally_populated"] = [
                    g.field for g in position.field_gaps if g.kind == conf.GAP_PARTIAL
                ]
                usable.append(entry)
            else:
                unavailable.append(entry)
        report[concept] = {
            "contract_version": contract.version,
            "required_fields": list(contract.required_fields),
            "usable": usable,
            "unavailable": unavailable,
            "usable_connector_ids": [e["connector_id"] for e in usable],
        }
    return report


def connector_gap_report(connector_id: str) -> Dict[str, Any]:
    """Per CONNECTOR: what it supports, what it owes, and what it cannot do.

    The connector-author view, and the one a reviewer reads when deciding whether a
    ``declared`` entry is still honest. ``outstanding`` is the work list — concepts the
    source genuinely carries with no mapper yet — kept separate from ``gaps``, which
    are decisions rather than debt.
    """
    decl = conf.get_conformance(connector_id)
    mapped = set(mapped_concepts(connector_id))
    return {
        "connector_id": connector_id,
        "source_family": decl.source_family,
        "concept_set_version": decl.concept_set_version,
        "supported": [
            {
                "concept": c.concept,
                "mapper": c.mapper,
                "field_gaps": [g.to_dict() for g in c.field_gaps],
            }
            for c in decl.concepts if c.conforms
        ],
        "outstanding": [
            {"concept": c.concept, "reason": c.reason}
            for c in decl.concepts if c.status == conf.STATUS_DECLARED
        ],
        "gaps": [g.to_dict() for g in decl.gaps],
        "not_applicable": [
            {"concept": c.concept, "reason": c.reason}
            for c in decl.concepts if c.status == conf.STATUS_NOT_APPLICABLE
        ],
        # A registered mapper for a concept NOT declared supported is a registry that
        # has fallen behind its own code. Surfaced rather than ignored.
        "mapped_but_not_declared": sorted(
            mapped - {c.concept for c in decl.concepts if c.conforms}
        ),
    }


def field_gaps_for(connector_id: str, concept: str) -> Tuple[conf.FieldGap, ...]:
    """The field gaps one connector declares on one concept."""
    return conf.get_conformance(connector_id).position(concept).field_gaps


def unpopulated_fields(connector_id: str, concept: str) -> Tuple[str, ...]:
    """Fields this connector NEVER populates on this concept (``absent`` only).

    What :func:`assert_no_approximation` enforces, and what a detector author should
    treat as unavailable rather than merely often-empty.
    """
    return tuple(sorted(
        gap.field for gap in field_gaps_for(connector_id, concept)
        if gap.kind == conf.GAP_ABSENT
    ))


def _field_value(produced: Any, field: str) -> Any:
    """Read a contract field off a produced concept, dict or object alike."""
    if isinstance(produced, dict):
        return produced.get(field)
    return getattr(produced, field, None)


def assert_no_approximation(connector_id: str, concept: str, produced: Any) -> None:
    """Raise if ``produced`` populated a field declared ``absent`` for this connector.

    The runtime half of AC5's "never silently approximated". Cheap enough to call on a
    mapper's output in a test for every fixture, which is where it is wired.

    ``partial`` gaps are deliberately NOT checked: a conditionally-populated field
    being present is the condition being met, not a violation.
    """
    for field in unpopulated_fields(connector_id, concept):
        value = _field_value(produced, field)
        if value not in (None, "", [], {}):
            gap = next(
                g for g in field_gaps_for(connector_id, concept) if g.field == field
            )
            raise ApproximationError(
                f"{connector_id}/{concept}: field {field!r} is declared ABSENT "
                f"({gap.reason}) but the mapper produced {value!r}. Either the mapper "
                f"is approximating a value the source does not carry, or the source "
                f"gained the field and the declaration is stale — resolve which, do "
                f"not delete the check."
            )


def gap_summary() -> Dict[str, Any]:
    """The whole gap picture, serialisable — what the concepts API serves.

    Counts are included because they are the number worth watching over a release:
    ``outstanding_count`` falling is progress, and ``field_gap_count`` RISING is
    usually progress too — it means somebody wrote down a limitation that was
    previously only discoverable by reading a mapper.
    """
    by_concept = concept_gap_report()
    connectors = {
        cid: connector_gap_report(cid) for cid in sorted(conf.CONFORMANCE)
    }
    field_gap_count = sum(
        len(entry["field_gaps"])
        for view in connectors.values()
        for entry in view["supported"]
    )
    return {
        "concept_set_version": CONCEPT_SET_VERSION,
        "mapper_count": len(MAPPERS),
        "concepts": by_concept,
        "connectors": connectors,
        "concept_gap_count": sum(len(v["gaps"]) for v in connectors.values()),
        "outstanding_count": sum(len(v["outstanding"]) for v in connectors.values()),
        "field_gap_count": field_gap_count,
        "registry_behind_code": sorted(
            cid for cid, v in connectors.items() if v["mapped_but_not_declared"]
        ),
    }


def concepts_usable_by(connector_id: str) -> Tuple[str, ...]:
    """Concepts a pack author can rely on from this connector TODAY."""
    decl = conf.get_conformance(connector_id)
    return decl.supported


def connectors_for_detector(*required_concepts: str) -> Tuple[str, ...]:
    """Connectors supporting EVERY concept a detector needs.

    The portability question 2.0-B4 T3 asks ("which source families can this detector
    run against?"), answered from the declarations rather than by trying it and seeing.
    An unknown concept raises rather than being ignored, so a typo in a detector's
    requirements cannot silently widen the answer.
    """
    for concept in required_concepts:
        if concept not in m.CONCEPT_SET:
            raise KeyError(
                f"{concept!r} is not a normalised concept; the set is "
                f"{sorted(m.CONCEPT_SET)}"
            )
    if not required_concepts:
        return ()
    return tuple(sorted(
        cid for cid, decl in conf.CONFORMANCE.items()
        if all(decl.position(c).conforms for c in required_concepts)
    ))


__all__ = [
    "ApproximationError",
    "concept_gap_report",
    "connector_gap_report",
    "field_gaps_for",
    "unpopulated_fields",
    "assert_no_approximation",
    "gap_summary",
    "concepts_usable_by",
    "connectors_for_detector",
]
