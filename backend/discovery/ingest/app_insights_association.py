"""
app_insights_association.py — 2.0-D3 T3: IIS / .NET / CMDB association.

Answers one question about an Application Insights signal: **which application
component or configuration item is actually affected?** An availability alert on
"checkout-api" is far more actionable when AgentIQ can say it is the .NET
application the customer already configured, or the ServiceNow CI the CMDB already
knows.

The whole module exists to make that answer TRUSTWORTHY, which means it is defined
as much by what it refuses to do as by what it does.

The no-inference rule
---------------------
An association is created ONLY from an exact, operator-supplied reference. Never
from a similar name, a URL, a hostname, an IIS site name, a resource group, an
owner, an environment, a tag, or two events happening at similar times. "Orders
API" existing in both Application Insights and the CMDB is **not evidence** that
they are the same thing — it is the single most tempting wrong answer here, because
it is right often enough to look like it works and wrong often enough to make
AgentIQ blame the wrong system during an incident.

So the only inputs are stable identifiers that a human already committed to:

* ``DotNetAppTarget.app_id`` — the identity the existing .NET ingestion
  (``dotnet_app_config``) already uses for each configured application.
* The ServiceNow ``sys_id`` — the identity MSP-B3's CMDB integration already
  stores as ``source_record_id`` on its ``system`` entities.

Display names are never association inputs. In particular the CMDB lookup passes
**no** ``display_name`` to ``lookup_resolved_entity``: that function falls back to
canonical-name matching when an id yields nothing, which would smuggle exactly the
name-based association this task forbids. A test pins that we never pass one.

Everything ambiguous stays unassociated
---------------------------------------
Missing, conflicting, duplicate, cross-organisation and ambiguous references all
resolve to **no association**, each with a named reason so an operator can see why
their configuration did not take effect. Notably a component with MORE THAN ONE
configuration entry is refused outright, even if the entries agree — a config that
names one component twice is ambiguous about intent, and guessing which entry was
meant is exactly the class of decision this module exists not to make.

Where the result goes
---------------------
Onto the record WRAPPER, nested inside the existing ``app_insights`` block — never
into the canonical MSP-B0 event. The event's identity (and therefore its
deterministic ``event_signature`` and its transport equivalence with every other
operational source) must not depend on whether a customer happens to have
configured an association. An otherwise valid event is always ingested; the
association is additive information, never a gate.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from . import is_live
except Exception:  # pragma: no cover - import shim
    from discovery.ingest import is_live

from .operational_config import find_inline_secret_keys

logger = logging.getLogger(__name__)


class AppInsightsAssociationConfigError(ValueError):
    """Raised for a malformed association configuration source."""


#: Live configuration: a JSON object keyed by org id (with a ``default``/``*``
#: fallback) OR a plain JSON array applied to every org — the same shape
#: ``ENTERPRISE_APP_REPOS`` uses. Non-secret by construction: an association names
#: identifiers, never credentials.
CONFIG_ENV = "APP_INSIGHTS_ASSOCIATIONS"

#: Offline / default source, so an offline run is deterministic and needs no env.
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_insights_associations_sample.json"

_DEFAULT_ORG_KEYS: Tuple[str, ...] = ("default", "*")

#: Association target types (closed vocabulary).
TARGET_DOTNET_APP = "dotnet_app"
TARGET_CMDB_CI = "cmdb_ci"
TARGET_TYPES = frozenset({TARGET_DOTNET_APP, TARGET_CMDB_CI})

#: Named reasons a configured reference did not produce an association. Stable
#: strings, so run health / a future UI can explain the outcome without parsing
#: prose.
REASON_DUPLICATE_CONFIG = "duplicate_configuration"
REASON_NO_TARGETS = "no_target_declared"
REASON_DOTNET_NOT_CONFIGURED = "dotnet_app_id_not_configured"
REASON_CMDB_NOT_RESOLVED = "cmdb_sys_id_not_resolved"
REASON_CROSS_ORG = "cross_organization_reference"

#: The ServiceNow entity shape MSP-B3's CMDB ingestion writes: a ``system`` entity
#: whose ``source_record_id`` is the CI's ``sys_id`` (see
#: ``app/entity_extractor.py`` CMDB CI upsert). Spelled here once so the lookup
#: cannot drift from the producer.
CMDB_ENTITY_TYPE = "system"
CMDB_SOURCE_SYSTEM = "servicenow"


# ── configuration ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AssociationConfigEntry:
    """One operator-declared association for one App Insights component.

    ``component_id`` is the Application Insights component ARM resource id — the
    key, because that is the identity D3's mapper already resolves for every
    in-scope signal. Either target may be given, or both; neither is a
    configuration error, it simply declares nothing to associate.
    """

    component_id: str
    dotnet_app_id: Optional[str] = None
    cmdb_ci_sys_id: Optional[str] = None
    org_id: Optional[str] = None          # optional explicit org guard
    notes: Optional[str] = None

    @property
    def declares_a_target(self) -> bool:
        return bool(self.dotnet_app_id or self.cmdb_ci_sys_id)


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _coerce_entry(entry: Dict[str, Any]) -> AssociationConfigEntry:
    """Build a config entry from a raw dict, rejecting inline secrets.

    An association carries identifiers only, so a credential-looking field is a
    configuration error rather than something to quietly ignore — the same
    shared guard the operational-app targets use, so the rule cannot drift.
    """
    offending = find_inline_secret_keys(entry)
    if offending:
        raise AppInsightsAssociationConfigError(
            f"association entry for component "
            f"'{entry.get('component_id', '?')}' contains inline credential "
            f"field(s) {sorted(offending)} — associations carry identifiers only"
        )
    component_id = _clean(entry.get("component_id"))
    if not component_id:
        raise AppInsightsAssociationConfigError(
            "association entry must declare a non-empty component_id"
        )
    return AssociationConfigEntry(
        component_id=component_id,
        dotnet_app_id=_clean(entry.get("dotnet_app_id")),
        cmdb_ci_sys_id=_clean(entry.get("cmdb_ci_sys_id")),
        org_id=_clean(entry.get("org_id")),
        notes=_clean(entry.get("notes")),
    )


def _select_org_entries(parsed: Any, org_id: str) -> List[Dict[str, Any]]:
    """Pick the raw entries for ``org_id``.

    An array applies to every org; an object is org-keyed with a ``default``/``*``
    fallback. A top-level scalar key such as ``_comment`` is naturally ignored
    because only keys whose value is a list are considered.
    """
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        for key in (org_id, *_DEFAULT_ORG_KEYS):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                return [e for e in candidate if isinstance(e, dict)]
    return []


def _raw_entries(org_id: str) -> List[Dict[str, Any]]:
    """Raw association entries for an org — configuration only, never discovery."""
    if not is_live():
        if not FIXTURE_PATH.exists():
            return []
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return _select_org_entries(data, org_id)

    raw = os.getenv(CONFIG_ENV, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AppInsightsAssociationConfigError(
            f"{CONFIG_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    return _select_org_entries(parsed, org_id)


def load_association_config(org_id: str) -> Dict[str, List[AssociationConfigEntry]]:
    """Load the org's association config, indexed by NORMALISED component id.

    The index maps to a LIST rather than a single entry on purpose: a component
    declared more than once must be detectable so it can be refused. Normalisation
    is a lower-case fold of the ARM resource id, which is identity (Azure resource
    ids are case-insensitive) — not name matching.

    A single malformed or credential-bearing entry is skipped and logged by
    component id / offending key, never by value, so one bad entry never blocks the
    rest (the project's "degrade, don't crash" configuration rule).
    """
    index: Dict[str, List[AssociationConfigEntry]] = {}
    for raw in _raw_entries(org_id):
        try:
            entry = _coerce_entry(raw)
        except AppInsightsAssociationConfigError as exc:
            logger.warning(
                "app_insights_association: skipping invalid entry (org=%s): %s",
                org_id, exc,
            )
            continue
        index.setdefault(entry.component_id.strip().lower(), []).append(entry)
    return index


# ── the association result ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppInsightsAssociation:
    """A resolved association from an App Insights component to a known target.

    ``evidence`` names the explicit configuration that produced it — which config
    key, which declared identifier, and how it was resolved — so every association
    is traceable back to a human decision rather than to a heuristic.
    """

    target_type: str
    target_id: str
    org_id: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "org_id": self.org_id,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AssociationOutcome:
    """Everything resolution decided for one component: hits and named misses."""

    associations: Tuple[AppInsightsAssociation, ...] = ()
    unresolved: Tuple[Dict[str, Any], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.associations and not self.unresolved

    def to_wrapper(self) -> Dict[str, Any]:
        """The additive record-wrapper fragment.

        Both keys are omitted when they have nothing to say, so a component with no
        configured association produces exactly the wrapper shape it produced
        before T3 — the association can only ever ADD information.
        """
        out: Dict[str, Any] = {}
        if self.associations:
            out["associations"] = [a.to_dict() for a in self.associations]
        if self.unresolved:
            out["association_unresolved"] = [dict(u) for u in self.unresolved]
        return out


# ── target resolvers ────────────────────────────────────────────────────────────

#: Resolves a declared .NET ``app_id`` to the configured target's id, or ``None``.
DotNetResolver = Callable[[str], Optional[str]]
#: Resolves a declared CMDB ``sys_id`` to a confident CI identity, or ``None``.
CmdbResolver = Callable[[str], Optional[Dict[str, Any]]]


def build_dotnet_resolver(org_id: str, *, targets: Optional[Sequence[Any]] = None) -> DotNetResolver:
    """Resolve a declared ``app_id`` against the CONFIGURED .NET targets.

    Uses the existing ``dotnet_app_config.load_targets`` identity set, so an
    association can only point at an application the customer already declared to
    AgentIQ. An id that is not in that set resolves to ``None`` — a reference to an
    application we do not ingest is not an association, it is a typo.

    Matching is exact on ``app_id`` (no case folding, no trimming beyond the
    config loader's own): ``app_id`` is a chosen identifier, and quietly accepting
    a near-miss would be inference by another name.
    """
    def _load() -> Sequence[Any]:
        if targets is not None:
            return targets
        try:
            from .dotnet_app_config import load_targets
            return load_targets(org_id)
        except Exception:  # noqa: BLE001 — no targets configured → nothing to resolve
            logger.debug(
                "app_insights_association: .NET targets unavailable for org=%s",
                org_id, exc_info=True,
            )
            return []

    def _resolve(app_id: str) -> Optional[str]:
        wanted = str(app_id or "")
        if not wanted:
            return None
        for target in _load():
            if getattr(target, "app_id", None) == wanted:
                return wanted
        return None

    return _resolve


def build_cmdb_resolver(org_id: str, *, lookup: Optional[Callable[..., Any]] = None) -> CmdbResolver:
    """Resolve a declared CMDB ``sys_id`` through the org-scoped identity lookup.

    Uses the EXISTING conservative ``app.entity_resolution.lookup_resolved_entity``,
    which is the same read-only, side-effect-free, org-scoped lookup R18-A4 uses for
    conversation participants. It returns a CI only when exactly ONE confidently
    ``resolved`` row carries that ``sys_id``; several rows (ambiguous) or none both
    yield ``None``.

    **No ``display_name`` is ever passed.** That parameter makes the lookup fall
    back to canonical-name matching when the id finds nothing, which is precisely
    the name-based association this task forbids — so the sys_id is the only signal
    that can ever produce a CMDB association.

    Guarded and a no-op when no database is configured, so an offline run never
    touches a DB and a lookup failure can only mean "no association", never a
    broken ingest.
    """
    def _resolve(sys_id: str) -> Optional[Dict[str, Any]]:
        wanted = str(sys_id or "").strip()
        if not wanted:
            return None
        fn = lookup
        if fn is None:
            if not os.getenv("DATABASE_URL"):
                return None
            try:
                from app.entity_resolution import lookup_resolved_entity as fn  # type: ignore
            except Exception:  # pragma: no cover — entity layer unavailable
                return None
        try:
            entity = fn(
                org_id=org_id,
                entity_type=CMDB_ENTITY_TYPE,
                source_system=CMDB_SOURCE_SYSTEM,
                source_record_id=wanted,
                # display_name deliberately omitted — see the docstring.
            )
        except Exception:  # noqa: BLE001 — a lookup failure just means "no association"
            logger.debug(
                "app_insights_association: CMDB lookup failed for org=%s", org_id,
                exc_info=True,
            )
            return None
        if entity is None:
            return None
        # Belt-and-braces: the lookup is org-scoped, but an association that
        # crossed an org boundary would be the worst possible defect here, so the
        # returned row's own org is checked before it is trusted.
        entity_org = getattr(entity, "org_id", None)
        if entity_org is not None and str(entity_org) != str(org_id):
            logger.warning(
                "app_insights_association: refusing cross-org CMDB association "
                "(lookup for org=%s returned an entity owned by another org)",
                org_id,
            )
            return None
        return {
            "sys_id": wanted,
            "entity_id": str(getattr(entity, "id", "")) or None,
            "display_name": getattr(entity, "display_name", None),
            "resolution_status": getattr(entity, "resolution_status", None),
        }

    return _resolve


# ── the resolver ────────────────────────────────────────────────────────────────


class AppInsightsAssociationResolver:
    """Resolves an App Insights component to its explicitly-configured targets.

    Constructed once per ingestor. Both target resolvers are injectable, so the
    whole decision table is testable with no database and no configured estate.
    """

    def __init__(
        self,
        org_id: str,
        *,
        config: Optional[Dict[str, List[AssociationConfigEntry]]] = None,
        dotnet_resolver: Optional[DotNetResolver] = None,
        cmdb_resolver: Optional[CmdbResolver] = None,
    ) -> None:
        self.org_id = str(org_id)
        if config is None:
            try:
                config = load_association_config(self.org_id)
            except AppInsightsAssociationConfigError:
                logger.warning(
                    "app_insights_association: unusable configuration for org=%s "
                    "— no associations will be made",
                    self.org_id, exc_info=True,
                )
                config = {}
        self._config = config
        self._dotnet = dotnet_resolver or build_dotnet_resolver(self.org_id)
        self._cmdb = cmdb_resolver or build_cmdb_resolver(self.org_id)

    @property
    def has_configuration(self) -> bool:
        return bool(self._config)

    def resolve(self, component_id: Optional[str]) -> AssociationOutcome:
        """The associations for one App Insights component.

        Returns an EMPTY outcome when nothing is configured for the component —
        the common case, and deliberately silent: an unconfigured component is not
        a problem to report, it is simply a component whose owner has not declared
        an association.
        """
        key = str(component_id or "").strip().lower()
        if not key:
            return AssociationOutcome()
        entries = self._config.get(key) or []
        if not entries:
            return AssociationOutcome()

        if len(entries) > 1:
            # Refused even if the entries agree: a config naming one component
            # twice is ambiguous about intent, and picking one would be a guess.
            logger.warning(
                "app_insights_association: %d configuration entries for component "
                "%s (org=%s) — refusing to associate (ambiguous configuration)",
                len(entries), component_id, self.org_id,
            )
            return AssociationOutcome(
                unresolved=({
                    "reason": REASON_DUPLICATE_CONFIG,
                    "component_id": component_id,
                    "entry_count": len(entries),
                },),
            )

        entry = entries[0]

        if entry.org_id is not None and entry.org_id != self.org_id:
            # An entry that names an org explicitly must match this one. Belt to
            # the org-keyed config's braces.
            logger.warning(
                "app_insights_association: configuration entry for component %s "
                "declares org %s but is being resolved for org %s — refusing",
                component_id, entry.org_id, self.org_id,
            )
            return AssociationOutcome(
                unresolved=({
                    "reason": REASON_CROSS_ORG,
                    "component_id": component_id,
                },),
            )

        if not entry.declares_a_target:
            return AssociationOutcome(
                unresolved=({
                    "reason": REASON_NO_TARGETS,
                    "component_id": component_id,
                },),
            )

        associations: List[AppInsightsAssociation] = []
        unresolved: List[Dict[str, Any]] = []

        if entry.dotnet_app_id:
            resolved = self._dotnet(entry.dotnet_app_id)
            if resolved:
                associations.append(AppInsightsAssociation(
                    target_type=TARGET_DOTNET_APP,
                    target_id=resolved,
                    org_id=self.org_id,
                    evidence={
                        "source": "configuration",
                        "config_key": CONFIG_ENV,
                        "component_id": entry.component_id,
                        "declared_reference": entry.dotnet_app_id,
                        "resolved_against": "dotnet_app_config.load_targets",
                        **({"notes": entry.notes} if entry.notes else {}),
                    },
                ))
            else:
                unresolved.append({
                    "reason": REASON_DOTNET_NOT_CONFIGURED,
                    "target_type": TARGET_DOTNET_APP,
                    "component_id": component_id,
                    "declared_reference": entry.dotnet_app_id,
                })

        if entry.cmdb_ci_sys_id:
            ci = self._cmdb(entry.cmdb_ci_sys_id)
            if ci:
                associations.append(AppInsightsAssociation(
                    target_type=TARGET_CMDB_CI,
                    target_id=ci["sys_id"],
                    org_id=self.org_id,
                    evidence={
                        "source": "configuration",
                        "config_key": CONFIG_ENV,
                        "component_id": entry.component_id,
                        "declared_reference": entry.cmdb_ci_sys_id,
                        "resolved_against": "entity_resolution.lookup_resolved_entity",
                        **({"entity_id": ci["entity_id"]} if ci.get("entity_id") else {}),
                        **({"ci_name": ci["display_name"]} if ci.get("display_name") else {}),
                        **({"notes": entry.notes} if entry.notes else {}),
                    },
                ))
            else:
                unresolved.append({
                    "reason": REASON_CMDB_NOT_RESOLVED,
                    "target_type": TARGET_CMDB_CI,
                    "component_id": component_id,
                    "declared_reference": entry.cmdb_ci_sys_id,
                })

        return AssociationOutcome(
            associations=tuple(associations),
            unresolved=tuple(unresolved),
        )
