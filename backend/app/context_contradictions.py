"""2.0-B3 T3 — contradiction handling: name the disagreement, never resolve it (AC3).

Assembly composes a finding from heterogeneous material — ServiceNow records,
runbooks, Confluence pages, code, CMDB, chat. Sometimes two of those sources say
different things about the same thing: the CMDB says the payments service is owned
by ``Platform Engineering``, the runbook says ``L2 Support``. Before this module the
assembler ranked one above the other and the loser simply never reached the prompt,
so the narrative asserted ONE owner with total confidence and the disagreement — a
genuinely useful operational finding, and often the actual root of the friction —
disappeared without trace.

**The rule this module exists to enforce: a disagreement is surfaced, never
resolved.** Detection appends a record; it never drops, reorders, re-ranks or
re-weights a candidate, and there is no return path meaning "prefer this side".
Both positions travel into the finding with their sources named. This mirrors the
2.0-A2 T4 confounder discipline (``outcome_confounders.py``): caveats are appended,
never silently applied as an adjustment — and it is enforced the same way, by a
structural test over this module's AST that fails the build if it ever assigns to a
selection or ranking structure.

**A contradiction must be structural, never read out of prose.** A position is only
taken from a DECLARED comparable attribute exposed as a structured field on a
candidate. This module never parses narrative text looking for claims, because
"the runbook says X" derived by reading a paragraph is an inference presented as an
observation — precisely the failure the evidence spine exists to prevent. A
document contradicts a record only when its producer indexed a structured claim.

**Material, not merely different.** Three rules keep the report worth reading:

  * values are NORMALISED before comparison (case, whitespace, ``-``/``_``
    separators, surrounding punctuation), and a declared equivalence class can
    additionally state that two spellings are one value — ``L2 Support`` and
    ``l2-support`` are not a disagreement, they are a formatting difference, and a
    detector that reports them trains people to ignore it;
  * numbers compare against a declared relative tolerance, so ``4.0`` vs ``4.01``
    hours is agreement;
  * a MISSING value is never a position. Absence of information is not
    disagreement — the same rule ``outcome_confounders`` applies to an absent pack
    version.

**Two sources, not two records.** A contradiction requires positions from at least
``min_distinct_sources`` distinct source systems (default 2). One system holding two
rows that disagree is a data-quality problem inside that system, not a cross-source
disagreement, and reporting it here would bury the ones that matter. By default
every position must also be OBSERVED: an inferred value disagreeing with an observed
one is the platform disagreeing with a source, which must not be reported as two
sources disagreeing.

**Detected over the ELIGIBLE set, not the selected one.** If detection only saw what
survived the 2.0-B3 T2 budget, then a budget that trimmed one side would silently
resolve the disagreement — the exact failure this story forbids, reintroduced one
layer down. So a position is recorded even when its candidate did not make the cut,
and each position carries ``in_context`` so a reader can see that one side of the
disagreement is named but not quoted.

Pure and deterministic: no DB, no clock, no LLM, no network. Same candidates in,
byte-identical report out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attribute vocabulary
# ---------------------------------------------------------------------------

#: How a declared attribute's values are compared.
KIND_CATEGORICAL = "categorical"
KIND_NUMERIC = "numeric"
KNOWN_ATTRIBUTE_KINDS: Tuple[str, ...] = (KIND_CATEGORICAL, KIND_NUMERIC)

#: Sub-mappings searched for a declared attribute, in order. Producers in this repo
#: hang structured fields off several of these (entities carry ``metadata``, chunks
#: carry ``provenance``), so the extractor reads all of them rather than coupling to
#: one producer's shape — the same tolerance ``context_assembly``'s adapters apply.
_NESTED_FIELD_HOLDERS: Tuple[str, ...] = (
    "metadata", "attributes", "provenance", "fields", "claims", "properties",
)

#: Where a candidate's subject is read from, best spelling first.
_SUBJECT_FIELDS: Tuple[str, ...] = (
    "subject", "display_name", "canonical_name", "entity_name", "ci_name",
    "service", "component", "resource_name",
)

#: Where a candidate's source system is read from, best spelling first.
_SOURCE_SYSTEM_FIELDS: Tuple[str, ...] = (
    "source_system", "source", "connector_id", "system", "source_id",
)

#: Reported when a candidate exposes a claim but names no source system. Kept as an
#: explicit token rather than dropping the position: a claim from an unnamed source
#: still counts toward the DISTINCT-SOURCE rule as one unknown source, and never
#: silently merges with another unnamed one.
UNKNOWN_SOURCE = "unknown"


@dataclass(frozen=True)
class ComparableAttribute:
    """One attribute two sources may be compared on.

    ``aliases`` are the real field spellings across connectors (ServiceNow's
    ``assignment_group`` and a CMDB's ``owned_by`` are one attribute). They are
    matched normalised, so ``Assignment Group`` and ``assignment_group`` are the
    same alias.
    """

    name: str
    aliases: Tuple[str, ...] = ()
    kind: str = KIND_CATEGORICAL
    #: Groups of spellings that mean the same value. Declared, never guessed — a
    #: fuzzy "these look similar" rule here would manufacture agreement, which is
    #: the mirror image of manufacturing disagreement.
    equivalences: Tuple[Tuple[str, ...], ...] = ()

    def matches(self, field_name: str) -> bool:
        norm = _normalise_key(field_name)
        if norm == _normalise_key(self.name):
            return True
        return any(norm == _normalise_key(a) for a in self.aliases)

    def equivalence_of(self, normalised_value: str) -> Optional[str]:
        """The canonical member of ``normalised_value``'s declared class, if any."""
        for group in self.equivalences:
            members = [_normalise_value(m) for m in group]
            if normalised_value in members:
                return members[0]
        return None


@dataclass(frozen=True)
class ContradictionPolicy:
    """What counts as a material cross-source disagreement — declared, not implied."""

    comparable_attributes: Tuple[ComparableAttribute, ...] = ()
    numeric_tolerance_ratio: float = 0.10
    #: Every position must be observed. On by default: an inferred value is our own
    #: guess, and presenting the platform disagreeing with a source as "two sources
    #: disagree" would overstate what was actually observed.
    require_observed: bool = True
    min_distinct_sources: int = 2
    #: Bound on what is reported (an unbounded list would flood a prompt). Whatever
    #: it omits is COUNTED and reported — the MSP-B7 loud-degradation rule.
    max_reported: int = 5

    @property
    def enabled(self) -> bool:
        return bool(self.comparable_attributes)

    def attribute_for(self, field_name: str) -> Optional[ComparableAttribute]:
        for attribute in self.comparable_attributes:
            if attribute.matches(field_name):
                return attribute
        return None


#: The attributes compared when a deployment's ``assembly_policy.json`` predates
#: this story and declares no ``contradictions`` block. Chosen because each is an
#: operational fact two systems routinely hold independently and routinely disagree
#: about — which is what makes the disagreement worth a finding.
DEFAULT_COMPARABLE_ATTRIBUTES: Tuple[ComparableAttribute, ...] = (
    ComparableAttribute(
        name="owner",
        aliases=(
            "owned_by", "owner_team", "assignment_group", "assigned_to",
            "support_group", "responsible_team", "managed_by",
        ),
    ),
    ComparableAttribute(
        name="state",
        aliases=("status", "lifecycle_state", "operational_status", "install_status"),
    ),
    ComparableAttribute(
        name="criticality",
        aliases=("business_criticality", "priority", "impact", "severity"),
    ),
    ComparableAttribute(name="environment", aliases=("env", "deployment_environment")),
    ComparableAttribute(
        name="escalation_target", aliases=("escalation_group", "escalates_to", "on_call")
    ),
)

DEFAULT_POLICY = ContradictionPolicy(
    comparable_attributes=DEFAULT_COMPARABLE_ATTRIBUTES
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[\s_\-/]+")
_TRIM_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", flags=re.UNICODE)


def _normalise_key(value: Any) -> str:
    """Canonical form of a FIELD NAME for alias matching."""
    return _SEPARATORS.sub("", str(value or "").strip().lower())


def _normalise_value(value: Any) -> str:
    """Canonical form of a VALUE for comparison.

    Case, separator style and surrounding punctuation are formatting, not
    disagreement. Deliberately conservative beyond that: no stemming, no token
    reordering, no similarity — anything fuzzier would decide that two genuinely
    different values agree, which suppresses a real finding.
    """
    text = str(value if value is not None else "").strip().lower()
    text = _TRIM_PUNCT.sub("", text)
    return _SEPARATORS.sub(" ", text).strip()


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContradictionPosition:
    """One source's stated value for the disputed attribute."""

    source_system: str
    value: str                       # verbatim, so the finding quotes the source
    normalised_value: str
    candidate_id: str
    kind: str                        # entity | relationship | evidence
    origin: str                      # observed | inferred
    source_type: str = ""
    source_timestamp: Optional[str] = None
    #: Did this side's candidate actually reach the composed context? False means the
    #: disagreement is named but that side is not quoted — usually a 2.0-B3 T2 budget
    #: trim. Recorded rather than hidden, because a budget must not be allowed to
    #: settle an argument.
    in_context: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_system": self.source_system,
            "value": self.value,
            "normalised_value": self.normalised_value,
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "origin": self.origin,
            "source_type": self.source_type,
            "source_timestamp": self.source_timestamp,
            "in_context": self.in_context,
        }


@dataclass(frozen=True)
class Contradiction:
    """A material disagreement between sources about one attribute of one subject.

    Carries no verdict, no preferred side and no severity: there is nothing here to
    rank the sources by, and inventing a scale would be the quiet winner-picking the
    story forbids, restated as a number.
    """

    subject: str
    attribute: str
    positions: Tuple[ContradictionPosition, ...]

    @property
    def source_systems(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for position in self.positions:
            if position.source_system not in seen:
                seen.append(position.source_system)
        return tuple(seen)

    @property
    def distinct_values(self) -> int:
        return len({p.normalised_value for p in self.positions})

    @property
    def fully_in_context(self) -> bool:
        """Every side of the disagreement is quoted in the composed context."""
        return all(p.in_context for p in self.positions)

    @property
    def summary(self) -> str:
        return render_contradiction(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "attribute": self.attribute,
            "source_systems": list(self.source_systems),
            "distinct_values": self.distinct_values,
            "fully_in_context": self.fully_in_context,
            "positions": [p.to_dict() for p in self.positions],
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ContradictionReport:
    """Every disagreement found, and an honest count of any not reported."""

    contradictions: Tuple[Contradiction, ...] = ()
    total_detected: int = 0

    @property
    def omitted(self) -> int:
        return max(0, self.total_detected - len(self.contradictions))

    @property
    def any_found(self) -> bool:
        return bool(self.contradictions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.total_detected,
            "reported": len(self.contradictions),
            "omitted": self.omitted,
            "contradictions": [c.to_dict() for c in self.contradictions],
        }


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def _read(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _first(obj: Any, names: Sequence[str]) -> Any:
    for name in names:
        value = _read(obj, name)
        if value is not None and str(value).strip():
            return value
    return None


def _field_sources(payload: Any) -> List[Mapping[str, Any]]:
    """The payload's own mapping plus every nested holder it exposes."""
    holders: List[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        holders.append(payload)
    else:
        holders.append(
            {k: v for k, v in vars(payload).items()} if hasattr(payload, "__dict__") else {}
        )
    for name in _NESTED_FIELD_HOLDERS:
        nested = _read(payload, name)
        if isinstance(nested, Mapping):
            holders.append(nested)
    return holders


def _subject_of(candidate: Any, payload: Any) -> Optional[str]:
    """The thing being described, or None when the candidate names none.

    Read from the payload first (the source record's own naming), then from the
    candidate. No subject means no claim — we never infer what an item is about.
    """
    for holder in _field_sources(payload):
        value = _first(holder, _SUBJECT_FIELDS)
        if value is not None:
            return str(value).strip()
    value = _first(candidate, _SUBJECT_FIELDS)
    return str(value).strip() if value is not None else None


def _source_system_of(candidate: Any, payload: Any) -> str:
    for holder in _field_sources(payload):
        value = _first(holder, _SOURCE_SYSTEM_FIELDS)
        if value is not None:
            return str(value).strip()
    value = _first(candidate, _SOURCE_SYSTEM_FIELDS)
    return str(value).strip() if value is not None else UNKNOWN_SOURCE


def _claims_of(
    candidate: Any, policy: ContradictionPolicy
) -> List[Tuple[str, ComparableAttribute, str]]:
    """``(subject, attribute, raw value)`` for every declared attribute the
    candidate states structurally. Empty when it states none."""
    payload = getattr(candidate, "payload", None)
    if payload is None:
        payload = candidate
    subject = _subject_of(candidate, payload)
    if not subject:
        return []

    claims: List[Tuple[str, ComparableAttribute, str]] = []
    seen: set = set()
    for holder in _field_sources(payload):
        for field_name, raw in holder.items():
            if raw is None or isinstance(raw, (Mapping, list, tuple, set)):
                continue
            text = str(raw).strip()
            if not text:
                continue  # absence of information is not a position
            attribute = policy.attribute_for(field_name)
            if attribute is None or attribute.name in seen:
                continue
            seen.add(attribute.name)
            claims.append((subject, attribute, text))
    return claims


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _comparison_key(attribute: ComparableAttribute, raw: str) -> str:
    """The value reduced to what comparison actually keys on.

    Runs the declared equivalence class after normalisation, so two declared
    spellings collapse to one position rather than presenting as a disagreement.
    """
    normalised = _normalise_value(raw)
    canonical = attribute.equivalence_of(normalised)
    return canonical if canonical is not None else normalised


def _materially_differs(
    attribute: ComparableAttribute, values: Sequence[str], tolerance: float
) -> bool:
    """Do these normalised values represent a real disagreement?"""
    distinct = sorted(set(values))
    if len(distinct) < 2:
        return False
    if attribute.kind != KIND_NUMERIC:
        return True
    numbers = [_as_number(v) for v in distinct]
    if any(n is None for n in numbers):
        return True  # an unparseable value beside a number is a genuine mismatch
    low, high = min(numbers), max(numbers)  # type: ignore[type-var]
    scale = max(abs(low), abs(high))
    if scale == 0:
        return low != high
    return (abs(high - low) / scale) > max(0.0, tolerance)


def detect_contradictions(
    candidates: Iterable[Any],
    policy: Optional[ContradictionPolicy] = None,
    selected_ids: Optional[Iterable[str]] = None,
) -> ContradictionReport:
    """Find material cross-source disagreements among ``candidates``.

    ``candidates`` are ``context_assembly.Candidate``-shaped items (anything with
    ``candidate_id`` / ``kind`` / ``origin`` / ``payload`` works). ``selected_ids``
    names the ones that reached the composed context; positions outside it are still
    reported, flagged ``in_context=False``, so a budget trim cannot settle an
    argument on its own.

    Returns a report. It never mutates, filters or reorders ``candidates`` — the
    disagreement is surfaced, and choosing between the sides is not this platform's
    call to make.
    """
    policy = policy or DEFAULT_POLICY
    if not policy.enabled:
        return ContradictionReport()

    # Named for what it is — the ids that reached the composed context — rather than
    # "selected", so a structural guard can assert this module never assigns to
    # selection state without tripping over its own read-only bookkeeping.
    in_context_ids = set(selected_ids) if selected_ids is not None else None
    # (subject_key, attribute_name) -> positions
    grouped: Dict[Tuple[str, str], List[ContradictionPosition]] = {}
    display_subject: Dict[str, str] = {}

    for candidate in candidates:
        origin = str(getattr(candidate, "origin", "") or "")
        if policy.require_observed and origin != "observed":
            continue
        try:
            claims = _claims_of(candidate, policy)
        except Exception as exc:  # noqa: BLE001 — one odd payload must not cost the rest
            logger.debug("context_contradictions: claim extraction skipped: %s", exc)
            continue
        if not claims:
            continue

        payload = getattr(candidate, "payload", None) or candidate
        source_system = _source_system_of(candidate, payload) or UNKNOWN_SOURCE
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        for subject, attribute, raw in claims:
            subject_key = _normalise_value(subject)
            display_subject.setdefault(subject_key, subject)
            grouped.setdefault((subject_key, attribute.name), []).append(
                ContradictionPosition(
                    source_system=source_system,
                    value=raw,
                    normalised_value=_comparison_key(attribute, raw),
                    candidate_id=candidate_id,
                    kind=str(getattr(candidate, "kind", "") or ""),
                    origin=origin,
                    source_type=str(getattr(candidate, "source_type", "") or ""),
                    source_timestamp=getattr(candidate, "source_timestamp", None),
                    in_context=(
                        in_context_ids is None or candidate_id in in_context_ids
                    ),
                )
            )

    by_name = {a.name: a for a in policy.comparable_attributes}
    found: List[Contradiction] = []
    for (subject_key, attribute_name), positions in grouped.items():
        attribute = by_name[attribute_name]
        if not _materially_differs(
            attribute,
            [p.normalised_value for p in positions],
            policy.numeric_tolerance_ratio,
        ):
            continue
        systems = {p.source_system for p in positions}
        if len(systems) < max(1, policy.min_distinct_sources):
            # One system disagreeing with itself is a data-quality problem inside
            # that system, not a cross-source disagreement.
            continue
        positions_in_order = tuple(
            sorted(positions, key=lambda p: (p.source_system, p.candidate_id, p.value))
        )
        found.append(
            Contradiction(
                subject=display_subject.get(subject_key, subject_key),
                attribute=attribute_name,
                positions=positions_in_order,
            )
        )

    found.sort(key=lambda c: (c.subject.lower(), c.attribute))
    reported = tuple(found[: max(0, policy.max_reported)])
    if len(found) > len(reported):
        logger.info(
            "context_contradictions: %d disagreement(s) found, %d reported "
            "(max_reported=%d) — the remainder are counted, not hidden",
            len(found), len(reported), policy.max_reported,
        )
    return ContradictionReport(contradictions=reported, total_detected=len(found))


# ---------------------------------------------------------------------------
# Rendering — one wording, checked against resolution language
# ---------------------------------------------------------------------------

#: Wording that would present a disagreement as settled. Blocked at BUILD time, in
#: the same spirit as ``discovery/projection/vocabulary.py``: the point of naming a
#: disagreement is lost the moment the sentence quietly picks a winner, and a phrase
#: like "the correct owner is" reads as a platform verdict the evidence cannot
#: support.
RESOLUTION_LANGUAGE: Tuple[str, ...] = (
    "the correct",
    "the actual",
    "the true",
    "should be",
    "is really",
    "is actually",
    "authoritative value",
    "we resolved",
    "resolved to",
    "we believe",
    "the right",
    "supersedes",
    "takes precedence",
    "is wrong",
    "is incorrect",
    "ignore the",
)


class ContradictionCopyError(ValueError):
    """Rendered contradiction copy asserted a resolution instead of a disagreement."""


def resolution_language_in(text: str) -> List[str]:
    """Prohibited phrases present in ``text`` (lowercased, in declaration order)."""
    lowered = str(text or "").lower()
    return [phrase for phrase in RESOLUTION_LANGUAGE if phrase in lowered]


def assert_no_resolution(text: str) -> str:
    """Return ``text``, or raise if it resolves the disagreement it describes."""
    offenders = resolution_language_in(text)
    if offenders:
        raise ContradictionCopyError(
            f"contradiction copy asserts a resolution ({offenders}): {text!r}. A "
            f"disagreement is reported with both sides named; choosing between them "
            f"is not something the evidence supports."
        )
    return text


def render_contradiction(contradiction: Contradiction) -> str:
    """One neutral sentence naming the subject, the attribute, and every side.

    Every surface renders from here so none composes its own wording — the same rule
    2.0-A1 T5 applies to recommendation copy and 2.0-A3 T3 to ranking reasons.
    """
    sides = "; ".join(
        f"{position.source_system} states \"{position.value}\""
        for position in contradiction.positions
    )
    text = (
        f"Sources disagree on the {contradiction.attribute} of "
        f"{contradiction.subject}: {sides}. Both are reported as observed; "
        f"AgentIQ does not choose between them."
    )
    if not contradiction.fully_in_context:
        text += (
            " One or more of these records was outside this finding's context "
            "budget, so the disagreement is named here rather than quoted in full."
        )
    return assert_no_resolution(text)


#: Prefaces the rendered block. Names the instruction explicitly because a model
#: handed two conflicting facts will otherwise pick the more fluent one and state it
#: plainly — the disagreement would survive detection and die in generation.
SECTION_INSTRUCTION = (
    "The sources below disagree. Report the disagreement itself as part of the "
    "finding. Do not choose a side, average them, or omit either value."
)


def _render_section(summaries: Sequence[str], omitted: int) -> str:
    if not summaries:
        return ""
    lines = [SECTION_INSTRUCTION]
    lines.extend(f"- {s}" for s in summaries)
    if omitted:
        lines.append(
            f"- ({omitted} further disagreement(s) detected and not listed here.)"
        )
    return "\n".join(lines)


def render_contradiction_section(report: Optional[ContradictionReport]) -> str:
    """The prompt/report block for a set of disagreements; empty when there are none."""
    if report is None or not report.any_found:
        return ""
    return _render_section([c.summary for c in report.contradictions], report.omitted)


def render_reported_section(raw: Optional[Mapping[str, Any]]) -> str:
    """:func:`render_contradiction_section` over a serialised report.

    The wording lives in one place, so a consumer holding the ``to_dict()`` form (a
    context package, a stored artifact, an API response) renders exactly what the
    in-memory report would, rather than reassembling the sentence itself.
    """
    if not raw:
        return ""
    entries = raw.get("contradictions") or []
    return _render_section(
        [str(entry.get("summary", "")) for entry in entries if entry.get("summary")],
        int(raw.get("omitted") or 0),
    )


# ---------------------------------------------------------------------------
# Declared configuration
# ---------------------------------------------------------------------------

class ContradictionConfigError(ValueError):
    """The declared ``contradictions`` block is invalid."""


def parse_contradiction_policy(raw: Any) -> ContradictionPolicy:
    """Validate a declared ``contradictions`` block into a :class:`ContradictionPolicy`.

    ``None``/absent yields :data:`DEFAULT_POLICY` — a config file written before this
    story still detects disagreements, rather than shipping the feature switched off.
    A block that IS present and invalid raises, because an operator who configured
    this and got it wrong must be told, not quietly given the defaults.
    """
    if raw is None:
        return DEFAULT_POLICY
    if not isinstance(raw, Mapping):
        raise ContradictionConfigError(
            f"contradictions must be an object, got {type(raw).__name__}"
        )
    data = {k: v for k, v in raw.items() if not str(k).startswith("_")}

    declared = data.get("comparable_attributes")
    if declared is None:
        attributes = DEFAULT_COMPARABLE_ATTRIBUTES
    else:
        if not isinstance(declared, (list, tuple)):
            raise ContradictionConfigError(
                "contradictions.comparable_attributes must be a list"
            )
        parsed: List[ComparableAttribute] = []
        names: set = set()
        for index, entry in enumerate(declared):
            where = f"contradictions.comparable_attributes[{index}]"
            if not isinstance(entry, Mapping):
                raise ContradictionConfigError(f"{where} must be an object")
            name = str(entry.get("name", "")).strip()
            if not name:
                raise ContradictionConfigError(f"{where}.name is required")
            if name in names:
                raise ContradictionConfigError(
                    f"{where}.name {name!r} is declared twice — the second would "
                    f"silently shadow the first"
                )
            names.add(name)
            kind = str(entry.get("kind", KIND_CATEGORICAL)).strip().lower()
            if kind not in KNOWN_ATTRIBUTE_KINDS:
                raise ContradictionConfigError(
                    f"{where}.kind must be one of {list(KNOWN_ATTRIBUTE_KINDS)}, "
                    f"got {kind!r}"
                )
            aliases = entry.get("aliases", []) or []
            if not isinstance(aliases, (list, tuple)):
                raise ContradictionConfigError(f"{where}.aliases must be a list")
            groups = entry.get("equivalences", []) or []
            if not isinstance(groups, (list, tuple)):
                raise ContradictionConfigError(f"{where}.equivalences must be a list")
            equivalences: List[Tuple[str, ...]] = []
            for group in groups:
                if not isinstance(group, (list, tuple)) or len(group) < 2:
                    raise ContradictionConfigError(
                        f"{where}.equivalences entries must each list 2+ spellings "
                        f"that mean the same value"
                    )
                equivalences.append(tuple(str(m) for m in group))
            parsed.append(
                ComparableAttribute(
                    name=name,
                    aliases=tuple(str(a) for a in aliases),
                    kind=kind,
                    equivalences=tuple(equivalences),
                )
            )
        attributes = tuple(parsed)

    tolerance = data.get("numeric_tolerance_ratio", 0.10)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise ContradictionConfigError(
            f"contradictions.numeric_tolerance_ratio must be a number, got {tolerance!r}"
        )
    if not 0.0 <= float(tolerance) < 1.0:
        raise ContradictionConfigError(
            f"contradictions.numeric_tolerance_ratio must be within 0.0..1.0 "
            f"(exclusive of 1.0), got {tolerance}"
        )

    require_observed = data.get("require_observed", True)
    if not isinstance(require_observed, bool):
        raise ContradictionConfigError(
            f"contradictions.require_observed must be true/false, got {require_observed!r}"
        )

    min_sources = data.get("min_distinct_sources", 2)
    if isinstance(min_sources, bool) or not isinstance(min_sources, int) or min_sources < 2:
        raise ContradictionConfigError(
            f"contradictions.min_distinct_sources must be an integer >= 2 — a "
            f"disagreement needs two sources by definition. Got {min_sources!r}."
        )

    max_reported = data.get("max_reported", 5)
    if isinstance(max_reported, bool) or not isinstance(max_reported, int) or max_reported < 1:
        raise ContradictionConfigError(
            f"contradictions.max_reported must be an integer >= 1 — 0 would detect "
            f"disagreements and report none of them. Got {max_reported!r}."
        )

    return ContradictionPolicy(
        comparable_attributes=attributes,
        numeric_tolerance_ratio=float(tolerance),
        require_observed=require_observed,
        min_distinct_sources=int(min_sources),
        max_reported=int(max_reported),
    )


__all__ = [
    "ComparableAttribute",
    "Contradiction",
    "ContradictionConfigError",
    "ContradictionCopyError",
    "ContradictionPolicy",
    "ContradictionPosition",
    "ContradictionReport",
    "DEFAULT_COMPARABLE_ATTRIBUTES",
    "DEFAULT_POLICY",
    "KIND_CATEGORICAL",
    "KIND_NUMERIC",
    "KNOWN_ATTRIBUTE_KINDS",
    "RESOLUTION_LANGUAGE",
    "SECTION_INSTRUCTION",
    "UNKNOWN_SOURCE",
    "assert_no_resolution",
    "detect_contradictions",
    "parse_contradiction_policy",
    "render_contradiction",
    "render_contradiction_section",
    "render_reported_section",
    "resolution_language_in",
]
