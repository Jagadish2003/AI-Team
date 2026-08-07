"""2.0-B4 T2 — connector → concept mappers, and the registry that makes a
conformance claim checkable.

T1 defined the concept set, the versioned contracts, and a per-connector
conformance declaration in which ``supported`` must NAME the mapper implementing
it. T1 shipped with nothing ``supported``, because no mapper existed. This package
is those mappers.

Why a registry rather than a naming convention
----------------------------------------------
``ConceptConformance`` refuses ``supported`` unless a mapper is named, but a NAME is
just a string — the strongest claim in the registry could still point at nothing. The
registry closes that: :func:`resolve_mapper` looks the name up, and
``test_r2_0_b4_t2_connector_mapping.py`` asserts every ``supported`` declaration
resolves to a REGISTERED callable. So a conformance claim cannot be made by editing a
docstring, which was the one soft spot left in T1's design.

Registration is by decorator at definition (`@maps("servicenow", CONCEPT_WORK_ITEM)`),
so the mapper and its claim cannot drift apart: there is no second list to update.
Submodules are imported at the bottom of this module and self-register on import —
the same pattern as ``discovery/ingest/extraction/__init__.py``.

What a mapper is, and is not
----------------------------
A mapper is a PURE function ``(org_id, record, **ctx) -> concept | list | None``. It
does no I/O, holds no client, reads no environment and never touches the DB, exactly
like MSP-B0's ``reference_mappers``. That is what lets a golden fixture pin the whole
normalised output, and what keeps the concept layer usable offline.

A mapper must not INVENT. Three rules, each enforced by a test rather than asserted:

1. A field the source does not carry is left ``None`` — never defaulted to something
   plausible. Where that field matters, the connector declares a ``field_gap``
   (T2's addition to the conformance record) so the omission is visible to a pack
   author instead of looking like missing data.
2. A native value with no mapping onto a closed vocabulary raises. The model's
   ``_validate_token`` already refuses an out-of-vocabulary token; mappers deliberately
   do not catch it and substitute ``"other"``, because a silent ``"other"`` is the
   approximation AC5 exists to prevent.
3. An individual is never turned into a group. Where a source records only a person
   (a Jira assignee, a Salesforce user owner), the group-shaped field stays ``None``
   and the gap is declared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

try:
    from discovery.concepts import model as m
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts import model as m

logger = logging.getLogger(__name__)


class MapperError(ValueError):
    """A mapper is registered wrongly, or asked to map a record it cannot."""


def _enforce_no_approximation(connector_id: str, concept: str, produced: Any) -> None:
    """Runtime enforcement of AC5's "never silently approximated" at the mapper's
    own output — the single choke point every registry-mediated mapping passes.

    ``gaps.assert_no_approximation`` was previously wired ONLY in the fixture test,
    so a new mapper added without a golden-fixture case could populate a field its
    connector declares ABSENT in production indefinitely, undetected — a pack author
    treating that field as unavailable per the conformance registry would then be
    silently wrong. Calling it here means the moment any caller invokes a mapper
    through the registry (``get_mapper(...)(...)``), an approximation raises.

    ``gaps`` imports ``MAPPERS`` from this module, so the import is LAZY to avoid a
    cycle. An ``ApproximationError`` propagates (the whole point); a conformance
    LOOKUP failure (e.g. a mapper whose connector has no conformance declaration, so
    nothing is declared absent and there is nothing to approximate) degrades to a
    debug log rather than breaking the mapping.
    """
    if produced is None:
        return
    try:
        from discovery.concepts import gaps
    except ModuleNotFoundError:  # pragma: no cover - import-style shim
        from backend.discovery.concepts import gaps
    items = produced if isinstance(produced, (list, tuple)) else (produced,)
    for item in items:
        if item is None:
            continue
        try:
            gaps.assert_no_approximation(connector_id, concept, item)
        except gaps.ApproximationError:
            raise
        except Exception as exc:  # noqa: BLE001 — a missing decl must not break mapping
            logger.debug(
                "no-approximation check skipped for %s/%s: %s",
                connector_id, concept, exc,
            )


@dataclass(frozen=True)
class ConceptMapper:
    """One registered (connector, concept) mapping."""

    connector_id: str
    concept: str
    fn: Callable[..., Any]
    #: ``module:function`` — the string a conformance declaration carries, so a
    #: reviewer reading the registry can open the code that backs the claim.
    name: str

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        produced = self.fn(*args, **kwargs)
        # AC5 runtime guard: a mapper must never populate a field its connector
        # declared ABSENT. Enforced at the invocation choke point, not only in the
        # fixture test, so a new mapper cannot approximate in production undetected.
        _enforce_no_approximation(self.connector_id, self.concept, produced)
        return produced


#: (connector_id, concept) → mapper. Populated by the decorator below.
MAPPERS: Dict[Tuple[str, str], ConceptMapper] = {}


def mapper_name(fn: Callable[..., Any]) -> str:
    """``module:function`` for a mapper, the form conformance records."""
    module = getattr(fn, "__module__", "") or ""
    # Record the package-relative module so the name is identical whether the
    # suite runs from backend/ or the repo root (both import paths exist).
    if module.startswith("backend."):
        module = module[len("backend."):]
    return f"{module}:{fn.__name__}"


def maps(connector_id: str, concept: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a mapper for one (connector, concept) pair.

    Registration happens at definition so the claim and the code are the same edit.
    A duplicate registration is an error rather than a silent overwrite: two mappers
    for one pair means one of them is dead, and which one is a coin toss.
    """
    if concept not in m.CONCEPT_SET:
        raise MapperError(
            f"{concept!r} is not a normalised concept; the set is {sorted(m.CONCEPT_SET)}"
        )

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        key = (connector_id, concept)
        if key in MAPPERS:
            raise MapperError(
                f"{connector_id}/{concept} already has mapper "
                f"{MAPPERS[key].name!r}; two mappers for one pair means one is dead"
            )
        MAPPERS[key] = ConceptMapper(
            connector_id=connector_id, concept=concept, fn=fn, name=mapper_name(fn)
        )
        return fn

    return decorate


