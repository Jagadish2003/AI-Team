"""Certification review criteria — 2.0-C2 T2 (AT-832).

The checklist a pack is reviewed against before CloudFulcrum signs its
certification. The story names four:

    declarative-manifest review, evidence-discipline conformance, terminology,
    and calibration sanity.

Those four are REQUIRED for every Certified or Partner review. Two further
criteria are declared here because the first-party packs already claim them in
their certification scope (AT-831) and they are genuinely pack-specific rather
than universal — a pack with no compliance guardrail and no aggregation floor
should not be forced to fake a verdict on one.

Why the vocabulary lives HERE and not in the review store
---------------------------------------------------------
A certification's ``scope.criteria`` (AT-831) and a review's checklist are the
same list read from two ends: the scope says *what was reviewed*, the review
says *how each item came out*. One vocabulary means they cannot drift, and a
structural test pins that every id a shipped pack claims in its scope is a
criterion that actually exists here.

Dependency-free (no ``app`` import, no I/O), matching
``pack_certification.py`` / ``platform_capabilities.py``, so both the API review
surface and any offline tooling can read it.

Adding a criterion
------------------
Append a :func:`_spec` entry. Mark it ``required=True`` only if EVERY pack must be
judged on it — a required criterion with no verdict blocks approval, so a
pack-specific concern belongs as optional with a note instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CriterionSpec:
    """One item on the certification review checklist.

    ``criterion_id``  stable identifier, used in a review record AND in a pack's
                      signed ``scope.criteria``.
    ``label``         short human label for the review UI.
    ``description``   what the reviewer is actually checking.
    ``required``      when True, a Certified/Partner review cannot be approved
                      without a passing verdict on it.
    """

    criterion_id: str
    label: str
    description: str
    required: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "criterionId": self.criterion_id,
            "label": self.label,
            "description": self.description,
            "required": self.required,
        }


def _spec(
    criterion_id: str, label: str, description: str, required: bool
) -> Tuple[str, CriterionSpec]:
    return criterion_id, CriterionSpec(criterion_id, label, description, required)


#: The review checklist, in the order a reviewer works through it.
CERTIFICATION_CRITERIA: Dict[str, CriterionSpec] = dict(
    [
        _spec(
            "declarative_manifest_review",
            "Declarative manifest review",
            "The pack is declarative configuration over platform primitives and "
            "normalised concepts — it ships no executable code and references "
            "nothing outside the primitive library (2.0-C3's governing "
            "constraint).",
            True,
        ),
        _spec(
            "evidence_discipline",
            "Evidence-discipline conformance",
            "Every finding the pack emits carries the four parts — evidence, "
            "confidence, corroboration status, and source trace — and confidence "
            "is never inflated beyond what the sources support.",
            True,
        ),
        _spec(
            "terminology",
            "Terminology",
            "Labels, narrative, and LLM context speak the pack's domain language, "
            "with no causal wording where only concentration is observed and no "
            "naming of individuals.",
            True,
        ),
        _spec(
            "calibration_sanity",
            "Calibration sanity",
            "Scorer weights and detector thresholds produce a defensible ranking "
            "on seeded data — no detector dominates by construction and no "
            "threshold is set where nothing can ever fire.",
            True,
        ),
        _spec(
            "compliance_guardrails",
            "Compliance guardrails",
            "Domain guardrails are present and enforced where the pack's domain "
            "requires them (no automated credit, benefit, merge, or remediation "
            "decisions — humans remain responsible).",
            False,
        ),
        _spec(
            "aggregation_floor",
            "Aggregation floor",
            "Security- or host-derived content is aggregated to the 1.9 floor — "
            "no host x vulnerability enumeration in findings, reports, or exports.",
            False,
        ),
    ]
)

#: Criteria every Certified/Partner review must pass. Order-preserved.
REQUIRED_CRITERION_IDS: List[str] = [
    spec.criterion_id for spec in CERTIFICATION_CRITERIA.values() if spec.required
]


def get_criterion(criterion_id: str) -> Optional[CriterionSpec]:
    """The spec for a criterion, or ``None`` if the checklist has no such item."""
    return CERTIFICATION_CRITERIA.get(str(criterion_id or "").strip())


def is_known_criterion(criterion_id: str) -> bool:
    """True when the id is on the checklist."""
    return str(criterion_id or "").strip() in CERTIFICATION_CRITERIA


def describe_criterion(criterion_id: str) -> str:
    """Human-readable label for a criterion, used verbatim in refusal messages.

    Falls back to the bare id for an unknown criterion, so a refusal naming a
    misspelled requirement still reads correctly.
    """
    spec = get_criterion(criterion_id)
    return f"{criterion_id} ({spec.label})" if spec else str(criterion_id)


def criteria_catalog() -> List[Dict[str, object]]:
    """JSON-serialisable checklist — the shape the review API serves."""
    return [spec.to_dict() for spec in CERTIFICATION_CRITERIA.values()]


__all__ = [
    "CERTIFICATION_CRITERIA",
    "CriterionSpec",
    "REQUIRED_CRITERION_IDS",
    "criteria_catalog",
    "describe_criterion",
    "get_criterion",
    "is_known_criterion",
]
