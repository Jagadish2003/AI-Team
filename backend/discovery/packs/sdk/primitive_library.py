"""Executable detector primitive library — 2.0-C3 T2 (AT-837).

The runnable half of the primitive set whose ids, parameter contracts, and concept
arity :mod:`.primitives` declares. **One vocabulary, two halves**: this module
implements against those contracts and never restates them — a structural test
pins that the implemented ids are exactly the declared ids, so the library cannot
grow a primitive nobody can author, nor promise one nobody implemented.

What a primitive guarantees its author
--------------------------------------
Everything in the four-part criterion, without the author writing any of it:

* **evidence** — the observed numbers behind the finding, built from the records
  that actually contributed;
* **confidence** — DERIVED from how many independent sources agree, never
  asserted, with the conversation ceiling and the pack's own (lowering-only) caps
  applied (:mod:`.contract`);
* **corroboration** — the agreeing sources, or an explicit single-source cap;
  a windowed join records the join type and the window that produced it;
* **source trace** — a pointer to every contributing record.

And the platform's non-negotiables, structurally rather than by review: findings
speak groups, queues, services, and entities (the signal layer refuses individuals
at admission), concentration wording passes the causal gate, and each primitive's
declared aggregation floor must be cleared before it can emit.

Deterministic by construction
-----------------------------
No wall-clock reads (``as_of`` comes from the caller or from the data), stable
ordering everywhere, and integer/rounded arithmetic at the boundaries — the same
fixture always yields the same findings, which is what makes the authoring
harness and reproducibility possible.

Dependency-free of ``app``: pure functions over :mod:`.signals`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import assert_not_causal, build_pack_contract
from .primitives import PrimitiveSpec, get_primitive, primitive_ids
from .signals import ConceptRecord, SignalSet

#: Lifecycle states treated as resolved. An ageing detector scoped to unresolved
#: work counts everything NOT in this set (including an unknown state — an item
#: whose state was never normalised has not been shown to be finished).
RESOLVED_STATES = frozenset(
    {"resolved", "closed", "cancelled", "canceled", "done", "complete", "completed"}
)
#: Lifecycle states treated as open work.
OPEN_STATES = frozenset(
    {"open", "new", "in_progress", "assigned", "pending", "active", "awaiting", "triage"}
)


class PrimitiveExecutionError(ValueError):
    """A primitive could not be executed as declared (unknown id, bad binding)."""


@dataclass(frozen=True)
class PrimitiveContext:
    """Everything a primitive needs beyond its records and parameters.

    ``as_of`` is the evaluation instant — supplied by the caller or derived from
    the data, never read from the clock. ``confidence_caps`` are the manifest's
    (lowering-only) calibration caps.
    """

    as_of: Optional[datetime] = None
    confidence_caps: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PrimitiveFinding:
    """One finding a primitive emitted, contract already complete."""

    detector_id: str
    primitive: str
    subject: str
    title: str
    statement: str
    metric_value: float
    threshold: float
    signal_source: str
    source_systems: Tuple[str, ...]
    contract: Mapping[str, Any]

    @property
    def evidence(self) -> Mapping[str, Any]:
        return self.contract.get("evidence", {})

    @property
    def confidence_level(self) -> str:
        return str(self.contract.get("confidence", {}).get("level", ""))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detectorId": self.detector_id,
            "primitive": self.primitive,
            "subject": self.subject,
            "title": self.title,
            "statement": self.statement,
            "metricValue": self.metric_value,
            "threshold": self.threshold,
            "signalSource": self.signal_source,
            "sourceSystems": list(self.source_systems),
            "findingContract": dict(self.contract),
        }


# ── Shared helpers ────────────────────────────────────────────────────────────


def _resolved_parameters(spec: PrimitiveSpec, parameters: Mapping[str, Any]) -> Dict[str, Any]:
    """Author parameters over the primitive's declared defaults."""
    resolved: Dict[str, Any] = {
        parameter.name: parameter.default
        for parameter in spec.parameters
        if parameter.default is not None
    }
    resolved.update({key: value for key, value in parameters.items() if value is not None})
    return resolved


def _as_of(signals: SignalSet, context: PrimitiveContext) -> Optional[datetime]:
    return context.as_of or signals.default_as_of()


