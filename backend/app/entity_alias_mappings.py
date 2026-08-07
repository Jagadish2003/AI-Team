"""
entity_alias_mappings.py — Release 2.0-B2 T1: the org-configured alias table.

Tier 2 of the ranked cross-source resolution engine
(:mod:`app.cross_source_resolution`) is an **org-configured alias mapping**: a
small, Owner-managed table that says "these names are the same real thing in this
organisation". It exists because tier 1 (explicit cross-references in the source
data) cannot cover the cases where two systems simply never reference each other
— ServiceNow calls a service ``Payments API``, the repo calls it
``payments-api`` — and tier 3 (name similarity) is deliberately not allowed to
merge anything.

An alias mapping is therefore a **human assertion of identity**, which is why it
is allowed to auto-merge: a person with knowledge of the estate stated it, and
the statement is recorded, attributable, and reversible. That also means the
table itself has to be trustworthy, so this module is strict:

  * every mapping is scoped to ONE ``entity_type`` — a team named "Payments" is
    never merged with a system named "Payments";
  * aliases are normalised through the SAME canonicalisation the entity layer
    uses (``entity_resolution.canonical_name_for``), so the table cannot disagree
    with the graph about what a name is;
  * a canonical value is itself an alias of its own group (so listing the
    canonical name is optional);
  * **a conflicting table is rejected, not silently resolved.** If one alias
    appears in two groups of the same entity type, the two groups would each
    claim the same name and the resolver's answer would depend on iteration
    order. That is exactly how a wrong merge happens invisibly, so
    :func:`normalize_alias_mappings` raises :class:`AliasMappingConflict`
    instead of picking one.

Storage. The table is org-scoped state, read through the existing ``kv`` layer
under :data:`ALIAS_KV_KEY` (``kv`` key ``entity_alias_mappings:{org_id}``) — the
same "namespace the key by the owning scope" pattern ``db.run_kv_get`` uses, so
no schema change is needed for the engine to be usable. ``ENTITY_ALIAS_MAPPINGS``
(a JSON array, or an object keyed by org id with a ``default``/``*`` fallback)
overrides it for offline/dev and for a deployment that prefers to declare the
table as configuration — mirroring ``ENTERPRISE_APP_REPOS``.

**Out of scope for T1:** the Owner-facing management surface (routes/UI) for
editing this table, and the review workflow for tier-3 proposals. This module is
the engine's INPUT and the seam that surface will write through; ``put_alias_
mappings`` is deliberately the only writer so validation cannot be bypassed.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from database.models.entities import ENTITY_TYPES

from .entity_resolution import canonical_name_for

logger = logging.getLogger(__name__)

#: ``kv`` key template for one org's alias table.
ALIAS_KV_KEY = "entity_alias_mappings"

#: Environment override (offline/dev, or a deployment that declares the table as
#: configuration). A JSON array, or an object keyed by org id.
ALIAS_ENV_VAR = "ENTITY_ALIAS_MAPPINGS"


class AliasMappingError(ValueError):
    """An alias mapping is malformed and cannot be trusted to auto-merge."""


class AliasMappingConflict(AliasMappingError):
    """One alias is claimed by two groups of the same entity type.

    Raised rather than resolved: the resolver's answer would otherwise depend on
    iteration order, which is how a wrong merge ships invisibly.
    """


@dataclass(frozen=True)
class AliasMapping:
    """One org-asserted identity group.

    ``canonical`` is the preferred name for the group (used as the group id and
    as the merge target's label); ``aliases`` are the other names that mean the
    same thing. Both are stored canonicalised. ``entity_type`` scopes the group,
    and ``note``/``created_by`` carry the human provenance an auditor needs when
    asking why two entities were merged.
    """

    entity_type: str
    canonical: str
    aliases: Tuple[str, ...] = ()
    note: str = ""
    created_by: str = ""

    @property
    def group_id(self) -> str:
        """Stable id for the group — ``"{entity_type}:{canonical}"``."""
        return f"{self.entity_type}:{self.canonical}"

    @property
    def members(self) -> Tuple[str, ...]:
        """Every canonical name in this group, canonical value included."""
        return (self.canonical,) + tuple(a for a in self.aliases if a != self.canonical)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "canonical": self.canonical,
            "aliases": list(self.aliases),
            "note": self.note,
            "created_by": self.created_by,
        }


@dataclass(frozen=True)
class AliasIndex:
    """Canonical name → alias group, for one org.

    Built once per resolution pass so a lookup is O(1) and the (validated,
    conflict-free) table cannot be re-interpreted differently mid-pass.
    """

    by_member: Mapping[Tuple[str, str], AliasMapping] = field(default_factory=dict)

    def group_for(self, entity_type: str, canonical: str) -> Optional[AliasMapping]:
        if not entity_type or not canonical:
            return None
        return self.by_member.get((entity_type, canonical))

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.by_member)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_mapping(raw: Any) -> AliasMapping:
    """Build one validated :class:`AliasMapping` from a raw dict."""
    if not isinstance(raw, Mapping):
        raise AliasMappingError(f"alias mapping must be an object, got {type(raw).__name__}")

    entity_type = _text(raw.get("entity_type")).lower()
    if entity_type not in ENTITY_TYPES:
        raise AliasMappingError(
            f"alias mapping entity_type must be one of {sorted(ENTITY_TYPES)}, "
            f"got {entity_type!r}"
        )

    canonical = canonical_name_for(_text(raw.get("canonical")))
    if not canonical:
        raise AliasMappingError("alias mapping requires a non-empty 'canonical' name")

    raw_aliases = raw.get("aliases")
    if raw_aliases is None:
        raw_aliases = []
    if isinstance(raw_aliases, (str, bytes)) or not isinstance(raw_aliases, Iterable):
        raise AliasMappingError("alias mapping 'aliases' must be a list of names")

    aliases: List[str] = []
    for alias in raw_aliases:
        normalised = canonical_name_for(_text(alias))
        if not normalised or normalised == canonical or normalised in aliases:
            continue
        aliases.append(normalised)

    if not aliases:
        # A group of one asserts nothing — it can never make two entities equal,
        # and silently keeping it would make the table look configured when it is
        # not. Reject it so the operator sees the mistake.
        raise AliasMappingError(
            f"alias mapping {entity_type}:{canonical!r} lists no aliases distinct "
            "from its canonical name — it cannot resolve anything"
        )

    return AliasMapping(
        entity_type=entity_type,
        canonical=canonical,
        aliases=tuple(sorted(aliases)),
        note=_text(raw.get("note")),
        created_by=_text(raw.get("created_by")),
    )


def normalize_alias_mappings(raw_mappings: Any) -> List[AliasMapping]:
    """Validate + normalise a raw alias table.

    Returns the mappings in a deterministic order (``entity_type``, then
    ``canonical``). Raises :class:`AliasMappingError` for a malformed entry and
    :class:`AliasMappingConflict` when one alias is claimed by two groups of the
    same entity type — see the module docstring for why that is fatal rather than
    resolved.
    """
    if raw_mappings is None:
        return []
    if isinstance(raw_mappings, Mapping):
        raw_mappings = [raw_mappings]
    if isinstance(raw_mappings, (str, bytes)) or not isinstance(raw_mappings, Iterable):
        raise AliasMappingError("alias mappings must be a list of objects")

    mappings = [_coerce_mapping(raw) for raw in raw_mappings]

    claimed: Dict[Tuple[str, str], str] = {}
    for mapping in mappings:
        for member in mapping.members:
            key = (mapping.entity_type, member)
            owner = claimed.get(key)
            if owner is not None and owner != mapping.group_id:
                raise AliasMappingConflict(
                    f"alias {member!r} ({mapping.entity_type}) is claimed by both "
                    f"{owner!r} and {mapping.group_id!r} — an ambiguous alias table "
                    "cannot auto-merge; keep one group per name"
                )
            claimed[key] = mapping.group_id

    return sorted(mappings, key=lambda m: (m.entity_type, m.canonical))


def build_alias_index(mappings: Sequence[AliasMapping]) -> AliasIndex:
    """Index validated mappings by ``(entity_type, canonical member)``."""
    by_member: Dict[Tuple[str, str], AliasMapping] = {}
    for mapping in mappings:
        for member in mapping.members:
            by_member[(mapping.entity_type, member)] = mapping
    return AliasIndex(by_member=by_member)


def _from_env(org_id: str) -> Optional[List[AliasMapping]]:
    """Alias table from :data:`ALIAS_ENV_VAR`, or None when unset/unusable.

    Accepts a plain array (applies to every org) or an object keyed by org id
    with a ``default``/``*`` fallback — the ``ENTERPRISE_APP_REPOS`` shape. A
    malformed override raises: an operator who configured this deliberately must
    see the mistake rather than silently get an empty table.
    """
    raw = os.getenv(ALIAS_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — surfaced as a config error below.
        raise AliasMappingError(f"{ALIAS_ENV_VAR} is not valid JSON: {exc}") from exc
    if isinstance(parsed, Mapping):
        for key in (org_id, "default", "*"):
            if key in parsed:
                return normalize_alias_mappings(parsed[key])
        return []
    return normalize_alias_mappings(parsed)


def get_alias_mappings(org_id: str) -> List[AliasMapping]:
    """The validated alias table for ``org_id`` (env override first, then ``kv``).

    Returns ``[]`` when nothing is configured. A STORED table that fails
    validation resolves to ``[]`` with a loud warning rather than raising: tier 2
    then contributes nothing and the caller still gets tiers 1 and 3, which is the
    conservative degradation — an unusable alias table must never merge anything,
    and it must never break a discovery run either. A malformed ENV override does
    raise (see :func:`_from_env`).
    """
    if not org_id:
        return []
    from_env = _from_env(org_id)
    if from_env is not None:
        return from_env

    try:
        from . import db

        stored = db.kv_get(f"{ALIAS_KV_KEY}:{org_id}")
    except Exception as exc:  # noqa: BLE001 — never break a run on a config read.
        logger.warning("entity alias table unreadable for org %s: %s", org_id, exc)
        return []

    if not stored:
        return []
    try:
        return normalize_alias_mappings(stored)
    except AliasMappingError as exc:
        logger.warning(
            "entity alias table for org %s is invalid and will not be applied "
            "(tier-2 alias resolution contributes nothing this pass): %s",
            org_id, exc,
        )
        return []


def get_alias_index(org_id: str) -> AliasIndex:
    """The org's alias table, indexed for the resolver."""
    return build_alias_index(get_alias_mappings(org_id))


def put_alias_mappings(org_id: str, raw_mappings: Any) -> List[AliasMapping]:
    """Replace ``org_id``'s alias table with a validated copy of ``raw_mappings``.

    The ONLY writer, so a malformed or conflicting table can never be persisted:
    validation runs first and raises, leaving the stored table untouched. Returns
    the normalised mappings that were stored. Org-scoped by construction — the key
    embeds the org, so one org's table can never overwrite another's.
    """
    if not org_id:
        raise AliasMappingError("an alias table must be scoped to an org")
    mappings = normalize_alias_mappings(raw_mappings)

    from . import db

    db.kv_set(f"{ALIAS_KV_KEY}:{org_id}", [m.to_dict() for m in mappings])
    return mappings


__all__ = [
    "ALIAS_KV_KEY",
    "ALIAS_ENV_VAR",
    "AliasMapping",
    "AliasIndex",
    "AliasMappingError",
    "AliasMappingConflict",
    "normalize_alias_mappings",
    "build_alias_index",
    "get_alias_mappings",
    "get_alias_index",
    "put_alias_mappings",
]
