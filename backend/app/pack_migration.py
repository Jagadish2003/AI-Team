"""Org-config pack migration — 2.0-C4 T3 (AT-844) migration assist.

The rule this module owns (sub-task scope):

    Where a replacement is declared, an org-config migration maps template/pack
    selections from the deprecated pack to the replacement, PREVIEWED before
    applying and REVERSIBLE.

Why this exists
---------------
2.0-C4 T1 (AT-842) lets a pack declare that it is superseded and name what replaces
it; T2 (AT-843) shows that notice at run configuration, in run health, and on the
pack's findings. Both are information. Neither moves the customer, and the parent
story is explicit that a superseded pack must leave the customer with *a path* —
"not a broken configuration". This module is the path: it rewrites the org's saved
run configuration so its pack and template selections point at the replacement.

Three properties, in this order (AC2)
-------------------------------------
1. **Preview.** :func:`preview_migration` computes the exact field-level change set
   and writes nothing. Migrating a customer's run configuration silently would be a
   worse failure than the deprecation it fixes.
2. **Apply on confirmation.** :func:`apply_migration` performs the rewrite. The
   caller may pass the ``fingerprint`` it previewed; if the plan has moved since
   (someone edited the configuration, or the declaration changed) the apply is
   REFUSED rather than applying a change nobody saw.
3. **Reversible.** Every applied change records its PREVIOUS value verbatim, so
   :func:`revert_migration` restores exactly what was there. Reverting is not
   "map the replacement back to the deprecated pack" — that would also rewrite
   selections that legitimately pointed at the replacement all along.

What it migrates, and what it deliberately does not
---------------------------------------------------
The migrated surface is the org's saved Stack Builder setup state (the ``kv`` row
``stack_builder_state:{org_id}``) — the persisted answer to "what does this org run".
Within it, only the SELECTION fields are rewritten:

===================== ===============================================================
``packId``            the primary pack
``packIds``           the order-preserving multi-pack selection
``templateId``        the primary template
``templateIds``       the template selection
===================== ===============================================================

``templateContributions`` is deliberately NOT rewritten. It records which systems a
template contributed to *this* configuration; re-keying it onto the replacement
template would attribute one template's contributions to another, which is inventing
provenance rather than migrating a selection. A remapped template that had
contributions raises a warning instead, so the customer reviews the system selection
themselves.

Nothing historical is touched. Run records, findings, and evidence keep the pack they
were produced with (2.0-C1 T4), and per-org pack lifecycle rows (disable, version pin)
are the customer's own dimension and are left alone — a stale pin on a pack that is no
longer selected is inert, and clearing it would erase a decision the customer made.

Template remapping is conservative
----------------------------------
A template is registry-owned and declares its pack, so a template selection can only
be migrated to another *registered* template that declares the replacement pack. The
resolution follows the same discipline as
``discovery/enterprise_apps/runtime_structure_resolution.py``:

* exactly one candidate → remap;
* zero candidates → left selected, reported as ``no_replacement_template``;
* two or more → left selected, reported as ``ambiguous_replacement_template``,
  naming them.

Never force-picking one of several is the whole point: guessing which template a
customer meant is how a migration quietly changes what their runs look for.

Failure posture
---------------
Preview never raises for an ordinary state — "this pack is not deprecated" and "no
replacement is declared" are answers, returned as ``available: false`` with a named
reason, because a UI has to explain them. WRITES do raise: an apply or revert that
did not do what the caller asked must never look like it succeeded (the same split as
``pack_state``). An apply with nothing to change is a no-op that writes no ledger row
and emits no audit event, mirroring ``pack_state``'s idempotent transitions.

The ledger is append-only
-------------------------
Applies and reverts are both appended; a revert never edits or removes the apply row
it undoes. "Has this migration been reverted?" is DERIVED from whether a later revert
references it, so the trail can always answer "what did this org do, and when",
which is what AT-846 builds its audit view on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# ── Surfaces ──────────────────────────────────────────────────────────────────

#: The org's saved Stack Builder setup state — the persisted run configuration.
SURFACE_SETUP_STATE = "stack_builder_setup_state"

#: The setup-state fields that ARE a pack selection.
PACK_SELECTION_FIELDS: Tuple[str, ...] = ("packId", "packIds")
#: …and the ones that are a template selection.
TEMPLATE_SELECTION_FIELDS: Tuple[str, ...] = ("templateId", "templateIds")

#: KV namespace for the append-only migration ledger.
MIGRATION_LEDGER_PREFIX = "pack_migrations"


# ── Why a migration is not available ──────────────────────────────────────────

#: The pack is not deprecated, so there is nothing to migrate away from.
UNAVAILABLE_NOT_DEPRECATED = "not_deprecated"
#: Deprecated, but no (registered) replacement pack was named — AT-842 drops a
#: replacement that is not a registered pack, so "no path yet" is the honest answer.
UNAVAILABLE_NO_REPLACEMENT = "no_replacement_declared"


# ── Why a reference could not be mapped ───────────────────────────────────────

#: No registered template declares the replacement pack.
UNMAPPED_NO_REPLACEMENT_TEMPLATE = "no_replacement_template"
#: Several registered templates declare it; picking one would be a guess.
UNMAPPED_AMBIGUOUS_TEMPLATE = "ambiguous_replacement_template"


# ── Advisory warnings on a plan ───────────────────────────────────────────────

#: The replacement pack is DISABLED for this org, so the migrated configuration
#: would still produce nothing until it is re-enabled (2.0-C1 T2).
WARNING_REPLACEMENT_DISABLED = "replacement_pack_disabled"
#: The replacement pack fails the 2.0-C1 T1 compatibility gate on this platform.
WARNING_REPLACEMENT_INCOMPATIBLE = "replacement_pack_incompatible"
#: A remapped template had recorded contributions that are NOT carried across.
WARNING_TEMPLATE_CONTRIBUTIONS = "template_contributions_need_review"
#: The deprecated pack's grace period has already ended (AT-845 disables it).
WARNING_GRACE_EXPIRED = "grace_period_expired"
#: The deprecation declaration itself carries named defects (AT-842 issues).
WARNING_DECLARATION_ISSUES = "deprecation_declaration_issues"


# ── Ledger record kinds ───────────────────────────────────────────────────────

RECORD_APPLY = "apply"
RECORD_REVERT = "revert"


# ── Errors ────────────────────────────────────────────────────────────────────


class PackMigrationUnavailable(ValueError):
    """There is no migration to apply — not deprecated, or no replacement named."""


class PackMigrationConflict(ValueError):
    """The configuration is not in the state the caller's request assumed.

    Raised when a previewed plan no longer matches, when a revert target has already
    been reverted, and when the configuration has been edited since the migration was
    applied (so reverting would clobber the newer edit). Every case maps to a 409.
    """


class PackMigrationNotFound(LookupError):
    """No migration with this id exists for this org."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _string_list(value: Any) -> Optional[List[str]]:
    """A list of non-empty strings, or ``None`` when the value is not one.

    Returning ``None`` rather than ``[]`` keeps "the field is absent or a shape we do
    not recognise" distinct from "the field is an empty list" — the first must be left
    alone, the second is simply nothing to migrate.
    """
    if not isinstance(value, list):
        return None
    return [str(item).strip() for item in value if str(item or "").strip()]