def get_mapper(connector_id: str, concept: str) -> ConceptMapper:
    """The mapper for one pair, or a named error."""
    try:
        return MAPPERS[(connector_id, concept)]
    except KeyError:
        raise MapperError(
            f"no mapper for {connector_id!r}/{concept!r}; "
            f"{connector_id} maps {list(mapped_concepts(connector_id))}"
        ) from None


def mapped_concepts(connector_id: str) -> Tuple[str, ...]:
    """Concepts this connector has a mapper for."""
    return tuple(sorted(
        concept for (cid, concept) in MAPPERS if cid == connector_id
    ))


def resolve_mapper(name: str) -> ConceptMapper:
    """Look up a registered mapper by its ``module:function`` name.

    The check that turns a conformance ``mapper`` string from documentation into a
    verifiable reference.
    """
    for mapper in MAPPERS.values():
        if mapper.name == name:
            return mapper
    raise MapperError(
        f"{name!r} is not a registered mapper — a `supported` conformance claim must "
        f"point at one. Registered: {sorted(mp.name for mp in MAPPERS.values())}"
    )


def registry_summary() -> Dict[str, Any]:
    """The registry, serialisable — read by the concepts API and the docs."""
    by_connector: Dict[str, list] = {}
    for (cid, concept), mapper in sorted(MAPPERS.items()):
        by_connector.setdefault(cid, []).append({"concept": concept, "mapper": mapper.name})
    return {
        "mapper_count": len(MAPPERS),
        "connectors": by_connector,
        "concepts_mapped": sorted({c for (_cid, c) in MAPPERS}),
    }


# Importing the mapper modules registers every mapper. Placed at the bottom so the
# decorator above is defined first; mirrors discovery/ingest/extraction/__init__.py.
try:  # pragma: no cover - import-style shim, exercised by both layouts
    from . import cloud_events, content, jira, salesforce, servicenow  # noqa: F401,E402
except ImportError:  # pragma: no cover
    from backend.discovery.concepts.mappers import (  # type: ignore # noqa: F401,E402
        cloud_events, content, jira, salesforce, servicenow,
    )


__all__ = [
    "MapperError",
    "ConceptMapper",
    "MAPPERS",
    "maps",
    "mapper_name",
    "get_mapper",
    "mapped_concepts",
    "resolve_mapper",
    "registry_summary",
]
