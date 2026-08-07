"""2.0-D4 T4 — the stated scale envelope, and how honest each number is.

**This module is modelled on ``discovery/signals/ops_calibration.py`` deliberately.**
MSP-B7/B8 already got this discipline right once for cloud events: a number is
stated, the measurement it came from is cited, and where the evidence stops is
said out loud rather than glossed. That file distinguishes a quantitatively
derived budget from operationally-justified floors, and this one carries the same
distinction across all four dimensions the story names.

**The rule that shapes everything here: a documented envelope with no
measurement behind it is a marketing number.** So every dimension declares a
``basis``:

* ``measured`` — derived from a reproducible measurement, which is cited;
* ``operationally_justified`` — a defensible default from domain reasoning,
  not yet measured;
* ``provisional`` — a first guess, expected to be wrong.

Only ONE of the four dimensions is currently ``measured``, and pretending
otherwise would be exactly the failure this subtask exists to prevent.

**The second rule: loud, never silent.** Every dimension declares what happens
at its edge, and none of them answers "results are dropped". Three enforce by
deferring-and-counting (the work is postponed and the postponement is reported);
one — findings per run — deliberately enforces NOTHING and only reports, because
a finding is the product's output and silently withholding one would be a far
worse failure than a large list.

**The third rule: volume is not time.** The native cloud connectors bound a run
three ways — the B7 event budget, a per-scope poll cap, and a wall-clock deadline
— precisely because a throttled provider exhausts patience long before it
exhausts a volume budget. An envelope stated purely in volume is therefore true
on a fast estate and false on a throttled one, so each dimension records the
CONDITIONS under which its number holds.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

ENVELOPE_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Bases — the same vocabulary ops_calibration.py and the A2/A3 configs use.
# --------------------------------------------------------------------------

BASIS_MEASURED = "measured"
BASIS_OPERATIONALLY_JUSTIFIED = "operationally_justified"
BASIS_PROVISIONAL = "provisional"
RECOGNISED_BASES: Tuple[str, ...] = (
    BASIS_MEASURED,
    BASIS_OPERATIONALLY_JUSTIFIED,
    BASIS_PROVISIONAL,
)

# --------------------------------------------------------------------------
# What happens at the edge. None of these is "drop it".
# --------------------------------------------------------------------------

#: Work past the limit is postponed and counted; the next run resumes it.
DEGRADE_DEFER_AND_COUNT = "defer_and_count"
#: The action is refused up front with a named reason (nothing is half-done).
DEGRADE_REFUSE_WITH_REASON = "refuse_with_reason"
#: Nothing is withheld; exceeding the envelope is reported as a fact.
DEGRADE_REPORT_ONLY = "report_only"

DEGRADATION_MODES: Tuple[str, ...] = (
    DEGRADE_DEFER_AND_COUNT,
    DEGRADE_REFUSE_WITH_REASON,
    DEGRADE_REPORT_ONLY,
)

#: The four dimensions the story names, as stable keys.
DIM_EVENTS_PER_RUN = "events_per_run"
DIM_DOCUMENTS_PER_RUN = "documents_per_run"
DIM_SYSTEMS_PER_DEPLOYMENT = "systems_per_deployment"
DIM_FINDINGS_PER_RUN = "findings_per_run"


@dataclass(frozen=True)
class Dimension:
    """One stated volume limit, and everything needed to judge it."""

    key: str
    label: str
    limit: Optional[int]
    unit: str
    basis: str
    #: Where the number came from. For a measured basis this cites the run.
    derivation: str
    #: The module/function that actually enforces it. "" when nothing does.
    enforced_by: str
    degradation: str
    #: What a customer sees when the edge is reached.
    degradation_detail: str
    #: When the stated number is true, and when it is not.
    conditions: str
    #: Set when the dimension is stated but NOT enforced anywhere.
    declared_gap: Optional[str] = None

    @property
    def is_enforced(self) -> bool:
        return bool(self.enforced_by)

    @property
    def is_measured(self) -> bool:
        return self.basis == BASIS_MEASURED

    def exceeded_by(self, observed: Optional[int]) -> Optional[int]:
        """How far past the limit an observation sits, or None if within."""
        if self.limit is None or observed is None or observed <= self.limit:
            return None
        return observed - self.limit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "limit": self.limit,
            "unit": self.unit,
            "basis": self.basis,
            "derivation": self.derivation,
            "enforcedBy": self.enforced_by or None,
            "isEnforced": self.is_enforced,
            "degradation": self.degradation,
            "degradationDetail": self.degradation_detail,
            "conditions": self.conditions,
            "declaredGap": self.declared_gap,
        }


def _b8_event_budget() -> int:
    """The B7/B8 event budget, read from its own source of truth.

    Imported rather than copied: a second literal here would drift the moment
    someone recalibrated ``ops_calibration.B8_MEASUREMENTS``, and an envelope
    that disagrees with the thing enforcing it is worse than no envelope.
    """
    try:
        from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET

        return int(CALIBRATED_RUN_EVENT_BUDGET)
    except Exception:  # pragma: no cover - offline-safe fallback
        return 250_000


def _document_budget_bytes() -> int:
    from discovery.ingest.documents import _DEFAULT_EXTRACTION_BUDGET_BYTES

    return int(_DEFAULT_EXTRACTION_BUDGET_BYTES)


def _document_max_file_bytes() -> int:
    from discovery.ingest.documents import _DEFAULT_MAX_FILE_BYTES

    return int(_DEFAULT_MAX_FILE_BYTES)


#: Average extracted bytes per document, used only to express the document
#: budget as a document COUNT. Deliberately conservative and clearly provisional
#: — no corpus has been measured, and a wrong average makes the count wrong
#: without making the enforced BYTE budget wrong.
ASSUMED_BYTES_PER_DOCUMENT = 256 * 1024  # 256 KiB


def _documents_per_run() -> int:
    return int(_document_budget_bytes() // ASSUMED_BYTES_PER_DOCUMENT)


def build_envelope() -> Dict[str, Dimension]:
    """The four stated dimensions. Built per call so env overrides are honoured."""
    event_budget = _b8_event_budget()
    doc_budget = _document_budget_bytes()
    doc_count = _documents_per_run()

    dims = [
        Dimension(
            key=DIM_EVENTS_PER_RUN,
            label="Cloud events processed per run",
            limit=event_budget,
            unit="events",
            basis=BASIS_MEASURED,
            derivation=(
                "Derived in discovery/signals/ops_calibration.py from the MSP-B8 "
                "month-scale validation run recorded in docs/MSP-B8_VOLUME_VALIDATION.md "
                f"(30,225 events/month measured): ceil(8 x month) rounded up to {event_budget:,}. "
                "At the measured 1.474 ms/event that is roughly a six-minute worst-case "
                "ingest stage."
            ),
            enforced_by="discovery/signals/budget.py::RunBudget",
            degradation=DEGRADE_DEFER_AND_COUNT,
            degradation_detail=(
                "Events past the budget are deferred, not dropped. BudgetReport records "
                "budget/processed/deferred/seen/breached, the deferral window, and the "
                "per-source split; the checkpoint does not advance past deferred volume, "
                "so the next run resumes exactly where this one stopped."
            ),
            conditions=(
                "Holds for a provider responding normally. A throttled source exhausts "
                "the wall-clock deadline (CLOUD_EVENT_POLL_DEADLINE_SECONDS, default 180s) "
                "or the per-scope poll cap (CLOUD_EVENT_MAX_POLLS_PER_SCOPE, default 4) "
                "long before the volume budget, and stops early with a named reason."
            ),
        ),
        Dimension(
            key=DIM_DOCUMENTS_PER_RUN,
            label="Documents extracted per run",
            limit=doc_count,
            unit="documents",
            basis=BASIS_OPERATIONALLY_JUSTIFIED,
            derivation=(
                f"The enforced limit is a BYTE budget of {doc_budget:,} bytes "
                f"({doc_budget // (1024*1024)} MiB) per run, with a per-file cap of "
                f"{_document_max_file_bytes() // (1024*1024)} MiB. The document COUNT "
                f"above is that budget divided by an assumed "
                f"{ASSUMED_BYTES_PER_DOCUMENT // 1024} KiB average extracted size — an "
                "assumption, not a measurement, which is why this dimension is not "
                "marked measured."
            ),
            enforced_by="discovery/ingest/documents.py::DocumentIngestor",
            degradation=DEGRADE_DEFER_AND_COUNT,
            degradation_detail=(
                "A file larger than the per-file cap is skipped with reason "
                "'size_capped' and DOES advance its checkpoint (the outcome is "
                "deterministic — it will be too large next run too). A file past the "
                "per-run budget is skipped with reason 'budget_exceeded' and does NOT "
                "advance, so it is retried next run. Every skip emits an "
                "ingestion.artifact_skipped event carrying its reason."
            ),
            conditions=(
                "Assumes text-extractable documents. A corpus of scanned images "
                "consumes the byte budget while yielding little text, so the document "
                "count reached will be lower than stated."
            ),
        ),
        Dimension(
            key=DIM_SYSTEMS_PER_DEPLOYMENT,
            label="Connected systems per deployment",
            limit=None,  # commercially scoped per licence, not engineering-bounded
            unit="systems",
            basis=BASIS_PROVISIONAL,
            derivation=(
                "No engineering limit has been measured. What exists is a COMMERCIAL "
                "limit: the licence payload's limits.max_systems, enforced at connect "
                "time by app/license_limits.py, which defaults to unlimited on a valid "
                "licence that scopes none. The largest configuration exercised in "
                "testing is well below any plausible engineering ceiling, so stating a "
                "number here would be inventing one."
            ),
            enforced_by="app/license_limits.py (commercial scope only)",
            degradation=DEGRADE_REFUSE_WITH_REASON,
            degradation_detail=(
                "A connect attempt beyond the licensed system count is refused up front "
                "with a plain-language reason. Nothing is half-connected, and existing "
                "connections are untouched."
            ),
            conditions=(
                "The commercial limit is per licence and says nothing about throughput. "
                "Each additional system adds ingest time roughly linearly, so a large "
                "estate meets the per-run wall-clock bounds before any system count."
            ),
            declared_gap=(
                "No measured engineering envelope for system count. Closing it needs a "
                "run against a deliberately large estate, which no environment currently "
                "has. Recorded rather than guessed."
            ),
        ),
        Dimension(
            key=DIM_FINDINGS_PER_RUN,
            label="Findings produced per run",
            limit=SOFT_FINDINGS_ENVELOPE,
            unit="findings",
            basis=BASIS_PROVISIONAL,
            derivation=(
                f"{SOFT_FINDINGS_ENVELOPE:,} is a REPORTING threshold, not a cap. It is "
                "set at roughly an order of magnitude above the largest run observed in "
                "development, on the reasoning that a run producing more than this has "
                "almost certainly hit a detector defect or a duplicated estate rather "
                "than found that many real problems. Unmeasured, and expected to move."
            ),
            enforced_by="",  # deliberately nothing — see degradation_detail
            degradation=DEGRADE_REPORT_ONLY,
            degradation_detail=(
                "NOTHING is withheld. A finding is the product's output, and silently "
                "dropping one to satisfy a volume target would be a far worse failure "
                "than a long list — it would mean the customer never learns a real "
                "problem exists. Exceeding this threshold is reported as a fact on the "
                "run's volume report so it can be investigated, and every finding is "
                "still served."
            ),
            conditions=(
                "Scales with estate size and with the number of packs selected. A "
                "multi-pack run legitimately produces more findings than a single-pack "
                "run over the same data, so the threshold is deliberately generous."
            ),
            declared_gap=(
                "This dimension is stated and reported but NOT enforced, by design. If a "
                "hard cap is ever wanted it must defer-and-count like the others, never "
                "truncate."
            ),
        ),
    ]
    return {d.key: d for d in dims}


#: The findings reporting threshold. Module-level so tests and the run report can
#: reference one value; deliberately generous (see the derivation above).
SOFT_FINDINGS_ENVELOPE = 5_000


def get_dimension(key: str) -> Optional[Dimension]:
    return build_envelope().get(key)


def envelope_summary() -> Dict[str, Any]:
    """A JSON-serialisable snapshot of the envelope and how honest it is.

    The audit surface. A reader should be able to tell, without opening any
    code, which of these numbers rests on a measurement and which does not.
    """
    dims = build_envelope()
    by_basis: Dict[str, List[str]] = {}
    for d in dims.values():
        by_basis.setdefault(d.basis, []).append(d.key)
    gaps = {k: d.declared_gap for k, d in dims.items() if d.declared_gap}
    return {
        "envelopeVersion": ENVELOPE_VERSION,
        "dimensions": {k: d.to_dict() for k, d in dims.items()},
        "basisBreakdown": {b: sorted(v) for b, v in sorted(by_basis.items())},
        "measuredCount": sum(1 for d in dims.values() if d.is_measured),
        "totalCount": len(dims),
        "declaredGaps": gaps,
        "honestyNote": (
            f"{sum(1 for d in dims.values() if d.is_measured)} of {len(dims)} dimensions "
            "rest on a reproducible measurement. The rest are operationally justified or "
            "provisional and are labelled as such rather than presented as measured."
        ),
        "degradationRule": (
            "No dimension drops work silently. Three defer-and-count or refuse with a "
            "named reason; findings-per-run reports only and never withholds a finding."
        ),
    }


__all__ = [
    "ASSUMED_BYTES_PER_DOCUMENT",
    "BASIS_MEASURED",
    "BASIS_OPERATIONALLY_JUSTIFIED",
    "BASIS_PROVISIONAL",
    "DEGRADATION_MODES",
    "DEGRADE_DEFER_AND_COUNT",
    "DEGRADE_REFUSE_WITH_REASON",
    "DEGRADE_REPORT_ONLY",
    "DIM_DOCUMENTS_PER_RUN",
    "DIM_EVENTS_PER_RUN",
    "DIM_FINDINGS_PER_RUN",
    "DIM_SYSTEMS_PER_DEPLOYMENT",
    "ENVELOPE_VERSION",
    "RECOGNISED_BASES",
    "SOFT_FINDINGS_ENVELOPE",
    "Dimension",
    "build_envelope",
    "envelope_summary",
    "get_dimension",
]
