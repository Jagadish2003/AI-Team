"""Pack compatibility gate — 2.0-C1 T1 (AT-826).

The rule this module enforces (parent story AC1):

    A pack declaring an unmet platform range cannot be activated; the refusal
    names the unmet requirement.

Two halves, one gate
--------------------
* ``pack_config.py`` is the DECLARATION surface — each pack declares its
  supported platform-version range and the normalised concepts (MSP-B4 and the
  rest of the vocabulary) its detectors require, under the ``compatibility`` key.
* ``platform_capabilities.py`` is the PLATFORM surface — the platform capability
  version and the normalised concepts it provides.
* This module compares them and produces a :class:`PackCompatibility` report
  whose ``reason`` **names every unmet requirement**, plus
  :func:`assert_selection_activatable`, the one call an activation edge makes.

Where the gate runs
-------------------
Every activation edge, so an incompatible pack cannot be activated by any route:

* ``POST /api/stack-builder/launch``  (``app/routes_stack_builder_launch.py``)
* ``POST /api/runs/{run_id}/compute`` (``app/routes_sprint4_t1.py``)
* ``discovery/runner.py``            — defence in depth at the execution point,
  so a direct/CLI caller that bypasses the API cannot run an incompatible pack.

Fail-closed posture
-------------------
An unparseable declared bound is an unmet requirement (``invalid_declaration``),
not an ignored bound: a typo in a range must never silently widen it. Likewise a
required concept the platform does not know at ANY version is unmet and named —
that is the unshipped-or-misspelled case.

Unknown pack ids are NOT a compatibility failure. ``get_pack()`` resolves an
unknown id to the default pack (warning, not raising); this module checks the
RESOLVED pack, so existing unknown-id behaviour is byte-identical.

Scope note
----------
This is the compatibility half of 2.0-C1. Pack ENABLE/DISABLE state (AT-827) and
version ROLLBACK (AT-828) are separate lifecycle concerns layered on top of this
gate; nothing here reads or writes pack state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .pack_config import (
    get_pack,
    get_pack_compatibility_declaration,
    get_pack_version,
    normalize_pack_ids,
)
from .platform_capabilities import (
    compare_versions,
    describe_concept,
    get_platform_version,
    is_concept_available,
    is_concept_known,
    parse_version,
)

# ── Unmet-requirement kinds ───────────────────────────────────────────────────

#: Platform version is BELOW the pack's declared inclusive floor.
KIND_PLATFORM_TOO_OLD = "platform_version_below_minimum"
#: Platform version is ABOVE the pack's declared inclusive ceiling.
KIND_PLATFORM_TOO_NEW = "platform_version_above_maximum"
#: A required normalised concept this platform version does not provide.
KIND_CONCEPT_UNAVAILABLE = "required_concept_unavailable"
#: A required concept the platform does not know at ANY version.
KIND_CONCEPT_UNKNOWN = "required_concept_unknown"
#: A declared bound (or the platform version) could not be parsed — fail closed.
KIND_INVALID_DECLARATION = "invalid_compatibility_declaration"


@dataclass(frozen=True)
class UnmetRequirement:
    """One named, unmet compatibility requirement.

    ``kind``        machine-readable classification (the ``KIND_*`` constants).
    ``requirement`` the declared requirement itself — a version bound or a
                    concept id. This is the value a refusal must NAME.
    ``detail``      one human-readable sentence, used verbatim in the refusal.
    """

    kind: str
    requirement: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "kind": self.kind,
            "requirement": self.requirement,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PackCompatibility:
    """The compatibility verdict for one pack against one platform version."""

    pack_id: str
    pack_name: str
    pack_version: str
    platform_version: str
    min_platform_version: Optional[str]
    max_platform_version: Optional[str]
    required_concepts: List[str] = field(default_factory=list)
    optional_concepts: List[str] = field(default_factory=list)
    unmet: List[UnmetRequirement] = field(default_factory=list)
    #: Declared-optional concepts this platform version does not provide. Advisory
    #: only — the pack degrades honestly without them and still activates.
    unavailable_optional_concepts: List[str] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not self.unmet

    @property
    def reason(self) -> str:
        """The refusal reason — names the pack and every unmet requirement.

        Version bounds are stated individually; unmet concepts are collapsed into
        ONE clause listing each concept by name, because a version floor failure
        normally drags every concept with it and five repetitions of the same
        sentence hide the actual cause. Each concept still gets its own
        :class:`UnmetRequirement` in ``unmet``, so nothing is lost structurally.

        Empty string when compatible, so callers can use it directly as the
        message of the error they raise.
        """
        if self.compatible:
            return ""

        clauses: List[str] = [
            item.detail
            for item in self.unmet
            if item.kind
            not in (KIND_CONCEPT_UNAVAILABLE, KIND_CONCEPT_UNKNOWN)
        ]

        unavailable = [
            item.requirement
            for item in self.unmet
            if item.kind == KIND_CONCEPT_UNAVAILABLE
        ]
        if unavailable:
            named = ", ".join(
                f"{concept} (introduced in platform version {_concept_since(concept)})"
                for concept in unavailable
            )
            clauses.append(
                f"requires normalised concepts this platform version does not "
                f"provide: {named}"
            )

        unknown = [
            item.requirement
            for item in self.unmet
            if item.kind == KIND_CONCEPT_UNKNOWN
        ]
        if unknown:
            clauses.append(
                f"requires normalised concepts this platform does not provide at "
                f"any version: {', '.join(unknown)}"
            )

        return (
            f"Pack '{self.pack_id}' (version {self.pack_version}) cannot be "
            f"activated on platform version {self.platform_version}: "
            f"{'; '.join(clauses)}."
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable report — the shape surfacing (AT-830) and run health
        consume, and the shape persisted alongside a refusal."""
        return {
            "packId": self.pack_id,
            "packName": self.pack_name,
            "packVersion": self.pack_version,
            "platformVersion": self.platform_version,
            "minPlatformVersion": self.min_platform_version,
            "maxPlatformVersion": self.max_platform_version,
            "requiredConcepts": list(self.required_concepts),
            "optionalConcepts": list(self.optional_concepts),
            "unavailableOptionalConcepts": list(self.unavailable_optional_concepts),
            "compatible": self.compatible,
            "unmet": [item.to_dict() for item in self.unmet],
            "reason": self.reason,
        }


