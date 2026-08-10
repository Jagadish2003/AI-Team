"""Authoring lint — 2.0-C3 T3 (AT-838).

The platform's non-negotiables, checked while an author still has the file open
rather than at a customer:

  * **no individual naming** — packs speak groups, queues, services, and
    entities;
  * **causal-gate wording** — a pack states concentration and recurrence; it
    never asserts causation;
  * **aggregation floors** — a detector must require more than a single record
    before it fires;
  * **evidence completeness** — every finding must carry all four parts, with
    numeric evidence and a resolvable source trace.

Why lint exists on top of schema validation
-------------------------------------------
Schema validation (AT-836) answers *is this document well-formed and closed?*
Lint answers *is this pack honest?* — a question that has nothing to do with
shape. A manifest can be perfectly valid and still set an ageing floor of one
item, or write "caused by" into a label, or name an assignee in its terminology.
The schema's numeric bounds are the outer sanity limit; lint's floors are the
discipline, which is deliberately stricter. Where the two coincide today, the
lint rule stays as the regression guard for the day somebody loosens a bound.

Two legs per rule
-----------------
Most rules have a STATIC leg (over the manifest) and a RUNTIME leg (over findings
the harness produced). Both matter and neither subsumes the other: a label can
name an individual the fixtures never exercise, and a finding can leak one through
a subject the manifest never mentions. :func:`lint_pack` runs whichever it has.

Dependency-free of ``app``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contract import (
    FOUR_PART_CONTRACT_FIELDS,
    find_causal_language,
    find_individual_references,
)
from .manifest import PackManifest
from .primitives import get_primitive

# ── Rule codes ────────────────────────────────────────────────────────────────

RULE_INDIVIDUAL_NAMING = "individual_naming"
RULE_CAUSAL_WORDING = "causal_wording"
RULE_AGGREGATION_FLOOR = "missing_aggregation_floor"
RULE_INCOMPLETE_EVIDENCE = "incomplete_evidence"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Every rule this pass enforces, with the one-line requirement it comes from.
LINT_RULES: Dict[str, str] = {
    RULE_INDIVIDUAL_NAMING: (
        "Findings reference groups, queues, services, and entities — never an "
        "individual person."
    ),
    RULE_CAUSAL_WORDING: (
        "A pack states what is observed (recurrence, concentration, ageing). "
        "Causation is the causal engine's to assert, never a pack's."
    ),
    RULE_AGGREGATION_FLOOR: (
        "A detector must require more than a single record before it fires — one "
        "record is a record, not a finding."
    ),
    RULE_INCOMPLETE_EVIDENCE: (
        "Every finding carries all four parts, with numeric evidence and a source "
        "trace that resolves to real records."
    ),
}

# ── Individual-naming vocabulary ──────────────────────────────────────────────
#
# Deliberately PHRASES, not bare words. "user" appears in legitimate pack prose
# ("end users report..."), and a rule that fires on it is a rule authors learn to
# suppress. These are the shapes that mean "this pack is about a named person".
_INDIVIDUAL_PHRASES: Tuple[str, ...] = (
    "assignee",
    "assigned to",
    "individual employee",
    "individual user",
    "named user",
    "named individual",
    "person responsible",
    "who resolved",
    "who closed",
    "who opened",
    "caller",
    "employee name",
    "user name",
    "username",
    "full name",
    "email address",
    "by name",
    "per person",
    "per employee",
    "per analyst",
    "per agent name",
)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# ── Causal-wording exceptions ─────────────────────────────────────────────────
#
# The causal gate is about claims regarding OBSERVATIONS ("the dependency caused
# these incidents"). Accountability language — "humans remain responsible for
# every action" — is the platform's own guardrail sentence, carried by every
# first-party pack's LLM context, and it contains the phrase "responsible for"
# without asserting anything causal about a finding. Flagging it would make the
# rule fire on the one sentence we most want authors to write, and a rule that
# cries wolf on correct text is a rule authors route around.
_CAUSAL_ACCOUNTABILITY_CONTEXTS: Tuple[str, ...] = (
    "remain responsible for",
    "remains responsible for",
    "are responsible for",
    "is responsible for",
    "stay responsible for",
)


def _spans(text: str, phrase: str) -> List[Tuple[int, int]]:
    found: List[Tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(phrase, start)
        if index < 0:
            return found
        found.append((index, index + len(phrase)))
        start = index + 1


def _causal_hits(text: str) -> List[str]:
    """Causal phrases in ``text``, minus those inside accountability wording."""
    hits = find_causal_language(text)
    if not hits:
        return []
    lowered = text.lower()
    allowed: List[Tuple[int, int]] = []
    for context in _CAUSAL_ACCOUNTABILITY_CONTEXTS:
        allowed.extend(_spans(lowered, context))
    if not allowed:
        return hits
    real: List[str] = []
    for hit in hits:
        # Keep the hit if ANY of its occurrences sits outside every accountability
        # span — one genuine causal claim is not excused by a guardrail sentence
        # elsewhere in the same text.
        for hit_start, hit_end in _spans(lowered, hit):
            covered = any(
                span_start <= hit_start and hit_end <= span_end
                for span_start, span_end in allowed
            )
            if not covered:
                real.append(hit)
                break
    return real

# ── Aggregation floors ────────────────────────────────────────────────────────
#
# Per primitive: the parameter that IS its aggregation floor, and the minimum a
# pack must set it to. Stricter than the schema's bound by design — the schema
# says what is structurally sane, this says what is honest.
FLOOR_PARAMETERS: Dict[str, Tuple[str, int]] = {
    "recurrence": ("min_occurrences", 2),
    "ageing": ("min_items", 2),
    "oscillation": ("min_distinct_participants", 2),
    "concentration_traversal": ("min_dependents", 2),
    "co_occurrence_window": ("min_pairs", 2),
    "threshold_vs_baseline": ("min_baseline_runs", 2),
}


@dataclass(frozen=True)
class LintFinding:
    """One lint violation.

    ``rule``     the ``RULE_*`` code, so tooling can group and a report can name
                 which non-negotiable was broken.
    ``path``     where in the pack it is — the thing the author edits.
    ``message``  one sentence, actionable on its own.
    """

    rule: str
    severity: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.severity}] {self.path}: {self.message}"

    def to_dict(self) -> Dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class LintReport:
    """The verdict for one pack. Never raises — the report IS the result."""

    findings: List[LintFinding] = field(default_factory=list)

    @property
    def errors(self) -> List[LintFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> List[LintFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing BLOCKING was found. Warnings do not fail a pack."""
        return not self.errors

    def rules_violated(self) -> List[str]:
        seen: List[str] = []
        for finding in self.findings:
            if finding.rule not in seen:
                seen.append(finding.rule)
        return seen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errorCount": len(self.errors),
            "warningCount": len(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


# ── Text collection ───────────────────────────────────────────────────────────


def _manifest_text(manifest: PackManifest) -> List[Tuple[str, str]]:
    """Every author-written string in a manifest, with its path.

    Everything an author can write prose into is linted, because the honesty
    rules are about what a reader will eventually see — and a label, a glossary
    entry, and an LLM context hint all reach a reader.
    """
    texts: List[Tuple[str, str]] = [("pack.description", manifest.description)]
    for index, declaration in enumerate(manifest.detectors):
        base = f"detectors[{index}]"
        texts.append((f"{base}.title", declaration.title))
        for key, value in declaration.labels.items():
            texts.append((f"{base}.labels.{key}", value))
    for key, value in manifest.glossary.items():
        texts.append((f"terminology.glossary.{key}", value))
    for key, value in manifest.language_map.items():
        texts.append((f"terminology.languageMap.{key}", value))
    if manifest.llm_context:
        texts.append(("terminology.llmContext", manifest.llm_context))
    focus = manifest.template_defaults.get("workflowFocus")
    if isinstance(focus, str) and focus:
        texts.append(("templateDefaults.workflowFocus", focus))
    return [(path, text) for path, text in texts if isinstance(text, str) and text]


# ── Rules ─────────────────────────────────────────────────────────────────────


def _check_individual_naming(
    manifest: PackManifest, findings: Sequence[Any]
) -> List[LintFinding]:
    out: List[LintFinding] = []
    for path, text in _manifest_text(manifest):
        lowered = text.lower()
        hits = [phrase for phrase in _INDIVIDUAL_PHRASES if phrase in lowered]
        if _EMAIL_RE.search(text):
            hits.append("an email address")
        if hits:
            out.append(
                LintFinding(
                    RULE_INDIVIDUAL_NAMING,
                    SEVERITY_ERROR,
                    path,
                    (
                        f"names an individual ({', '.join(sorted(set(hits)))}); "
                        f"packs describe groups, queues, services, and entities"
                    ),
                )
            )
    for index, finding in enumerate(findings):
        leaked = find_individual_references(dict(getattr(finding, "contract", {}) or {}))
        if leaked:
            out.append(
                LintFinding(
                    RULE_INDIVIDUAL_NAMING,
                    SEVERITY_ERROR,
                    f"findings[{index}] ({getattr(finding, 'detector_id', '')})",
                    f"emitted finding references an individual: {leaked}",
                )
            )
    return out


def _check_causal_wording(
    manifest: PackManifest, findings: Sequence[Any]
) -> List[LintFinding]:
    out: List[LintFinding] = []
    for path, text in _manifest_text(manifest):
        hits = _causal_hits(text)
        if hits:
            out.append(
                LintFinding(
                    RULE_CAUSAL_WORDING,
                    SEVERITY_ERROR,
                    path,
                    (
                        f"asserts causation ({', '.join(hits)}); state what is "
                        f"observed — recurrence, concentration, ageing — and leave "
                        f"causation to the causal engine"
                    ),
                )
            )
    for index, finding in enumerate(findings):
        statement = str(getattr(finding, "statement", "") or "")
        hits = _causal_hits(statement)
        if hits:
            out.append(
                LintFinding(
                    RULE_CAUSAL_WORDING,
                    SEVERITY_ERROR,
                    f"findings[{index}] ({getattr(finding, 'detector_id', '')})",
                    f"emitted statement asserts causation ({', '.join(hits)})",
                )
            )
    return out


def _check_aggregation_floors(manifest: PackManifest) -> List[LintFinding]:
    out: List[LintFinding] = []
    for index, declaration in enumerate(manifest.detectors):
        floor = FLOOR_PARAMETERS.get(declaration.primitive)
        if floor is None:
            continue
        parameter, minimum = floor
        resolved = declaration.resolved_parameters().get(parameter)
        if resolved is None:
            out.append(
                LintFinding(
                    RULE_AGGREGATION_FLOOR,
                    SEVERITY_ERROR,
                    f"detectors[{index}].parameters.{parameter}",
                    (
                        f"primitive {declaration.primitive!r} has no aggregation "
                        f"floor set; {parameter} must be at least {minimum}"
                    ),
                )
            )
            continue
        if isinstance(resolved, (int, float)) and resolved < minimum:
            out.append(
                LintFinding(
                    RULE_AGGREGATION_FLOOR,
                    SEVERITY_ERROR,
                    f"detectors[{index}].parameters.{parameter}",
                    (
                        f"{parameter}={resolved} lets a single record become a "
                        f"finding; the aggregation floor for "
                        f"{declaration.primitive!r} is {minimum}"
                    ),
                )
            )
    return out


def _check_evidence_completeness(
    manifest: PackManifest, findings: Sequence[Any]
) -> List[LintFinding]:
    out: List[LintFinding] = []
    for index, declaration in enumerate(manifest.detectors):
        if not str(declaration.labels.get("summary", "")).strip():
            out.append(
                LintFinding(
                    RULE_INCOMPLETE_EVIDENCE,
                    SEVERITY_ERROR,
                    f"detectors[{index}].labels.summary",
                    (
                        "no summary label: the finding would surface its numbers "
                        "with no claim a reader can interrogate"
                    ),
                )
            )
        spec = get_primitive(declaration.primitive)
        if spec is not None and not declaration.concepts:
            out.append(
                LintFinding(
                    RULE_INCOMPLETE_EVIDENCE,
                    SEVERITY_ERROR,
                    f"detectors[{index}].concepts",
                    "binds no normalised concept, so it can produce no evidence",
                )
            )

    for index, finding in enumerate(findings):
        where = f"findings[{index}] ({getattr(finding, 'detector_id', '')})"
        contract = dict(getattr(finding, "contract", {}) or {})
        missing = [part for part in FOUR_PART_CONTRACT_FIELDS if not contract.get(part)]
        if missing:
            out.append(
                LintFinding(
                    RULE_INCOMPLETE_EVIDENCE,
                    SEVERITY_ERROR,
                    where,
                    f"emitted finding is missing contract part(s): {missing}",
                )
            )
            continue
        evidence = contract.get("evidence") or {}
        if not any(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in _flatten(evidence)
        ):
            out.append(
                LintFinding(
                    RULE_INCOMPLETE_EVIDENCE,
                    SEVERITY_ERROR,
                    where,
                    "emitted finding carries no numeric evidence",
                )
            )
        trace = contract.get("source_trace") or {}
        if not trace.get("systems") or not trace.get("artifacts"):
            out.append(
                LintFinding(
                    RULE_INCOMPLETE_EVIDENCE,
                    SEVERITY_ERROR,
                    where,
                    "emitted finding's source trace resolves to no source record",
                )
            )
    return out


def _flatten(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


# ── Public API ────────────────────────────────────────────────────────────────


def lint_pack(
    manifest: PackManifest, findings: Optional[Sequence[Any]] = None
) -> LintReport:
    """Lint a validated manifest, and the findings it produced when available.

    Pass the harness's findings to get the runtime legs — a pack that lints clean
    statically can still emit a finding that names an individual through a subject
    the manifest never mentions.
    """
    produced = list(findings or [])
    report = LintReport()
    report.findings.extend(_check_individual_naming(manifest, produced))
    report.findings.extend(_check_causal_wording(manifest, produced))
    report.findings.extend(_check_aggregation_floors(manifest))
    report.findings.extend(_check_evidence_completeness(manifest, produced))
    return report


def lint_rule_reference() -> Dict[str, Any]:
    """The rules and their floors, for the authoring docs and the CLI's ``--rules``."""
    return {
        "rules": [
            {"rule": rule, "requirement": requirement}
            for rule, requirement in LINT_RULES.items()
        ],
        "aggregationFloors": {
            primitive: {"parameter": parameter, "minimum": minimum}
            for primitive, (parameter, minimum) in sorted(FLOOR_PARAMETERS.items())
        },
        "individualNamingPhrases": list(_INDIVIDUAL_PHRASES),
    }


__all__ = [
    "FLOOR_PARAMETERS",
    "LINT_RULES",
    "RULE_AGGREGATION_FLOOR",
    "RULE_CAUSAL_WORDING",
    "RULE_INCOMPLETE_EVIDENCE",
    "RULE_INDIVIDUAL_NAMING",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "LintFinding",
    "LintReport",
    "lint_pack",
    "lint_rule_reference",
]