def _within_window(
    records: Sequence[ConceptRecord],
    *,
    as_of: Optional[datetime],
    window_days: Optional[int],
) -> List[ConceptRecord]:
    """Records observed inside the trailing window. Inclusive at both ends.

    A record with no parseable timestamp cannot be placed in a window, so it is
    excluded rather than assumed recent — the same posture the correlation-window
    service takes on an unparseable instant.
    """
    if as_of is None or not window_days:
        return [record for record in records if record.observed_at is not None]
    floor = as_of - timedelta(days=int(window_days))
    return [
        record
        for record in records
        if record.observed_at is not None and floor <= record.observed_at <= as_of
    ]


def _subject_of(record: ConceptRecord, anchor: str = "") -> str:
    """The group/queue/service/entity a finding is about — never an individual."""
    if anchor:
        keyed = record.group_key(anchor)
        if keyed:
            return keyed
    return (
        record.entity_reference
        or record.artifact
        or record.actor_group
        or record.signature
        or record.record_id
    )


def _grouped(
    records: Sequence[ConceptRecord], key: Callable[[ConceptRecord], str]
) -> List[Tuple[str, List[ConceptRecord]]]:
    """Group records deterministically, dropping entries with no group key."""
    buckets: Dict[str, List[ConceptRecord]] = {}
    for record in records:
        bucket = key(record)
        if not bucket:
            continue
        buckets.setdefault(bucket, []).append(record)
    return sorted(buckets.items(), key=lambda item: item[0])


def _source_systems(records: Sequence[ConceptRecord]) -> List[str]:
    seen: List[str] = []
    for record in records:
        if record.source_system not in seen:
            seen.append(record.source_system)
    return sorted(seen)


def _span(records: Sequence[ConceptRecord]) -> Tuple[str, str]:
    stamps = sorted(
        record.observed_at for record in records if record.observed_at is not None
    )
    if not stamps:
        return "", ""
    return stamps[0].isoformat(), stamps[-1].isoformat()


def _artifacts(records: Sequence[ConceptRecord], *, limit: int = 25) -> List[Dict[str, Any]]:
    """Source-trace pointers, bounded but never empty.

    Bounded because a finding over ten thousand records must not carry ten
    thousand pointers; the count stays exact in the evidence, and the trace
    records that it is a sample, so nothing is silently dropped.
    """
    ordered = sorted(records, key=lambda record: record.record_id)
    pointers = [record.to_artifact() for record in ordered[:limit]]
    if len(ordered) > limit:
        pointers.append(
            {
                "type": "sample_note",
                "id": f"{len(ordered)} contributing records, {limit} pointers sampled",
                "sampled": limit,
                "total": len(ordered),
            }
        )
    return pointers


def _finding(
    *,
    declaration_id: str,
    primitive: str,
    subject: str,
    title: str,
    statement: str,
    metric_value: float,
    threshold: float,
    records: Sequence[ConceptRecord],
    evidence: Mapping[str, Any],
    context: PrimitiveContext,
    window_gated: bool = False,
    join_type: str = "",
) -> PrimitiveFinding:
    systems = _source_systems(records)
    contract = build_pack_contract(
        evidence=dict(evidence),
        source_systems=systems,
        artifacts=_artifacts(records),
        caps=context.confidence_caps,
        window_gated=window_gated,
        join_type=join_type,
        statement=statement,
    )
    return PrimitiveFinding(
        detector_id=declaration_id,
        primitive=primitive,
        subject=subject,
        title=title,
        statement=statement,
        metric_value=float(metric_value),
        threshold=float(threshold),
        signal_source=systems[0] if systems else "",
        source_systems=tuple(systems),
        contract=contract,
    )


# ── recurrence ────────────────────────────────────────────────────────────────


