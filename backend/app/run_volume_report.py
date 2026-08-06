"""2.0-D4 T4 — the per-run volume picture, assembled where someone can see it.

MSP-B7 already produces the raw material: ``BudgetReport`` counts deferred
events, ``SuppressionReport`` counts floored ones, and the document ingestor
records every skip with its reason. What has been missing is a place where those
land together and a customer-facing surface that renders them. A budget reported
into a JSON blob nobody looks at satisfies the letter of "loud" and none of its
intent.

**What this module does not do.** It computes nothing about volume and enforces
nothing. Every number here is read from the report the enforcing component
already produced — the event budget from ``RunBudget``, the document skips from
the ingestor, the findings count from the run's own output. Recomputing any of
them here would create a second answer to a question that already has one, and
the two would eventually disagree.

**Reading a breach.** ``breached`` on this report means at least one dimension
went past its stated envelope. It does NOT mean anything was lost: three of the
four dimensions defer-and-count, and findings-per-run withholds nothing at all.
The report says which dimension, by how much, and what happened as a result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .scale_envelope import (
    DIM_DOCUMENTS_PER_RUN,
    DIM_EVENTS_PER_RUN,
    DIM_FINDINGS_PER_RUN,
    DIM_SYSTEMS_PER_DEPLOYMENT,
    build_envelope,
)

logger = logging.getLogger(__name__)

VOLUME_REPORT_VERSION = "1.0.0"


@dataclass(frozen=True)
class DimensionObservation:
    """What one run actually did on one dimension, against what was stated."""

    key: str
    label: str
    observed: Optional[int]
    limit: Optional[int]
    unit: str
    basis: str
    degradation: str
    #: Work postponed rather than done. Zero is meaningful; None means the
    #: dimension does not defer at all.
    deferred: Optional[int] = None
    #: Work suppressed below a noise floor — counted, never silent.
    suppressed: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    @property
    def exceeded_by(self) -> Optional[int]:
        if self.limit is None or self.observed is None or self.observed <= self.limit:
            return None
        return self.observed - self.limit

    @property
    def breached(self) -> bool:
        return self.exceeded_by is not None or bool(self.deferred)

    @property
    def utilisation(self) -> Optional[float]:
        """Observed as a fraction of the stated limit, for a progress bar."""
        if not self.limit or self.observed is None:
            return None
        return round(self.observed / self.limit, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "observed": self.observed,
            "limit": self.limit,
            "unit": self.unit,
            "basis": self.basis,
            "degradation": self.degradation,
            "deferred": self.deferred,
            "suppressed": self.suppressed,
            "exceededBy": self.exceeded_by,
            "breached": self.breached,
            "utilisation": self.utilisation,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RunVolumeReport:
    """One run's volume outcome across every stated dimension."""

    run_id: Optional[str]
    observations: Sequence[DimensionObservation]

    @property
    def breached_dimensions(self) -> List[str]:
        return [o.key for o in self.observations if o.breached]

    @property
    def breached(self) -> bool:
        return bool(self.breached_dimensions)

    @property
    def total_deferred(self) -> int:
        return sum(o.deferred or 0 for o in self.observations)

    @property
    def total_suppressed(self) -> int:
        return sum(o.suppressed or 0 for o in self.observations)

    @property
    def headline(self) -> str:
        """One plain sentence a run-health panel can show without composing its own."""
        if not self.breached:
            return "All volume dimensions stayed within the stated envelope."
        parts = []
        if self.total_deferred:
            parts.append(f"{self.total_deferred:,} item(s) deferred to the next run")
        if self.total_suppressed:
            parts.append(f"{self.total_suppressed:,} suppressed below a noise floor")
        tail = f" — {'; '.join(parts)}." if parts else "."
        return (
            f"{len(self.breached_dimensions)} dimension(s) reached the stated envelope"
            f"{tail} Nothing was discarded without a record."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": VOLUME_REPORT_VERSION,
            "runId": self.run_id,
            "breached": self.breached,
            "breachedDimensions": list(self.breached_dimensions),
            "totalDeferred": self.total_deferred,
            "totalSuppressed": self.total_suppressed,
            "headline": self.headline,
            "dimensions": [o.to_dict() for o in self.observations],
        }


# --------------------------------------------------------------------------
# Reading the reports the enforcing components already produced
# --------------------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_observation(run: Mapping[str, Any], dims) -> DimensionObservation:
    """Read the B7 budget reports off the run's cloud-ops runtime block.

    A run may carry several (AWS, Azure, the bridge), each with its own budget
    report; they are summed, because the envelope is stated per RUN.
    """
    dim = dims[DIM_EVENTS_PER_RUN]
    runtime = run.get("cloudOpsRuntime")
    processed = deferred = 0
    notes: List[str] = []
    seen_any = False

    if isinstance(runtime, Mapping):
        for source, block in runtime.items():
            if not isinstance(block, Mapping):
                continue
            budget = block.get("budget") or block.get("budgetReport")
            if not isinstance(budget, Mapping):
                continue
            seen_any = True
            processed += _as_int(budget.get("processed")) or 0
            d = _as_int(budget.get("deferred")) or 0
            deferred += d
            if d:
                reason = budget.get("reason") or "budget exhausted"
                notes.append(f"{source}: {d:,} deferred ({reason})")
            # A poll bound stopping early is a different reason for the same
            # symptom — volume is not time — so it is reported distinctly.
            poll = block.get("poll")
            if isinstance(poll, Mapping) and poll.get("complete") is False:
                notes.append(
                    f"{source}: polling stopped early — "
                    f"{poll.get('reason') or 'bound reached'}; resumes next run"
                )

    return DimensionObservation(
        key=dim.key, label=dim.label,
        observed=processed if seen_any else None,
        limit=dim.limit, unit=dim.unit, basis=dim.basis, degradation=dim.degradation,
        deferred=deferred if seen_any else None,
        notes=notes,
    )


