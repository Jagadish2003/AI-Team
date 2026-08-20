"""Platform capability declaration — 2.0-C1 T1 (AT-826).

Single source of truth for the two things a pack declares compatibility
*against*:

  1. **The platform capability version** (:data:`PLATFORM_VERSION`) — the version
     of the discovery platform a pack runs on. A pack declares the range of
     platform versions it supports; a pack outside that range cannot be
     activated.
  2. **The normalised-concept vocabulary** (:data:`NORMALISED_CONCEPTS`) — the
     normalised signal concepts the platform *provides*, each stamped with the
     platform version that introduced it. A pack declares the concepts its
     detectors require; a pack requiring a concept this platform version does
     not provide cannot be activated.

Why a version *and* a concept list
----------------------------------
The platform version alone is too coarse to explain a refusal ("needs ≥ 1.9.0"
says nothing about *what* is missing), and a concept list alone cannot express a
breaking change to a concept that already exists. Together they give AT-826 its
requirement: an incompatible pack is refused with a reason that **names the
unmet requirement**.

Concepts are *platform capabilities*, not per-run data availability
-------------------------------------------------------------------
A concept is listed here when the platform can normalise it **at all** — i.e.
the ingestion + normalisation code ships. Whether a given run actually has that
data is a different question, answered by connector selection and the existing
per-source degradation rules (a pack whose source is not connected degrades to
partial/unavailable findings; it is NOT "incompatible"). Conflating the two
would turn a disconnected connector into a refused pack, which is wrong.

Deliberately dependency-free
----------------------------
No ``app`` import and no I/O, so the compatibility gate can run in BOTH layers —
the API activation edges (``app/routes_stack_builder_launch.py``,
``app/routes_sprint4_t1.py``) and the discovery runner — without the runner
taking an ``app`` dependency. Mirrors ``app/connector_roadmap.py``'s posture as
the one place a shipped-vs-not rule is declared.

Bumping
-------
* Add a concept here when the platform starts normalising a new signal concept,
  stamped with the release that ships it.
* Bump :data:`PLATFORM_VERSION` when the platform's capability surface changes in
  a way a pack could depend on. A pack range is inclusive at both ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ── Platform capability version ───────────────────────────────────────────────

#: The discovery platform's capability version. Packs declare a supported range
#: against this value; the compatibility gate compares against it (see
#: ``pack_compatibility.check_pack_compatibility``). Release 2.0 (Arc-C).
PLATFORM_VERSION = "2.0.0"

# Release constants for concept "since" stamps, so a release string is written
# once and a concept's introduction is greppable.
_SINCE_1_0 = "1.0.0"   # Original Salesforce-era normalisation
_SINCE_1_9 = "1.9.0"   # MSP release — B0/B3/B4/B5/B7/B11 normalised concepts


# ── Normalised-concept vocabulary ─────────────────────────────────────────────


@dataclass(frozen=True)
class ConceptSpec:
    """One normalised concept the platform provides.

    ``concept_id``  stable identifier a pack declares in ``requiredConcepts``.
    ``since``       platform version that introduced it (inclusive).
    ``description`` one line, used verbatim in refusal reasons and surfacing.
    """

    concept_id: str
    since: str
    description: str


def _spec(concept_id: str, since: str, description: str) -> Tuple[str, ConceptSpec]:
    return concept_id, ConceptSpec(concept_id, since, description)


#: Every normalised concept this platform can produce, keyed by concept id.
#: A concept a pack requires but which is absent from this map is unmet at ANY
#: platform version — the refusal names it (that is the typo/unshipped case).
NORMALISED_CONCEPTS: Dict[str, ConceptSpec] = dict(
    [
        # ── Salesforce-era concepts (original platform) ────────────────────────
        _spec(
            "case_workflow",
            _SINCE_1_0,
            "Salesforce case, flow, and approval-process workflow normalisation",
        ),
        _spec(
            "loan_origination_workflow",
            _SINCE_1_0,
            "nCino commercial-lending origination, checklist, and spreading normalisation",
        ),
        _spec(
            "benefit_administration_workflow",
            _SINCE_1_0,
            "STRS benefit application, election, and disbursement normalisation",
        ),
        _spec(
            "db_operational_signal",
            _SINCE_1_0,
            "Native database operational signals (ticket volume, SLA, queue depth)",
        ),
        _spec(
            "code_activity_signal",
            _SINCE_1_0,
            "Source-control activity signals (pull requests, commits, branches)",
        ),
        _spec(
            "cross_system_link",
            _SINCE_1_0,
            "Cross-system signal linking between ServiceNow, Jira, and Salesforce",
        ),
        # ServiceNow incident normalisation predates the MSP release — the
        # ServiceNow ingestor and its incident metrics ship in the original
        # platform (enterprise_ops has consumed them since 1.0). MSP-B4 added the
        # deterministic SIGNATURES and group-routing history below, not incident
        # normalisation itself; stamping this concept 1.9.0 would wrongly refuse
        # enterprise_ops on the platform it has always run on.
        _spec(
            "incident_workflow",
            _SINCE_1_0,
            "ServiceNow incident workflow normalisation (state, category, close code, "
            "time-to-resolve)",
        ),
        # ── MSP release concepts ──────────────────────────────────────────────
        _spec(
            "resolution_signature",
            _SINCE_1_9,
            "MSP-B4 deterministic resolution signature — how an incident was resolved",
        ),
        _spec(
            "incident_identity_signature",
            _SINCE_1_9,
            "MSP-B4 deterministic incident-identity signature — what kind of incident this is",
        ),
        _spec(
            "assignment_group_routing",
            _SINCE_1_9,
            "MSP-B4 assignment-group routing history (group-level reassignment hops)",
        ),
        _spec(
            "operational_event",
            _SINCE_1_9,
            "MSP-B0 normalised operational cloud event (with MSP-B7 dedup and volume "
            "disciplines)",
        ),
        _spec(
            "cmdb_dependency",
            _SINCE_1_9,
            "MSP-B3 CMDB configuration items and dependency edges",
        ),
        _spec(
            "runbook_match",
            _SINCE_1_9,
            "MSP-B5 runbook match states (observed / proposed / confirmed)",
        ),
        _spec(
            "security_incident_workflow",
            _SINCE_1_9,
            "MSP-B11 ServiceNow security-incident (SIR) workflow normalisation",
        ),
        _spec(
            "vulnerability_workflow",
            _SINCE_1_9,
            "MSP-B11 ServiceNow vulnerability-response workflow normalisation",
        ),
    ]
)


# ── Version parsing / comparison ──────────────────────────────────────────────


def parse_version(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse a dotted numeric version into a comparable 3-tuple.

    Tolerant of the shapes actually used in this repo: ``"2"``, ``"2.0"``,
    ``"2.0.0"``, and a pre-release/build suffix (``"2.0.0-rc1"``, ``"2.0.0+ci"``)
    whose suffix is ignored. Shorter versions pad with zeros, so ``"1.9"`` and
    ``"1.9.0"`` compare equal.

    Returns ``None`` for anything unparseable (including ``None``/empty). Callers
    treat an unparseable *declared* version as a loud invalid declaration rather
    than silently ignoring the bound — a typo must never widen a range.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Drop a pre-release / build suffix — ordering within a release line is not
    # something a pack range needs to express.
    for separator in ("-", "+"):
        if separator in text:
            text = text.split(separator, 1)[0]
    parts = text.split(".")
    if len(parts) > 3:
        return None
    numbers: List[int] = []
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            return None
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def compare_versions(left: str, right: str) -> Optional[int]:
    """Return -1/0/1 comparing two versions, or ``None`` if either is unparseable."""
    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left < parsed_right:
        return -1
    if parsed_left > parsed_right:
        return 1
    return 0


# ── Public API ────────────────────────────────────────────────────────────────


def get_platform_version() -> str:
    """The platform capability version packs are checked against."""
    return PLATFORM_VERSION


def get_concept(concept_id: str) -> Optional[ConceptSpec]:
    """The spec for a normalised concept, or ``None`` if the platform has no such
    concept at any version (an unshipped or misspelled requirement)."""
    return NORMALISED_CONCEPTS.get(concept_id)


def is_concept_known(concept_id: str) -> bool:
    """True when the platform declares this concept at SOME version."""
    return concept_id in NORMALISED_CONCEPTS


def is_concept_available(
    concept_id: str, platform_version: Optional[str] = None
) -> bool:
    """True when this platform version provides the concept.

    A concept is available once the platform reaches the version that introduced
    it (``since`` is inclusive). An unknown concept is never available. A concept
    whose ``since`` cannot be compared against ``platform_version`` is treated as
    unavailable — fail closed, so a malformed stamp cannot silently pass a gate.
    """
    spec = NORMALISED_CONCEPTS.get(concept_id)
    if spec is None:
        return False
    ordering = compare_versions(
        platform_version or PLATFORM_VERSION, spec.since
    )
    return ordering is not None and ordering >= 0


def available_concepts(platform_version: Optional[str] = None) -> List[str]:
    """Sorted concept ids this platform version provides."""
    return sorted(
        concept_id
        for concept_id in NORMALISED_CONCEPTS
        if is_concept_available(concept_id, platform_version)
    )


def describe_concept(concept_id: str) -> str:
    """Human-readable label for a concept, used in refusal reasons.

    Falls back to the bare id for an unknown concept so a refusal naming an
    unshipped requirement still reads correctly.
    """
    spec = NORMALISED_CONCEPTS.get(concept_id)
    if spec is None:
        return concept_id
    return f"{concept_id} ({spec.description})"


def platform_capability_summary(
    platform_version: Optional[str] = None,
) -> Dict[str, object]:
    """JSON-serialisable snapshot of the platform's capability surface.

    The audit/surfacing shape (AT-830 consumes this; the compatibility gate uses
    the primitives above directly).
    """
    version = platform_version or PLATFORM_VERSION
    return {
        "platformVersion": version,
        "concepts": [
            {
                "conceptId": spec.concept_id,
                "since": spec.since,
                "description": spec.description,
                "available": is_concept_available(spec.concept_id, version),
            }
            for spec in sorted(
                NORMALISED_CONCEPTS.values(), key=lambda s: (s.since, s.concept_id)
            )
        ],
    }