def _run_recurrence(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """The same normalised fact recurring above a count within a window."""
    as_of = _as_of(signals, context)
    window_days = int(parameters["window_days"])
    minimum = int(parameters["min_occurrences"])
    group_by = str(parameters.get("group_by", "signature"))
    min_groups = parameters.get("min_distinct_actor_groups")

    records = _within_window(
        signals.for_concept(concepts[0]), as_of=as_of, window_days=window_days
    )
    findings: List[PrimitiveFinding] = []
    for subject, bucket in _grouped(records, lambda record: record.group_key(group_by)):
        occurrences = len(bucket)
        if occurrences < minimum:
            continue
        actor_groups = sorted({record.actor_group for record in bucket if record.actor_group})
        if min_groups is not None and len(actor_groups) < int(min_groups):
            continue
        first_seen, last_seen = _span(bucket)
        statement = (
            f"The same {concepts[0]} recurs {occurrences} times in {window_days} days "
            f"({group_by}: {subject})."
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="recurrence",
                subject=subject,
                title=title,
                statement=statement,
                metric_value=occurrences,
                threshold=minimum,
                records=bucket,
                evidence={
                    "concept": concepts[0],
                    "grouped_by": group_by,
                    "subject": subject,
                    "occurrences": occurrences,
                    "window_days": window_days,
                    "distinct_actor_groups": len(actor_groups),
                    "actor_groups": actor_groups,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                },
                context=context,
            )
        )
    return sorted(findings, key=lambda f: (-f.metric_value, f.subject))


# ── threshold_vs_baseline ─────────────────────────────────────────────────────


def _run_threshold_vs_baseline(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """A measure departing from ITS OWN baseline — never a global mean.

    Each subject is judged only against the baseline carried on its own records
    (``{metric}_baseline`` with ``baseline_runs``), the per-queue discipline the
    first-party ageing detector established. A subject with no established
    baseline does not fire; it is unbaselined, not compliant.
    """
    as_of = _as_of(signals, context)
    metric = str(parameters["metric"])
    departure_pct = float(parameters["departure_pct"])
    direction = str(parameters.get("direction", "above"))
    min_baseline_runs = int(parameters.get("min_baseline_runs", 3))
    window_days = int(parameters.get("window_days", 30))

    records = _within_window(
        signals.for_concept(concepts[0]), as_of=as_of, window_days=window_days
    )
    findings: List[PrimitiveFinding] = []
    for subject, bucket in _grouped(records, _subject_of):
        latest = max(
            bucket,
            key=lambda record: (
                record.observed_at.timestamp() if record.observed_at else 0.0,
                record.record_id,
            ),
        )
        current = latest.metric(metric)
        baseline = latest.metric(f"{metric}_baseline")
        runs = latest.metric("baseline_runs") or 0.0
        if current is None or baseline is None or baseline <= 0.0:
            continue
        if int(runs) < min_baseline_runs:
            continue
        departure = round((current - baseline) / baseline, 4)
        if direction == "above" and departure < departure_pct:
            continue
        if direction == "below" and departure > -departure_pct:
            continue
        if direction == "either" and abs(departure) < departure_pct:
            continue
        movement = "above" if departure >= 0 else "below"
        statement = (
            f"{metric} for {subject} sits {abs(departure) * 100:.0f}% {movement} its "
            f"own baseline over {window_days} days."
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="threshold_vs_baseline",
                subject=subject,
                title=title,
                statement=statement,
                metric_value=departure,
                threshold=departure_pct,
                records=bucket,
                evidence={
                    "concept": concepts[0],
                    "subject": subject,
                    "metric": metric,
                    "current_value": current,
                    "baseline_value": baseline,
                    "departure_pct": departure,
                    "baseline_runs": int(runs),
                    "baseline_scope": "per_subject",
                    "window_days": window_days,
                },
                context=context,
            )
        )
    return sorted(findings, key=lambda f: (-abs(f.metric_value), f.subject))


# ── ageing ────────────────────────────────────────────────────────────────────


def _in_state_scope(record: ConceptRecord, scope: str) -> bool:
    if scope == "any":
        return True
    if scope == "unresolved":
        return record.state not in RESOLVED_STATES
    return record.state in OPEN_STATES