class PackIncompatibleError(Exception):
    """Raised when an incompatible pack would be activated.

    ``str(exc)`` is the user-facing refusal naming every unmet requirement, so an
    activation edge can pass it straight into an HTTP error detail.
    """

    def __init__(self, reports: Sequence[PackCompatibility]) -> None:
        self.reports: List[PackCompatibility] = [
            report for report in reports if not report.compatible
        ]
        super().__init__(" ".join(report.reason for report in self.reports))

    @property
    def pack_ids(self) -> List[str]:
        """The refused pack ids, in the order they were selected."""
        return [report.pack_id for report in self.reports]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "pack_incompatible",
            "message": str(self),
            "packs": [report.to_dict() for report in self.reports],
        }


# ── The check ─────────────────────────────────────────────────────────────────


def check_pack_compatibility(
    pack_id: Optional[str] = None,
    *,
    platform_version: Optional[str] = None,
) -> PackCompatibility:
    """Check one REGISTERED pack against a platform version.

    ``pack_id`` is resolved through ``get_pack()``, so an unknown id is checked as
    the default pack (unchanged behaviour). ``platform_version`` defaults to the
    running platform's :func:`platform_capabilities.get_platform_version`.

    Never raises — the verdict is the return value. Use
    :func:`assert_pack_activatable` when a refusal should raise.
    """
    pack = get_pack(pack_id)
    return check_declaration_compatibility(
        pack_id=pack["packId"],
        pack_name=pack.get("packName", pack["packId"]),
        pack_version=get_pack_version(pack_id),
        declaration=get_pack_compatibility_declaration(pack_id),
        platform_version=platform_version,
    )