def _document_observation(run: Mapping[str, Any], dims) -> DimensionObservation:
    dim = dims[DIM_DOCUMENTS_PER_RUN]
    block = run.get("documentVolume")
    if not isinstance(block, Mapping):
        return DimensionObservation(
            key=dim.key, label=dim.label, observed=None, limit=dim.limit,
            unit=dim.unit, basis=dim.basis, degradation=dim.degradation,
        )
    extracted = _as_int(block.get("extracted")) or 0
    skipped = block.get("skippedByReason")
    notes: List[str] = []
    deferred = 0
    if isinstance(skipped, Mapping):
        for reason, count in skipped.items():
            n = _as_int(count) or 0
            if not n:
                continue
            notes.append(f"{n:,} skipped: {reason}")
            # Only budget_exceeded is retried; size_capped is a settled outcome.
            if reason == "budget_exceeded":
                deferred += n
    return DimensionObservation(
        key=dim.key, label=dim.label, observed=extracted, limit=dim.limit,
        unit=dim.unit, basis=dim.basis, degradation=dim.degradation,
        deferred=deferred, notes=notes,
    )


def _systems_observation(run: Mapping[str, Any], dims) -> DimensionObservation:
    dim = dims[DIM_SYSTEMS_PER_DEPLOYMENT]
    systems = run.get("succeeded")
    if not isinstance(systems, (list, tuple)):
        inputs = run.get("inputs")
        systems = (inputs or {}).get("systems") if isinstance(inputs, Mapping) else None
    observed = len(systems) if isinstance(systems, (list, tuple)) else None
    return DimensionObservation(
        key=dim.key, label=dim.label, observed=observed, limit=dim.limit,
        unit=dim.unit, basis=dim.basis, degradation=dim.degradation,
        notes=["Limit is commercial (licence-scoped), not an engineering ceiling."],
    )


def _findings_observation(run: Mapping[str, Any], dims) -> DimensionObservation:
    dim = dims[DIM_FINDINGS_PER_RUN]
    opps = run.get("opportunities")
    observed = len(opps) if isinstance(opps, (list, tuple)) else None
    notes: List[str] = []
    if dim.limit is not None and observed is not None and observed > dim.limit:
        notes.append(
            f"{observed:,} findings exceeds the reporting threshold of "
            f"{dim.limit:,}. Every finding is still served — this is a prompt to "
            "check for a detector defect or a duplicated estate, not a truncation."
        )
    return DimensionObservation(
        key=dim.key, label=dim.label, observed=observed, limit=dim.limit,
        unit=dim.unit, basis=dim.basis, degradation=dim.degradation,
        notes=notes,
    )


def build_run_volume_report(run: Optional[Mapping[str, Any]]) -> RunVolumeReport:
    """Assemble one run's volume report from the reports it already carries.

    Never raises: a run-health surface must render something even for a run
    whose record predates this report, and a missing dimension is reported as
    ``observed: null`` rather than as a zero that would read as "nothing
    happened".
    """
    record = run if isinstance(run, Mapping) else {}
    dims = build_envelope()
    observations: List[DimensionObservation] = []
    for builder in (_event_observation, _document_observation,
                    _systems_observation, _findings_observation):
        try:
            observations.append(builder(record, dims))
        except Exception as exc:  # noqa: BLE001 - one bad block must not hide the rest
            logger.warning("Volume observation failed: %s", exc)
    return RunVolumeReport(run_id=record.get("runId") or record.get("id"),
                           observations=tuple(observations))


def document_volume_block(
    extracted: int, skipped_by_reason: Optional[Mapping[str, int]] = None
) -> Dict[str, Any]:
    """Shape a ``documentVolume`` block for a run record.

    Document ingestion currently runs OUTSIDE the discovery run (there is no
    call to ``ingest_documents`` in ``discovery/runner.py``), so a run record
    carries no document volume today and the report honestly says "not
    observed" rather than a zero that would read as "nothing was skipped".
    This helper is the ready-made path for whoever wires the two together, so
    the block is shaped once rather than invented at the call site.
    """
    return {
        "extracted": int(extracted),
        "skippedByReason": {str(k): int(v) for k, v in (skipped_by_reason or {}).items()},
    }


__all__ = [
    "VOLUME_REPORT_VERSION",
    "document_volume_block",
    "DimensionObservation",
    "RunVolumeReport",
    "build_run_volume_report",
]