def _run_ageing(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """Work items sitting in a state longer than a threshold."""
    as_of = _as_of(signals, context)
    if as_of is None:
        return []
    min_age_days = int(parameters["min_age_days"])
    min_items = int(parameters.get("min_items", 3))
    age_from = str(parameters.get("age_from", "opened_at"))
    scope = str(parameters.get("state_scope", "open"))

    aged: List[Tuple[ConceptRecord, float]] = []
    for record in signals.for_concept(concepts[0]):
        if not _in_state_scope(record, scope):
            continue
        anchor = record.timestamp(age_from)
        if anchor is None or anchor > as_of:
            continue
        age_days = round((as_of - anchor).total_seconds() / 86400.0, 2)
        if age_days >= min_age_days:
            aged.append((record, age_days))

    ages = {record.record_id: age for record, age in aged}
    findings: List[PrimitiveFinding] = []
    for subject, bucket in _grouped([record for record, _ in aged], _subject_of):
        # The aggregation floor: one aged item is a record, not a finding.
        if len(bucket) < min_items:
            continue
        bucket_ages = [ages[record.record_id] for record in bucket]
        oldest = max(bucket_ages)
        statement = (
            f"{len(bucket)} {concepts[0]} items in {subject} have been {scope} for at "
            f"least {min_age_days} days (oldest {oldest:.0f} days, measured from "
            f"{age_from})."
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="ageing",
                subject=subject,
                title=title,
                statement=statement,
                metric_value=len(bucket),
                threshold=min_items,
                records=bucket,
                evidence={
                    "concept": concepts[0],
                    "subject": subject,
                    "aged_items": len(bucket),
                    "min_age_days": min_age_days,
                    "oldest_age_days": oldest,
                    "median_age_days": round(statistics.median(bucket_ages), 2),
                    "age_from": age_from,
                    "state_scope": scope,
                    "as_of": as_of.isoformat(),
                },
                context=context,
            )
        )
    return sorted(findings, key=lambda f: (-f.metric_value, f.subject))


# ── oscillation ───────────────────────────────────────────────────────────────


def _run_oscillation(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """Repeated back-and-forth transitions between groups or states."""
    as_of = _as_of(signals, context)
    min_hops = int(parameters["min_hops"])
    kind = str(parameters.get("transition_kind", "assignment"))
    window_days = int(parameters.get("window_days", 30))
    min_participants = int(parameters.get("min_distinct_participants", 2))

    records = _within_window(
        signals.for_concept(concepts[0]), as_of=as_of, window_days=window_days
    )
    qualifying: List[Tuple[ConceptRecord, int, List[str]]] = []
    for record in records:
        hops = [t for t in record.transitions if t.kind == kind]
        participants = sorted({t.participant for t in hops if t.participant})
        if len(hops) >= min_hops and len(participants) >= min_participants:
            qualifying.append((record, len(hops), participants))

    by_id = {record.record_id: (hops, participants) for record, hops, participants in qualifying}
    findings: List[PrimitiveFinding] = []
    for subject, bucket in _grouped([record for record, _, _ in qualifying], _subject_of):
        hop_counts = [by_id[record.record_id][0] for record in bucket]
        participants = sorted(
            {
                participant
                for record in bucket
                for participant in by_id[record.record_id][1]
            }
        )
        worst = max(hop_counts)
        statement = (
            f"{len(bucket)} {concepts[0]} items in {subject} moved between "
            f"{len(participants)} groups {min_hops} or more times "
            f"(worst: {worst} {kind} hops)."
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="oscillation",
                subject=subject,
                title=title,
                statement=statement,
                metric_value=worst,
                threshold=min_hops,
                records=bucket,
                evidence={
                    "concept": concepts[0],
                    "subject": subject,
                    "oscillating_items": len(bucket),
                    "max_hops": worst,
                    "median_hops": round(statistics.median(hop_counts), 2),
                    "transition_kind": kind,
                    "distinct_participants": len(participants),
                    "participants": participants,
                    "window_days": window_days,
                },
                context=context,
            )
        )
    return sorted(findings, key=lambda f: (-f.metric_value, f.subject))


# ── concentration_traversal ───────────────────────────────────────────────────


def _concentration_statement(
    *, anchor: str, dependent_count: int, item_count: int, max_depth: int
) -> str:
    """Concentration-shaped wording, self-validated against the causal gate.

    "Concentrates on" is an observation; "caused by" is a claim reserved for the
    causal engine. The statement validates its own output so the wording contract
    cannot regress silently — the first-party hotspot detector's discipline,
    applied to authored packs.
    """
    statement = (
        f"Work across {dependent_count} dependents concentrates on a shared "
        f"dependency ({anchor}) within {max_depth} hop(s) — {item_count} items."
    )
    assert_not_causal(statement)
    return statement