def check_declaration_compatibility(
    *,
    pack_id: str,
    pack_name: str,
    pack_version: str,
    declaration: Dict[str, Any],
    platform_version: Optional[str] = None,
) -> PackCompatibility:
    """Check a compatibility DECLARATION against a platform version.

    The rule itself, separated from where the declaration came from. A registered
    pack reads its block from ``pack_config`` (above); an AUTHORED pack being
    installed (2.0-C3 T4 / AT-839) is not in the registry at all and supplies its
    manifest's block directly. Both must be judged identically — an installed pack
    held to a second, parallel implementation of this rule would drift from the one
    the runner enforces, which is the failure the R17-A4 shared-extraction
    discipline exists to prevent.

    Never raises.
    """
    resolved_id = pack_id
    effective_platform = platform_version or get_platform_version()

    minimum = declaration["minPlatformVersion"]
    maximum = declaration["maxPlatformVersion"]
    required = list(declaration["requiredConcepts"])
    optional = list(declaration["optionalConcepts"])

    unmet: List[UnmetRequirement] = []

    # The platform version itself must be parseable before any bound can be
    # compared against it. Fail closed rather than skipping the range check.
    if parse_version(effective_platform) is None:
        unmet.append(
            UnmetRequirement(
                KIND_INVALID_DECLARATION,
                effective_platform,
                (
                    f"platform version {effective_platform!r} is not a parseable "
                    f"version, so the declared range cannot be verified"
                ),
            )
        )
    else:
        if minimum is not None:
            ordering = compare_versions(effective_platform, minimum)
            if ordering is None:
                unmet.append(
                    UnmetRequirement(
                        KIND_INVALID_DECLARATION,
                        minimum,
                        (
                            f"declared minimum platform version {minimum!r} is not a "
                            f"parseable version"
                        ),
                    )
                )
            elif ordering < 0:
                unmet.append(
                    UnmetRequirement(
                        KIND_PLATFORM_TOO_OLD,
                        minimum,
                        (
                            f"requires platform version >= {minimum} "
                            f"(this platform is {effective_platform})"
                        ),
                    )
                )

        if maximum is not None:
            ordering = compare_versions(effective_platform, maximum)
            if ordering is None:
                unmet.append(
                    UnmetRequirement(
                        KIND_INVALID_DECLARATION,
                        maximum,
                        (
                            f"declared maximum platform version {maximum!r} is not a "
                            f"parseable version"
                        ),
                    )
                )
            elif ordering > 0:
                unmet.append(
                    UnmetRequirement(
                        KIND_PLATFORM_TOO_NEW,
                        maximum,
                        (
                            f"requires platform version <= {maximum} "
                            f"(this platform is {effective_platform})"
                        ),
                    )
                )

    # Required normalised concepts (MSP-B4 and the wider vocabulary). Reported
    # per concept so the refusal names each one, never just a count.
    for concept in required:
        if not is_concept_known(concept):
            unmet.append(
                UnmetRequirement(
                    KIND_CONCEPT_UNKNOWN,
                    concept,
                    (
                        f"requires normalised concept {concept!r}, which this "
                        f"platform does not provide at any version"
                    ),
                )
            )
        elif not is_concept_available(concept, effective_platform):
            unmet.append(
                UnmetRequirement(
                    KIND_CONCEPT_UNAVAILABLE,
                    concept,
                    (
                        f"requires normalised concept "
                        f"{describe_concept(concept)}, introduced in platform "
                        f"version {_concept_since(concept)} (this platform is "
                        f"{effective_platform})"
                    ),
                )
            )

    unavailable_optional = [
        concept
        for concept in optional
        if not is_concept_available(concept, effective_platform)
    ]

    return PackCompatibility(
        pack_id=resolved_id,
        pack_name=pack_name or resolved_id,
        pack_version=pack_version,
        platform_version=effective_platform,
        min_platform_version=minimum,
        max_platform_version=maximum,
        required_concepts=required,
        optional_concepts=optional,
        unmet=unmet,
        unavailable_optional_concepts=unavailable_optional,
    )


def _concept_since(concept_id: str) -> str:
    from .platform_capabilities import get_concept

    spec = get_concept(concept_id)
    return spec.since if spec is not None else "unknown"


def check_pack_selection(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> List[PackCompatibility]:
    """Check a whole (multi-)pack selection, order-preserved and de-duplicated.

    An empty selection checks the DEFAULT pack, mirroring the runner's
    "no selection → ``get_pack(None)``" rule, so the gate covers the default-pack
    path too. De-duplication is by RESOLVED pack id (two unknown ids both resolve
    to the default → one report), matching the runner's own de-duplication.
    """
    selection: List[Optional[str]] = list(normalize_pack_ids(list(pack_ids or [])))
    if not selection:
        selection = [None]

    reports: List[PackCompatibility] = []
    seen: set = set()
    for pack_id in selection:
        report = check_pack_compatibility(
            pack_id, platform_version=platform_version
        )
        if report.pack_id in seen:
            continue
        seen.add(report.pack_id)
        reports.append(report)
    return reports


def assert_pack_activatable(
    pack_id: Optional[str] = None,
    *,
    platform_version: Optional[str] = None,
) -> PackCompatibility:
    """Return the compatibility report, raising :class:`PackIncompatibleError` if
    the pack cannot be activated."""
    report = check_pack_compatibility(pack_id, platform_version=platform_version)
    if not report.compatible:
        raise PackIncompatibleError([report])
    return report


def assert_selection_activatable(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> List[PackCompatibility]:
    """Gate a whole pack selection — the one call an activation edge makes.

    Raises :class:`PackIncompatibleError` naming EVERY incompatible pack in the
    selection (not just the first), so a user fixing a multi-pack selection sees
    all of it at once. Returns the reports when the whole selection is activatable.
    """
    reports = check_pack_selection(pack_ids, platform_version=platform_version)
    refused = [report for report in reports if not report.compatible]
    if refused:
        raise PackIncompatibleError(refused)
    return reports


def compatibility_summary(
    pack_ids: Optional[Iterable[str]] = None,
    *,
    platform_version: Optional[str] = None,
) -> Dict[str, Any]:
    """JSON-serialisable compatibility snapshot for a selection.

    Persisted on a run and consumed by surfacing (AT-830) / run health so the
    pack's declared range and version are reported accurately, not re-derived
    later from a registry that may have moved on.
    """
    reports = check_pack_selection(pack_ids, platform_version=platform_version)
    return {
        "platformVersion": platform_version or get_platform_version(),
        "compatible": all(report.compatible for report in reports),
        "packs": [report.to_dict() for report in reports],
    }
