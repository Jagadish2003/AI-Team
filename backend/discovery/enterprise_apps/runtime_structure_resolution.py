"""R18-A6 / AT-609 (T4) — runtime-to-structure entity resolution.

Phase one (R17-A3/A4) sees a running application *behave* — it emits an
operational (runtime) entity: "service X, error rate rising", identified by its
``app_id`` / ``service`` on a platform (Java / .NET). Phase two (T1/T2/T6) sees
how the application is *built* — a structural entity: the application and its
components, keyed by the configured ``app_id`` (:mod:`.app_repo_map`). The
phase-two payoff is the JOIN: **the service emitting errors IS this component**,
so a located runtime failure can be explained in the application's own terms.

This module makes that join, and it makes it CONSERVATIVELY — consistent with the
standing entity-resolution discipline in :mod:`app.entity_resolution`
(``resolve_or_create_entity``). Only confident, evidence-supported matches merge;
ambiguity leaves the entities separate rather than guessing (AC4). Concretely it
mirrors that engine's three-branch rule and its confidence tiers:

  * **0 structural candidates** → ``unresolved`` (the runtime entity stays
    separate — no structural counterpart is known);
  * **exactly 1 candidate** → ``resolved`` (merge), confidence ``1.0`` when the
    match is on the stable ``app_id`` (the ``source_record_id`` analogue) or
    ``0.8`` when it is only on the service/display name;
  * **2+ candidates** → ``ambiguous`` (left separate, NEVER force-merged — the
    same N+1 avoidance the standing engine applies to multiple candidates).

A platform gate makes the resolution safer still: a Java runtime service never
resolves to a .NET application (or vice-versa) even if their names coincide.

Pure and offline — no DB, no ``app`` import — like the rest of
:mod:`discovery.enterprise_apps`. It returns resolution OUTCOMES; persisting a
confirmed match as a same-entity graph link is the graph layer's job (T3), which
consumes these outcomes. The canonical-name normalisation is identical to the
standing engine's (``" ".join(x.split()).lower()``) so the two agree on what
"the same name" means.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .app_repo_map import AppRepoMapping, load_app_repo_mappings
from .structure import PLATFORM_DOTNET, PLATFORM_JAVA

logger = logging.getLogger(__name__)

# Resolution statuses — the SAME vocabulary as app.entity_resolution / the
# entities table CHECK constraint, so a downstream graph writer can store an
# outcome's status directly.
STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNRESOLVED = "unresolved"

# Confidence tiers — mirror app.entity_resolution._initial_confidence exactly.
CONFIDENCE_STABLE_ID = 1.0  # matched on the stable app_id (source_record_id analogue)
CONFIDENCE_NAME = 0.8       # matched on the service / display name only
CONFIDENCE_AMBIGUOUS = 0.6  # 2+ candidates — recorded, never merged
CONFIDENCE_NONE = 0.0       # no candidate matched

# How a runtime entity matched its structural counterpart.
MATCH_APP_ID = "app_id"
MATCH_SERVICE = "service"

#: Phase-one operational ``source_system`` → structural platform. Anchors the
#: platform gate: a ``java_app`` runtime entity is only ever compatible with a
#: ``java`` structural application.
_SOURCE_SYSTEM_PLATFORM: Dict[str, str] = {
    "java_app": PLATFORM_JAVA,
    "dotnet_app": PLATFORM_DOTNET,
}


def _canonical(value: Optional[str]) -> str:
    """Normalise a name for comparison — identical to
    ``app.entity_resolution._canonicalize`` (collapse whitespace, lowercase) so
    the two layers agree on what "the same name" means."""
    return " ".join((value or "").split()).lower()


# ─────────────────────────────────────────────────────────────────────────────
# The two sides of the join
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuntimeEntity:
    """A phase-one operational (runtime) entity — the service AgentIQ observed
    behaving (R17-A3/A4).

    ``app_id`` is the stable operational identity (``JavaAppTarget`` /
    ``DotNetAppTarget`` ``app_id`` — the checkpoint/artifact key); ``service`` is
    the cross-system service name (``target.service``, which falls back to
    ``app_id``); ``platform`` is derived from ``source_system``
    (``java_app`` → ``java``, ``dotnet_app`` → ``dotnet``). ``entity_id`` is the
    graph entity id when the runtime entity already exists in the graph.
    """

    app_id: str
    service: str
    platform: str
    source_system: str
    entity_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructuralEntity:
    """A phase-two structural counterpart — an application (default) or one of its
    components (T1/T2 + T6).

    ``app_id`` is the configured application id (:class:`AppRepoMapping.app_id`),
    the primary join key; ``service`` mirrors the mapping's service name;
    ``platform`` gates the match. ``kind`` is ``application`` for the app-level
    counterpart (the natural granularity for "the service IS this application")
    or a finer component kind. ``entity_id`` is the graph entity id when known.
    """

    app_id: str
    name: str
    platform: str
    service: str = ""
    kind: str = "application"
    qualified_name: str = ""
    entity_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolutionOutcome:
    """The conservative resolution decision for one runtime entity.

    ``matched`` is the structural counterpart when ``status == 'resolved'``, else
    ``None`` (unresolved/ambiguous entities are deliberately left separate).
    ``candidates`` records every structural entity considered a candidate, so an
    ambiguous decision is auditable — you can see exactly which apps collided.
    """

    runtime: RuntimeEntity
    status: str
    confidence: float
    matched: Optional[StructuralEntity] = None
    match_kind: Optional[str] = None
    reason: str = ""
    candidates: Tuple[StructuralEntity, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.status == STATUS_RESOLVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime": self.runtime.to_dict(),
            "status": self.status,
            "confidence": self.confidence,
            "matched": self.matched.to_dict() if self.matched else None,
            "match_kind": self.match_kind,
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Builders — construct the two sides from the phase-one / phase-two producers
# ─────────────────────────────────────────────────────────────────────────────
def runtime_entity_from_operational(record: Dict[str, Any]) -> Optional[RuntimeEntity]:
    """Build a :class:`RuntimeEntity` from an operational record/target dict.

    Reads the identity fields the R17-A3/A4 producers carry (``app_id``,
    ``service``, ``source_system``). ``service`` falls back to ``app_id`` (as
    ``target.service`` does); ``platform`` derives from ``source_system``, falling
    back to an explicit ``platform`` field. Returns ``None`` when the record
    carries no usable identity at all — a nameless runtime signal cannot be
    resolved and must not become a phantom entity.
    """
    if not isinstance(record, dict):
        return None
    app_id = str(record.get("app_id") or "").strip()
    service = str(record.get("service") or "").strip()
    if not app_id and not service:
        return None
    source_system = str(record.get("source_system") or "").strip()
    platform = _SOURCE_SYSTEM_PLATFORM.get(
        source_system, str(record.get("platform") or "").strip().lower()
    )
    return RuntimeEntity(
        app_id=app_id or service,
        service=service or app_id,
        platform=platform,
        source_system=source_system,
        entity_id=(str(record["entity_id"]) if record.get("entity_id") else None),
        metadata=record.get("metadata") if isinstance(record.get("metadata"), dict) else {},
    )


def structural_entities_from_mappings(
    mappings: Iterable[AppRepoMapping],
) -> List[StructuralEntity]:
    """Application-level structural counterparts from the T6 app→repo mappings.

    Each configured application is the structural entity a runtime service most
    naturally resolves to ("the service IS this application"); its components and
    code (T1/T2) hang off it for the downstream finding join (AC7).
    """
    return [
        StructuralEntity(
            app_id=m.app_id,
            name=m.name,
            platform=m.platform,
            service=m.service,
            kind="application",
            qualified_name=m.app_id,
            metadata=dict(m.metadata or {}),
        )
        for m in mappings
    ]


def structural_entities_for_org(org_id: str) -> List[StructuralEntity]:
    """The configured application structural counterparts for an org (via T6)."""
    return structural_entities_from_mappings(load_app_repo_mappings(org_id))


# ─────────────────────────────────────────────────────────────────────────────
# The conservative resolver
# ─────────────────────────────────────────────────────────────────────────────
def _platform_compatible(runtime: RuntimeEntity, candidate: StructuralEntity) -> bool:
    """A runtime entity and a structural entity can only be the same real thing if
    their platforms agree. When either side's platform is unknown we cannot rule
    the match out on platform grounds, so we do not gate on it."""
    if runtime.platform and candidate.platform:
        return runtime.platform == candidate.platform
    return True


def _match_kind(runtime: RuntimeEntity, candidate: StructuralEntity) -> Optional[str]:
    """How (if at all) a runtime entity matches a structural candidate.

    Stable-id match (on ``app_id``) is the strongest evidence and is checked
    first; otherwise a service/display-name match. All comparisons are on the
    canonical (whitespace-collapsed, lowercased) form.
    """
    r_app = _canonical(runtime.app_id)
    if r_app and r_app == _canonical(candidate.app_id):
        return MATCH_APP_ID
    runtime_keys = {k for k in (_canonical(runtime.service), r_app) if k}
    struct_keys = {
        k
        for k in (
            _canonical(candidate.service),
            _canonical(candidate.name),
            _canonical(candidate.app_id),
        )
        if k
    }
    if runtime_keys & struct_keys:
        return MATCH_SERVICE
    return None


def _distinct(entities: List[StructuralEntity]) -> List[StructuralEntity]:
    """Distinct structural entities (by app_id/kind/qualified_name), deterministically
    ordered — so two apps with a coincidentally shared name still count as two."""
    seen: set = set()
    out: List[StructuralEntity] = []
    for e in sorted(entities, key=lambda x: (x.app_id, x.kind, x.qualified_name, x.name)):
        key = (e.app_id, e.kind, e.qualified_name)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def resolve_runtime_entity(
    runtime: RuntimeEntity, structural_entities: Iterable[StructuralEntity]
) -> ResolutionOutcome:
    """Resolve ONE runtime entity to its structural counterpart, conservatively.

    Prefers a stable ``app_id`` match over a name-only match (stronger evidence).
    Applies the three-branch discipline: 0 candidates → unresolved; exactly 1 →
    resolved (merge); 2+ → ambiguous (left separate). Never force-merges.
    """
    compatible = [c for c in structural_entities if _platform_compatible(runtime, c)]

    id_matches: List[StructuralEntity] = []
    name_matches: List[StructuralEntity] = []
    for cand in compatible:
        kind = _match_kind(runtime, cand)
        if kind == MATCH_APP_ID:
            id_matches.append(cand)
        elif kind == MATCH_SERVICE:
            name_matches.append(cand)

    if id_matches:
        chosen, match_kind, confidence = _distinct(id_matches), MATCH_APP_ID, CONFIDENCE_STABLE_ID
    elif name_matches:
        chosen, match_kind, confidence = _distinct(name_matches), MATCH_SERVICE, CONFIDENCE_NAME
    else:
        return ResolutionOutcome(
            runtime=runtime,
            status=STATUS_UNRESOLVED,
            confidence=CONFIDENCE_NONE,
            matched=None,
            match_kind=None,
            reason="no structural counterpart matched — left separate",
            candidates=(),
        )

    if len(chosen) == 1:
        return ResolutionOutcome(
            runtime=runtime,
            status=STATUS_RESOLVED,
            confidence=confidence,
            matched=chosen[0],
            match_kind=match_kind,
            reason=f"resolved on {match_kind} to '{chosen[0].app_id}'",
            candidates=tuple(chosen),
        )

    # 2+ distinct candidates — the standing N+1 discipline: record the ambiguity,
    # do NOT merge. The runtime entity stays separate until the evidence is
    # unambiguous (e.g. a stable app_id is configured).
    return ResolutionOutcome(
        runtime=runtime,
        status=STATUS_AMBIGUOUS,
        confidence=CONFIDENCE_AMBIGUOUS,
        matched=None,
        match_kind=match_kind,
        reason=(
            f"{len(chosen)} structural candidates matched on {match_kind} "
            "— left separate (conservative)"
        ),
        candidates=tuple(chosen),
    )


def resolve_runtime_to_structure(
    runtime_entities: Iterable[RuntimeEntity],
    structural_entities: Iterable[StructuralEntity],
) -> List[ResolutionOutcome]:
    """Resolve a batch of runtime entities against the structural entities.

    Each runtime entity is resolved independently and in input order, so the
    result is deterministic. The structural set is materialised once and reused.
    """
    structural = list(structural_entities)
    return [resolve_runtime_entity(r, structural) for r in runtime_entities]


def resolve_for_org(
    org_id: str, runtime_entities: Iterable[RuntimeEntity]
) -> List[ResolutionOutcome]:
    """Resolve runtime entities against the org's configured applications (T6).

    The convenience integration point: it pulls the structural counterparts from
    the per-org app→repo mapping (:func:`structural_entities_for_org`) so a caller
    only has to supply the runtime side.
    """
    return resolve_runtime_to_structure(
        runtime_entities, structural_entities_for_org(org_id)
    )