def _run_concentration_traversal(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """Work concentrating on a shared entity, reached by depth-bounded traversal."""
    as_of = _as_of(signals, context)
    max_depth = int(parameters["max_depth"])
    min_dependents = int(parameters["min_dependents"])
    anchor_field = str(parameters.get("anchor", "entity_reference"))
    window_days = int(parameters.get("window_days", 30))
    require_corroboration = bool(parameters.get("require_corroboration", False))

    records: List[ConceptRecord] = []
    for concept in concepts:
        records.extend(
            _within_window(
                signals.for_concept(concept), as_of=as_of, window_days=window_days
            )
        )

    # Every entity a record names is a candidate anchor; traversal then attributes
    # a record to the anchors its entity depends on, up to the depth bound.
    direct: Dict[str, List[ConceptRecord]] = {}
    for record in records:
        entity = record.group_key(anchor_field) or _subject_of(record, anchor_field)
        if entity:
            direct.setdefault(entity, []).append(record)

    findings: List[PrimitiveFinding] = []
    for anchor in sorted(direct):
        attributed: Dict[str, List[ConceptRecord]] = {anchor: list(direct[anchor])}
        for dependent in signals.dependents_of(anchor, max_depth=max_depth):
            if dependent in direct:
                attributed[dependent] = list(direct[dependent])
        dependents = sorted(name for name in attributed if name != anchor)
        if len(dependents) < min_dependents:
            continue

        contributing = [
            record for name in sorted(attributed) for record in attributed[name]
        ]
        systems = _source_systems(contributing)
        if require_corroboration and len(systems) < 2:
            continue

        statement = _concentration_statement(
            anchor=anchor,
            dependent_count=len(dependents),
            item_count=len(contributing),
            max_depth=max_depth,
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="concentration_traversal",
                subject=anchor,
                title=title,
                statement=statement,
                metric_value=len(dependents),
                threshold=min_dependents,
                records=contributing,
                evidence={
                    "concepts": list(concepts),
                    "anchor": anchor,
                    "anchor_field": anchor_field,
                    "dependent_count": len(dependents),
                    "dependents": dependents,
                    "item_count": len(contributing),
                    "max_depth": max_depth,
                    "window_days": window_days,
                    "relationship": "concentration",
                },
                context=context,
            )
        )
    return sorted(findings, key=lambda f: (-f.metric_value, f.subject))


# ── co_occurrence_window ──────────────────────────────────────────────────────


def _run_co_occurrence_window(
    *,
    declaration_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: PrimitiveContext,
) -> List[PrimitiveFinding]:
    """Two concepts co-occurring inside a bounded correlation window.

    A pair outside the window contributes NOTHING — not a weaker signal, nothing.
    That is the correlation-window discipline: coincidence must never inflate
    confidence. Each second-concept record matches at most its NEAREST qualifying
    first-concept record, so a busy window cannot inflate the pair count
    combinatorially.
    """
    window_minutes = int(parameters["window_minutes"])
    min_pairs = int(parameters.get("min_pairs", 2))
    ordering = str(parameters.get("ordering", "either"))
    window = timedelta(minutes=window_minutes)

    first = [r for r in signals.for_concept(concepts[0]) if r.observed_at is not None]
    second = [r for r in signals.for_concept(concepts[1]) if r.observed_at is not None]

    pairs: List[Tuple[ConceptRecord, ConceptRecord, float]] = []
    for candidate in second:
        best: Optional[Tuple[ConceptRecord, float]] = None
        for anchor in first:
            delta = (candidate.observed_at - anchor.observed_at).total_seconds()
            if ordering == "first_before_second" and delta < 0:
                continue
            if abs(delta) > window.total_seconds():
                continue
            if best is None or abs(delta) < abs(best[1]):
                best = (anchor, delta)
        if best is not None:
            pairs.append((best[0], candidate, round(best[1] / 60.0, 2)))

    grouped: Dict[str, List[Tuple[ConceptRecord, ConceptRecord, float]]] = {}
    for anchor, candidate, delta in pairs:
        subject = _subject_of(anchor) or _subject_of(candidate)
        grouped.setdefault(subject, []).append((anchor, candidate, delta))

    findings: List[PrimitiveFinding] = []
    for subject in sorted(grouped):
        bucket = grouped[subject]
        if len(bucket) < min_pairs:
            continue
        deltas = [abs(delta) for _, _, delta in bucket]
        contributing = [record for pair in bucket for record in (pair[0], pair[1])]
        statement = (
            f"{len(bucket)} {concepts[0]} / {concepts[1]} pairs co-occur within "
            f"{window_minutes} minutes for {subject} "
            f"(median gap {statistics.median(deltas):.0f} minutes)."
        )
        findings.append(
            _finding(
                declaration_id=declaration_id,
                primitive="co_occurrence_window",
                subject=subject,
                title=title,
                statement=statement,
                metric_value=len(bucket),
                threshold=min_pairs,
                records=contributing,
                evidence={
                    "concepts": list(concepts),
                    "subject": subject,
                    "pair_count": len(bucket),
                    "window_minutes": window_minutes,
                    "median_gap_minutes": round(statistics.median(deltas), 2),
                    "max_gap_minutes": round(max(deltas), 2),
                    "ordering": ordering,
                    "join_type": "co_occurrence",
                    "within_window": True,
                },
                context=context,
                window_gated=True,
                join_type="co_occurrence",
            )
        )
    return sorted(findings, key=lambda f: (-f.metric_value, f.subject))