def _remap_list(values: List[str], mapping: Dict[str, str]) -> List[str]:
    """Apply ``mapping`` order-preservingly, collapsing a duplicate it creates.

    Migrating ``[cloud_ops, service_cloud]`` with ``cloud_ops → service_cloud`` must
    yield ``[service_cloud]``, not the same id twice.
    """
    out: List[str] = []
    for value in values:
        mapped = mapping.get(value, value)
        if mapped not in out:
            out.append(mapped)
    return out


# ── Plan shapes ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationChange:
    """One field rewrite, carrying BOTH values so it can be undone exactly.

    ``previous_value``/``new_value`` are whole field values rather than a diff. That
    makes apply, revert, and conflict detection all a single comparison, and it means
    a revert restores the customer's configuration verbatim instead of reconstructing
    something equivalent-looking.
    """

    surface: str
    field: str
    previous_value: Any
    new_value: Any
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "surface": self.surface,
            "field": self.field,
            "previousValue": self.previous_value,
            "newValue": self.new_value,
            "description": self.description,
        }

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> "MigrationChange":
        return MigrationChange(
            surface=str(payload.get("surface") or ""),
            field=str(payload.get("field") or ""),
            previous_value=payload.get("previousValue"),
            new_value=payload.get("newValue"),
            description=str(payload.get("description") or ""),
        )