# ── Registry ──────────────────────────────────────────────────────────────────

#: Executable implementation per declared primitive id. Keyed by the SAME ids
#: ``primitives.PRIMITIVE_LIBRARY`` declares — a structural test pins the two sets
#: equal, so a declared-but-unimplemented (or implemented-but-undeclared)
#: primitive fails the build rather than a customer's run.
PRIMITIVE_IMPLEMENTATIONS: Dict[str, Callable[..., List[PrimitiveFinding]]] = {
    "recurrence": _run_recurrence,
    "threshold_vs_baseline": _run_threshold_vs_baseline,
    "ageing": _run_ageing,
    "oscillation": _run_oscillation,
    "concentration_traversal": _run_concentration_traversal,
    "co_occurrence_window": _run_co_occurrence_window,
}


def implemented_primitive_ids() -> List[str]:
    """Every primitive id with a runnable implementation, sorted."""
    return sorted(PRIMITIVE_IMPLEMENTATIONS)


def run_primitive(
    primitive_id: str,
    *,
    detector_id: str,
    title: str,
    concepts: Sequence[str],
    parameters: Mapping[str, Any],
    signals: SignalSet,
    context: Optional[PrimitiveContext] = None,
) -> List[PrimitiveFinding]:
    """Execute one primitive and return its findings, each contract-complete.

    Parameters are resolved against the primitive's declared defaults and its
    concept arity is checked, so a caller that bypassed manifest validation still
    cannot run a primitive outside its contract.
    """
    spec = get_primitive(primitive_id)
    implementation = PRIMITIVE_IMPLEMENTATIONS.get(str(primitive_id or "").strip())
    if spec is None or implementation is None:
        raise PrimitiveExecutionError(
            f"{primitive_id!r} is not a runnable primitive; the library provides: "
            f"{', '.join(primitive_ids())}"
        )
    minimum, maximum = spec.concept_arity
    bound = [str(concept) for concept in concepts if str(concept).strip()]
    if len(bound) < minimum or (maximum is not None and len(bound) > maximum):
        raise PrimitiveExecutionError(
            f"primitive {primitive_id!r} binds {minimum}"
            f"{'' if maximum == minimum else f'-{maximum}'} concept(s), got {len(bound)}"
        )
    resolved = _resolved_parameters(spec, parameters)
    for parameter in spec.parameters:
        if parameter.required and parameter.name not in resolved:
            raise PrimitiveExecutionError(
                f"primitive {primitive_id!r} requires parameter {parameter.name!r}"
            )
    return implementation(
        declaration_id=detector_id,
        title=title,
        concepts=bound,
        parameters=resolved,
        signals=signals,
        context=context or PrimitiveContext(),
    )


__all__ = [
    "OPEN_STATES",
    "PRIMITIVE_IMPLEMENTATIONS",
    "PrimitiveContext",
    "PrimitiveExecutionError",
    "PrimitiveFinding",
    "RESOLVED_STATES",
    "implemented_primitive_ids",
    "run_primitive",
]