@dataclass(frozen=True)
class UnmappedReference:
    """A reference the migration deliberately did NOT rewrite, and why.

    Reported rather than silently skipped: a customer told "3 changes applied" who is
    not told that a template still points at the old pack has been given a false
    picture of their configuration.
    """

    surface: str
    field: str
    value: str
    reason: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "surface": self.surface,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MigrationWarning:
    """Something true about the migration that the customer should know first."""

    code: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class MigrationPlan:
    """What a migration WOULD do. Computed without writing anything."""

    org_id: str
    pack_id: str
    pack_name: str
    replacement_pack_id: str
    replacement_pack_name: str
    available: bool
    #: Human sentence explaining why no migration is available. Empty when one is.
    reason: str = ""
    #: The same thing as a machine-readable code, so a surface can branch on WHY
    #: without matching on prose. Empty when a migration IS available.
    reason_code: str = ""
    changes: List[MigrationChange] = field(default_factory=list)
    unmapped: List[UnmappedReference] = field(default_factory=list)
    warnings: List[MigrationWarning] = field(default_factory=list)
    #: The AT-843 compact notice for the deprecated pack, so a surface previewing a
    #: migration renders the SAME sentence it showed on the pack picker.
    deprecation: Optional[Dict[str, Any]] = None
    evaluated_on: str = ""

    @property
    def applicable(self) -> bool:
        """True when applying would actually change something."""
        return self.available and bool(self.changes)

    @property
    def fingerprint(self) -> str:
        """Stable digest of the exact change set this plan describes.

        A caller may hand it back on apply; a mismatch means the configuration or the
        declaration moved between preview and confirmation, and the apply is refused
        rather than applying a change the customer never saw.
        """
        return hashlib.sha256(
            _canonical(
                {
                    "packId": self.pack_id,
                    "replacementPackId": self.replacement_pack_id,
                    "changes": [change.to_dict() for change in self.changes],
                }
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "orgId": self.org_id,
            "packId": self.pack_id,
            "packName": self.pack_name,
            "replacementPackId": self.replacement_pack_id,
            "replacementPackName": self.replacement_pack_name,
            "available": self.available,
            "applicable": self.applicable,
            "reason": self.reason,
            "reasonCode": self.reason_code,
            "changes": [change.to_dict() for change in self.changes],
            "unmapped": [item.to_dict() for item in self.unmapped],
            "warnings": [item.to_dict() for item in self.warnings],
            "deprecation": self.deprecation,
            "evaluatedOn": self.evaluated_on,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class MigrationRecord:
    """One ledger entry — an apply, or the revert of one.

    ``reverted`` is DERIVED (see :func:`_hydrate`) from whether a later revert
    references this record, so nothing has to mutate the original row for the trail
    to stay truthful.
    """

    id: str
    kind: str
    org_id: str
    pack_id: str
    replacement_pack_id: str
    changes: List[MigrationChange]
    actor_id: str
    at: str
    reason: Optional[str] = None
    unmapped: List[UnmappedReference] = field(default_factory=list)
    warnings: List[MigrationWarning] = field(default_factory=list)
    fingerprint: str = ""
    reverts_migration_id: Optional[str] = None
    reverted: bool = False
    reverted_at: Optional[str] = None
    reverted_by: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "orgId": self.org_id,
            "packId": self.pack_id,
            "replacementPackId": self.replacement_pack_id,
            "changes": [change.to_dict() for change in self.changes],
            "unmapped": [item.to_dict() for item in self.unmapped],
            "warnings": [item.to_dict() for item in self.warnings],
            "reason": self.reason,
            "actorId": self.actor_id,
            "at": self.at,
            "fingerprint": self.fingerprint,
            "revertsMigrationId": self.reverts_migration_id,
            "reverted": self.reverted,
            "revertedAt": self.reverted_at,
            "revertedBy": self.reverted_by,
            "changed": self.changed,
        }


# ── Store contract ────────────────────────────────────────────────────────────


class PackMigrationStore:
    """Read/write contract for the migrated config and the ledger.

    Deliberately narrow — the migration logic never touches storage directly, so the
    whole preview/apply/revert cycle is exercisable with no database (the same
    injection seam as ``pack_state``/``runbook_match_decisions``).

    There is no delete operation, on either half.
    """

    def read_setup_state(self, org_id: str) -> Optional[Dict[str, Any]]:
        """The org's saved setup state, or ``None`` when it has never saved one."""
        raise NotImplementedError

    def write_setup_state(self, org_id: str, state: Dict[str, Any]) -> None:
        """Replace the saved setup state, preserving its storage envelope."""
        raise NotImplementedError

    def records(self, org_id: str) -> List[Dict[str, Any]]:
        """Every ledger row for this org, oldest first (insertion order)."""
        raise NotImplementedError

    def append_record(self, org_id: str, record: Dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryPackMigrationStore(PackMigrationStore):
    """Thread-safe contract implementation for offline runs and tests."""

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, Any]] = {}
        self._ledger: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def seed_setup_state(self, org_id: str, state: Dict[str, Any]) -> None:
        """Test convenience — put a saved configuration in place."""
        with self._lock:
            self._states[_required(org_id, "org_id")] = json.loads(json.dumps(state))

    def read_setup_state(self, org_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._states.get(_required(org_id, "org_id"))
        return json.loads(json.dumps(state)) if state is not None else None

    def write_setup_state(self, org_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            self._states[_required(org_id, "org_id")] = json.loads(json.dumps(state))

    def records(self, org_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(row))
                for row in self._ledger.get(_required(org_id, "org_id"), [])
            ]

    def append_record(self, org_id: str, record: Dict[str, Any]) -> None:
        with self._lock:
            self._ledger.setdefault(_required(org_id, "org_id"), []).append(
                json.loads(json.dumps(record))
            )


class KvPackMigrationStore(PackMigrationStore):
    """Production store, on the existing ``kv`` table.

    Both halves live in ``kv`` because that is already where the org's setup state
    lives (``routes_stack_builder``), and because ``kv`` is in
    ``history_retention.PROTECTED_TABLES`` — the ledger inherits the never-delete
    guarantee without a new table, and therefore without a migration that would
    collide with the concurrently-developed 2.0-C3 schema work.
    """

    def _setup_state_key(self, org_id: str) -> str:
        # Imported lazily and from the module that OWNS this KV namespace, so the key
        # format has exactly one definition rather than a copy that can drift.
        from .routes_stack_builder import setup_state_key

        return setup_state_key(org_id)

    def _ledger_key(self, org_id: str) -> str:
        return f"{MIGRATION_LEDGER_PREFIX}:{org_id}"

    def read_setup_state(self, org_id: str) -> Optional[Dict[str, Any]]:
        from . import db

        envelope = db.kv_get(self._setup_state_key(_required(org_id, "org_id")))
        if not isinstance(envelope, dict):
            return None
        state = envelope.get("state")
        return state if isinstance(state, dict) else None

    def write_setup_state(self, org_id: str, state: Dict[str, Any]) -> None:
        from . import db

        org = _required(org_id, "org_id")
        key = self._setup_state_key(org)
        envelope = db.kv_get(key)
        if not isinstance(envelope, dict):
            # Nothing to migrate into. Creating a configuration the customer never
            # saved would be inventing one, so this is a no-op by construction —
            # the planner only produces changes when a state was read.
            logger.warning(
                "No saved setup state for org=%s; migration write skipped", org
            )
            return
        envelope["state"] = state
        db.kv_set(key, envelope)

    def records(self, org_id: str) -> List[Dict[str, Any]]:
        from . import db

        rows = db.kv_get(self._ledger_key(_required(org_id, "org_id")))
        return list(rows) if isinstance(rows, list) else []

    def append_record(self, org_id: str, record: Dict[str, Any]) -> None:
        from . import db

        org = _required(org_id, "org_id")
        key = self._ledger_key(org)
        rows = self.records(org)
        rows.append(record)
        db.kv_set(key, rows)


_STORE: Optional[PackMigrationStore] = None


def get_pack_migration_store() -> PackMigrationStore:
    global _STORE
    if _STORE is None:
        _STORE = KvPackMigrationStore()
    return _STORE


def set_pack_migration_store(store: Optional[PackMigrationStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


# ── Preview ───────────────────────────────────────────────────────────────────


def preview_migration(
    org_id: str,
    pack_id: str,
    *,
    as_of: Optional[date] = None,
) -> MigrationPlan:
    """Compute the org-config migration for a deprecated pack. Writes nothing.

    Returns an ``available: false`` plan with a named reason when the pack is not
    deprecated or names no registered replacement — those are answers a surface has to
    explain, not errors. Raises only for a genuinely unknown pack id.

    ``as_of`` injects the evaluation date so grace-phase assertions stay deterministic,
    exactly as ``pack_deprecation`` and the certification expiry tests do.
    """
    from discovery.packs.pack_config import PACK_REGISTRY, get_pack
    from discovery.packs.pack_deprecation import (
        deprecation_notice,
        get_pack_deprecation,
    )

    from .pack_state import PackNotFound

    org = _required(org_id, "org_id")
    pack = _required(pack_id, "pack_id")
    if pack not in PACK_REGISTRY:
        # Strict, like `pack_state`: `get_pack()` resolves an unknown id to the
        # DEFAULT pack, so a typo would otherwise preview a migration off a pack the
        # caller never named.
        raise PackNotFound(f"unknown pack '{pack}'")

    deprecation = get_pack_deprecation(pack, as_of=as_of)
    pack_name = deprecation.pack_name or get_pack(pack).get("packName", pack)

    def _unavailable(reason_code: str, detail: str) -> MigrationPlan:
        return MigrationPlan(
            org_id=org,
            pack_id=pack,
            pack_name=pack_name,
            replacement_pack_id=deprecation.replacement_pack_id,
            replacement_pack_name=deprecation.replacement_pack_name,
            available=False,
            reason=detail,
            reason_code=reason_code,
            deprecation=deprecation_notice(pack, as_of=as_of),
            evaluated_on=deprecation.evaluated_on,
        )

    if not deprecation.deprecated:
        return _unavailable(
            UNAVAILABLE_NOT_DEPRECATED,
            f"Pack '{pack}' is not deprecated, so there is nothing to migrate.",
        )
    if not deprecation.has_replacement:
        return _unavailable(
            UNAVAILABLE_NO_REPLACEMENT,
            f"Pack '{pack}' is deprecated but names no registered replacement pack, "
            f"so no migration can be offered.",
        )

    replacement = deprecation.replacement_pack_id
    state = get_pack_migration_store().read_setup_state(org) or {}
    changes, unmapped, warnings = _plan_setup_state_changes(
        state, pack, replacement
    )
    warnings.extend(_replacement_warnings(org, replacement, deprecation))

    return MigrationPlan(
        org_id=org,
        pack_id=pack,
        pack_name=pack_name,
        replacement_pack_id=replacement,
        replacement_pack_name=deprecation.replacement_pack_name,
        available=True,
        reason="",
        changes=changes,
        unmapped=unmapped,
        warnings=warnings,
        deprecation=deprecation_notice(pack, as_of=as_of),
        evaluated_on=deprecation.evaluated_on,
    )


def _plan_setup_state_changes(
    state: Dict[str, Any], pack_id: str, replacement_id: str
) -> Tuple[List[MigrationChange], List[UnmappedReference], List[MigrationWarning]]:
    """The field-level change set for the saved setup state."""
    changes: List[MigrationChange] = []
    pack_mapping = {pack_id: replacement_id}
    template_mapping, unmapped, warnings = _template_mapping(
        state, pack_id, replacement_id
    )

    for field_name in PACK_SELECTION_FIELDS:
        change = _plan_field(
            state, field_name, pack_mapping,
            f"Pack selection moves from '{pack_id}' to '{replacement_id}'.",
        )
        if change is not None:
            changes.append(change)

    for field_name in TEMPLATE_SELECTION_FIELDS:
        change = _plan_field(
            state, field_name, template_mapping,
            "Template selection moves to the template that declares "
            f"'{replacement_id}'.",
        )
        if change is not None:
            changes.append(change)

    return changes, unmapped, warnings


def _plan_field(
    state: Dict[str, Any],
    field_name: str,
    mapping: Dict[str, str],
    description: str,
) -> Optional[MigrationChange]:
    """One field's change, or ``None`` when the field does not need rewriting.

    Scalar and list forms are handled independently rather than deriving one from the
    other, which is what keeps ``packId``/``packIds`` (and the template pair) mutually
    consistent afterwards without this function needing to know they are a pair: the
    same mapping applied to both preserves whatever relationship they had.
    """
    if not mapping or field_name not in state:
        return None
    current = state.get(field_name)

    if isinstance(current, str):
        mapped = mapping.get(current.strip())
        if not mapped or mapped == current:
            return None
        return MigrationChange(
            SURFACE_SETUP_STATE, field_name, current, mapped, description
        )

    values = _string_list(current)
    if values is None:
        return None
    mapped_values = _remap_list(values, mapping)
    if mapped_values == values:
        return None
    return MigrationChange(
        SURFACE_SETUP_STATE, field_name, current, mapped_values, description
    )


def _template_mapping(
    state: Dict[str, Any], pack_id: str, replacement_id: str
) -> Tuple[Dict[str, str], List[UnmappedReference], List[MigrationWarning]]:
    """Resolve each selected template that declares the deprecated pack.

    Conservative by construction (see the module docstring): exactly one candidate
    remaps, zero or several are left alone and reported.
    """
    from discovery.packs.template_registry import get_template, list_templates

    selected = _selected_template_ids(state)
    if not selected:
        return {}, [], []

    candidates = sorted(
        defn.template_id
        for defn in list_templates()
        if defn.pack_id == replacement_id
    )
    contributions = state.get("templateContributions")
    contributions = contributions if isinstance(contributions, dict) else {}

    mapping: Dict[str, str] = {}
    unmapped: List[UnmappedReference] = []
    warnings: List[MigrationWarning] = []

    for template_id in selected:
        defn = get_template(template_id)
        if defn is None or defn.pack_id != pack_id:
            continue
        if len(candidates) == 1:
            mapping[template_id] = candidates[0]
            if contributions.get(template_id):
                warnings.append(
                    MigrationWarning(
                        WARNING_TEMPLATE_CONTRIBUTIONS,
                        f"Template '{template_id}' contributed system selections that "
                        f"are not carried over to '{candidates[0]}'. Review the "
                        f"selected systems after migrating.",
                    )
                )
            continue
        unmapped.append(
            UnmappedReference(
                SURFACE_SETUP_STATE,
                "templateIds",
                template_id,
                UNMAPPED_NO_REPLACEMENT_TEMPLATE
                if not candidates
                else UNMAPPED_AMBIGUOUS_TEMPLATE,
                f"No registered template declares pack '{replacement_id}', so "
                f"template '{template_id}' is left selected."
                if not candidates
                else f"Templates {', '.join(candidates)} all declare pack "
                f"'{replacement_id}'; template '{template_id}' is left selected "
                f"rather than guessing which was intended.",
            )
        )
    return mapping, unmapped, warnings


def _selected_template_ids(state: Dict[str, Any]) -> List[str]:
    """The org's template selection, from either the list or the scalar alias."""
    ids = _string_list(state.get("templateIds")) or []
    scalar = state.get("templateId")
    if isinstance(scalar, str) and scalar.strip() and scalar.strip() not in ids:
        ids.append(scalar.strip())
    return ids


def _replacement_warnings(
    org_id: str, replacement_id: str, deprecation: Any
) -> List[MigrationWarning]:
    """Things that are true about the destination the customer should know first.

    All fail-soft: a lifecycle or compatibility read that fails omits its warning
    rather than blocking the migration. A missing advisory is a smaller harm than
    refusing a customer the path out of a deprecated pack.
    """
    warnings: List[MigrationWarning] = []

    if deprecation.grace_expired:
        warnings.append(
            MigrationWarning(
                WARNING_GRACE_EXPIRED,
                f"The grace period for '{deprecation.pack_id}' ended on "
                f"{deprecation.grace_ends_on}; it no longer runs.",
            )
        )
    if deprecation.issues:
        warnings.append(
            MigrationWarning(
                WARNING_DECLARATION_ISSUES,
                "The deprecation declaration has defects: "
                + ", ".join(deprecation.issues),
            )
        )

    try:
        from .pack_state import is_pack_disabled

        if is_pack_disabled(org_id, replacement_id):
            warnings.append(
                MigrationWarning(
                    WARNING_REPLACEMENT_DISABLED,
                    f"Replacement pack '{replacement_id}' is disabled for this "
                    f"organisation and will be excluded from runs until it is "
                    f"re-enabled.",
                )
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pack state for replacement '%s'", replacement_id,
            exc_info=True,
        )

    try:
        from discovery.packs.pack_compatibility import check_pack_compatibility

        compatibility = check_pack_compatibility(replacement_id)
        if not compatibility.compatible:
            warnings.append(
                MigrationWarning(
                    WARNING_REPLACEMENT_INCOMPATIBLE,
                    f"Replacement pack '{replacement_id}' cannot be activated on this "
                    f"platform: {compatibility.reason}",
                )
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not check compatibility for replacement '%s'", replacement_id,
            exc_info=True,
        )

    return warnings


# ── Apply ─────────────────────────────────────────────────────────────────────


def apply_migration(
    org_id: str,
    pack_id: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
    expected_fingerprint: Optional[str] = None,
    as_of: Optional[date] = None,
) -> MigrationRecord:
    """Apply the previewed migration to the org's saved configuration.

    Raises :class:`PackMigrationUnavailable` when there is no migration to make
    (not deprecated, or no registered replacement), and :class:`PackMigrationConflict`
    when ``expected_fingerprint`` does not match the plan as it stands now — the
    caller confirmed a change set, and applying a DIFFERENT one would defeat the
    preview entirely.

    A plan with no changes is a no-op: the store is untouched, no ledger row is
    written, and the returned record reports ``changed: false``. That mirrors
    ``pack_state``'s idempotent transitions, and it means re-applying a migration that
    has already run is safe rather than an error.
    """
    org = _required(org_id, "org_id")
    actor = _required(actor_id, "actor_id")
    plan = preview_migration(org, pack_id, as_of=as_of)

    if not plan.available:
        raise PackMigrationUnavailable(plan.reason)
    if expected_fingerprint and expected_fingerprint != plan.fingerprint:
        raise PackMigrationConflict(
            "The configuration changed since it was previewed. Preview the migration "
            "again and confirm the current change set."
        )

    now = _now()
    record = MigrationRecord(
        id=f"pmig_{uuid4().hex}",
        kind=RECORD_APPLY,
        org_id=org,
        pack_id=plan.pack_id,
        replacement_pack_id=plan.replacement_pack_id,
        changes=list(plan.changes),
        actor_id=actor,
        at=now,
        reason=reason,
        unmapped=list(plan.unmapped),
        warnings=list(plan.warnings),
        fingerprint=plan.fingerprint,
    )
    if not plan.changes:
        return record

    store = get_pack_migration_store()
    # Ledger FIRST, then the configuration. The two writes are not one transaction,
    # so the ordering decides which half-completed state a crash can leave behind,
    # and the two are not equally bad. Ledger-then-config can leave a recorded
    # migration that never happened — the preview still offers it, a revert attempt
    # refuses on the conflict guard, and re-applying fixes it. Config-then-ledger
    # would leave a migrated configuration with NO record to revert, which breaks
    # the one property (AC2) this whole operation exists to provide.
    store.append_record(org, record.to_dict())
    state = store.read_setup_state(org) or {}
    for change in plan.changes:
        state[change.field] = change.new_value
    store.write_setup_state(org, state)
    logger.info(
        "Applied pack migration %s for org=%s: %s → %s (%d change(s))",
        record.id, org, plan.pack_id, plan.replacement_pack_id, len(plan.changes),
    )
    return record


# ── Revert ────────────────────────────────────────────────────────────────────


def revert_migration(
    org_id: str,
    migration_id: str,
    *,
    actor_id: str,
    reason: Optional[str] = None,
    force: bool = False,
) -> MigrationRecord:
    """Restore the configuration this migration replaced (AC2's "reversible").

    Restores each change's recorded ``previous_value`` verbatim. It deliberately does
    NOT invert the pack mapping: a selection that pointed at the replacement before
    the migration must stay pointing at it.

    Refuses (:class:`PackMigrationConflict`) when the migration has already been
    reverted, and when a field no longer holds the value the migration wrote — the
    configuration has been edited since, and restoring the old value would silently
    discard that edit. ``force`` overrides the second case for a caller that has seen
    the conflict and decided.
    """
    org = _required(org_id, "org_id")
    actor = _required(actor_id, "actor_id")
    target = get_migration(org, migration_id)

    if target.kind != RECORD_APPLY:
        raise PackMigrationConflict(
            f"Migration '{target.id}' is a revert and cannot itself be reverted."
        )
    if target.reverted:
        raise PackMigrationConflict(
            f"Migration '{target.id}' was already reverted on {target.reverted_at}."
        )

    store = get_pack_migration_store()
    state = store.read_setup_state(org) or {}

    conflicts = [
        change.field
        for change in target.changes
        if state.get(change.field) != change.new_value
    ]
    if conflicts and not force:
        raise PackMigrationConflict(
            "The configuration has changed since this migration was applied ("
            + ", ".join(sorted(conflicts))
            + " no longer hold the migrated value). Reverting would discard that "
            "change; re-apply with force to restore the pre-migration values anyway."
        )

    now = _now()
    # Configuration FIRST here — the opposite order to apply, and for the same
    # reason: choose the survivable half. A revert row written before a failed
    # restore would report the configuration as put back when it is still migrated,
    # which is the one outcome that misleads. Restoring first and losing the row
    # leaves the configuration correct and the trail merely incomplete.
    for change in target.changes:
        state[change.field] = change.previous_value
    store.write_setup_state(org, state)

    record = MigrationRecord(
        id=f"pmig_{uuid4().hex}",
        kind=RECORD_REVERT,
        org_id=org,
        pack_id=target.pack_id,
        replacement_pack_id=target.replacement_pack_id,
        # Recorded from the revert's point of view, so this row can be read on its own:
        # it moved each field from the migrated value back to the original.
        changes=[
            MigrationChange(
                change.surface,
                change.field,
                change.new_value,
                change.previous_value,
                f"Reverted by migration '{target.id}'.",
            )
            for change in target.changes
        ],
        actor_id=actor,
        at=now,
        reason=reason,
        fingerprint=target.fingerprint,
        reverts_migration_id=target.id,
    )
    store.append_record(org, record.to_dict())
    logger.info(
        "Reverted pack migration %s for org=%s (revert record %s)",
        target.id, org, record.id,
    )
    return record


# ── Ledger reads ──────────────────────────────────────────────────────────────


def migration_history(org_id: str) -> List[MigrationRecord]:
    """Every migration for this org, NEWEST FIRST (the repo audit-list convention)."""
    org = _required(org_id, "org_id")
    rows = get_pack_migration_store().records(org)
    return list(reversed(_hydrate(rows)))


def get_migration(org_id: str, migration_id: str) -> MigrationRecord:
    """One migration by id. Raises :class:`PackMigrationNotFound` when absent."""
    org = _required(org_id, "org_id")
    wanted = _required(migration_id, "migration_id")
    for record in _hydrate(get_pack_migration_store().records(org)):
        if record.id == wanted:
            return record
    raise PackMigrationNotFound(f"unknown migration '{wanted}'")


def migration_history_safe(org_id: Optional[str]) -> List[MigrationRecord]:
    """:func:`migration_history`, degrading to ``[]`` on any read failure.

    For DISPLAY paths only. A ledger that cannot be read must not blank the pack
    picker; a write path never uses this.
    """
    if not org_id:
        return []
    try:
        return migration_history(org_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read pack migration history for org=%s", org_id, exc_info=True
        )
        return []


def _hydrate(rows: Iterable[Dict[str, Any]]) -> List[MigrationRecord]:
    """Ledger rows → records, deriving each apply's reverted state from the trail."""
    raw = list(rows)
    reverted: Dict[str, Dict[str, Any]] = {}
    for row in raw:
        target = row.get("revertsMigrationId")
        if row.get("kind") == RECORD_REVERT and target:
            reverted[str(target)] = row

    records: List[MigrationRecord] = []
    for row in raw:
        row_id = str(row.get("id") or "")
        undo = reverted.get(row_id)
        records.append(
            MigrationRecord(
                id=row_id,
                kind=str(row.get("kind") or RECORD_APPLY),
                org_id=str(row.get("orgId") or ""),
                pack_id=str(row.get("packId") or ""),
                replacement_pack_id=str(row.get("replacementPackId") or ""),
                changes=[
                    MigrationChange.from_dict(change)
                    for change in row.get("changes") or []
                ],
                actor_id=str(row.get("actorId") or ""),
                at=str(row.get("at") or ""),
                reason=row.get("reason"),
                unmapped=[
                    UnmappedReference(
                        surface=str(item.get("surface") or ""),
                        field=str(item.get("field") or ""),
                        value=str(item.get("value") or ""),
                        reason=str(item.get("reason") or ""),
                        detail=str(item.get("detail") or ""),
                    )
                    for item in row.get("unmapped") or []
                ],
                warnings=[
                    MigrationWarning(
                        code=str(item.get("code") or ""),
                        detail=str(item.get("detail") or ""),
                    )
                    for item in row.get("warnings") or []
                ],
                fingerprint=str(row.get("fingerprint") or ""),
                reverts_migration_id=row.get("revertsMigrationId"),
                reverted=undo is not None,
                reverted_at=str(undo.get("at")) if undo else None,
                reverted_by=str(undo.get("actorId")) if undo else None,
            )
        )
    return records


__all__ = [
    "InMemoryPackMigrationStore",
    "KvPackMigrationStore",
    "MIGRATION_LEDGER_PREFIX",
    "MigrationChange",
    "MigrationPlan",
    "MigrationRecord",
    "MigrationWarning",
    "PACK_SELECTION_FIELDS",
    "PackMigrationConflict",
    "PackMigrationNotFound",
    "PackMigrationStore",
    "PackMigrationUnavailable",
    "RECORD_APPLY",
    "RECORD_REVERT",
    "SURFACE_SETUP_STATE",
    "TEMPLATE_SELECTION_FIELDS",
    "UNAVAILABLE_NOT_DEPRECATED",
    "UNAVAILABLE_NO_REPLACEMENT",
    "UNMAPPED_AMBIGUOUS_TEMPLATE",
    "UNMAPPED_NO_REPLACEMENT_TEMPLATE",
    "UnmappedReference",
    "WARNING_DECLARATION_ISSUES",
    "WARNING_GRACE_EXPIRED",
    "WARNING_REPLACEMENT_DISABLED",
    "WARNING_REPLACEMENT_INCOMPATIBLE",
    "WARNING_TEMPLATE_CONTRIBUTIONS",
    "apply_migration",
    "get_migration",
    "get_pack_migration_store",
    "migration_history",
    "migration_history_safe",
    "preview_migration",
    "revert_migration",
    "set_pack_migration_store",
]
