"""Manifest detector execution — 2.0-C3 T2 (AT-837).

Runs the detectors an authored manifest declares, over normalised signal, through
the primitive library — the step that turns a validated document into findings.
It is what the fixture-based authoring harness drives (2.0-C3 §3) and what
installation-time sandbox validation exercises before activation (§6).

The pack boundary is enforced HERE
----------------------------------
Every finding is checked for the complete four-part contract and for individual
references before it leaves this module, and a violation RAISES — the same
posture ``cloud_ops_finding.enforce_pack_findings`` takes for a first-party pack.
A partner pack is held to the identical bar; a certification level that could be
earned by a pack emitting weaker findings would not be worth printing on a board
paper.

Why the ``DetectorResult`` adapter is separate and lazy
-------------------------------------------------------
``discovery.models`` imports ``app.temporal``, and the SDK's whole posture is that
authoring tooling runs with no ``app`` dependency. So the execution core returns
plain :class:`~.primitive_library.PrimitiveFinding` objects, and the adapter that
converts them into pipeline ``DetectorResult`` objects imports the model inside
the function. Offline validation therefore never touches ``app``; only the
in-pipeline path does. A structural test pins that importing the SDK does not
import ``app``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .contract import enforce_pack_contract
from .manifest import DetectorDeclaration, PackManifest
from .primitive_library import (
    PrimitiveContext,
    PrimitiveFinding,
    run_primitive,
)
from .signals import SignalSet


@dataclass(frozen=True)
class DetectorOutcome:
    """What one declared detector did on this signal — including doing nothing.

    A detector that fired nothing is reported explicitly rather than omitted: an
    author debugging a fixture needs to tell "did not fire" from "was not run",
    and the sandbox validation surface needs the same distinction.
    """

    detector_id: str
    primitive: str
    fired: bool
    findings: Sequence[PrimitiveFinding] = ()
    skipped_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detectorId": self.detector_id,
            "primitive": self.primitive,
            "fired": self.fired,
            "findingCount": len(self.findings),
            "skippedReason": self.skipped_reason,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class PackExecutionResult:
    """Everything one manifest produced over one signal set."""

    pack_id: str
    pack_version: str
    outcomes: Sequence[DetectorOutcome] = ()

    @property
    def findings(self) -> List[PrimitiveFinding]:
        return [
            finding for outcome in self.outcomes for finding in outcome.findings
        ]

    def findings_for(self, detector_id: str) -> List[PrimitiveFinding]:
        return [
            finding
            for outcome in self.outcomes
            if outcome.detector_id == detector_id
            for finding in outcome.findings
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packId": self.pack_id,
            "packVersion": self.pack_version,
            "findingCount": len(self.findings),
            "detectors": [outcome.to_dict() for outcome in self.outcomes],
        }


def run_detector(
    declaration: DetectorDeclaration,
    signals: SignalSet,
    *,
    context: Optional[PrimitiveContext] = None,
) -> DetectorOutcome:
    """Run one declared detector and enforce the contract on what it produced."""
    findings = run_primitive(
        declaration.primitive,
        detector_id=declaration.detector_id,
        title=declaration.title,
        concepts=list(declaration.concepts),
        parameters=declaration.resolved_parameters(),
        signals=signals,
        context=context,
    )
    for index, finding in enumerate(findings):
        enforce_pack_contract(
            finding.contract, detector_id=declaration.detector_id, index=index
        )
    return DetectorOutcome(
        detector_id=declaration.detector_id,
        primitive=declaration.primitive,
        fired=bool(findings),
        findings=tuple(findings),
    )


def run_manifest(
    manifest: PackManifest,
    signals: SignalSet,
    *,
    as_of: Optional[datetime] = None,
    include_disabled: bool = False,
) -> PackExecutionResult:
    """Run every enabled detector a manifest declares over ``signals``.

    ``as_of`` defaults to the latest instant in the DATA, never the clock, so the
    same fixture always produces the same findings. The manifest's confidence caps
    ride along — they may only lower a level, which the schema already enforced.
    """
    context = PrimitiveContext(
        as_of=as_of or signals.default_as_of(),
        confidence_caps=dict(manifest.confidence_caps),
    )
    outcomes: List[DetectorOutcome] = []
    for declaration in manifest.detectors:
        if not declaration.enabled_by_default and not include_disabled:
            outcomes.append(
                DetectorOutcome(
                    detector_id=declaration.detector_id,
                    primitive=declaration.primitive,
                    fired=False,
                    skipped_reason="disabled by the manifest's enabledByDefault",
                )
            )
            continue
        outcomes.append(run_detector(declaration, signals, context=context))
    return PackExecutionResult(
        pack_id=manifest.pack_id,
        pack_version=manifest.pack_version,
        outcomes=tuple(outcomes),
    )


def to_detector_results(
    result: PackExecutionResult, findings: Optional[Sequence[PrimitiveFinding]] = None
) -> List[Any]:
    """Adapt findings into pipeline ``DetectorResult`` objects.

    The only path in the SDK that touches ``discovery.models`` (and through it
    ``app``), imported lazily for the reason in the module docstring. Each result
    carries the pack stamp and the full four-part contract on ``raw_evidence``,
    exactly as a first-party pack's detector does, so downstream scoring,
    provenance, and run-health surfaces need no special case for authored packs.
    """
    from ...models import DetectorResult  # local import: keeps the SDK app-free

    selected = list(findings if findings is not None else result.findings)
    adapted: List[Any] = []
    for finding in selected:
        contract = dict(finding.contract)
        adapted.append(
            DetectorResult(
                detector_id=finding.detector_id.upper(),
                signal_source=finding.signal_source,
                metric_value=float(finding.metric_value),
                threshold=float(finding.threshold),
                raw_evidence={
                    **{
                        key: value
                        for key, value in contract.get("evidence", {}).items()
                        if isinstance(value, (int, float, str))
                        and not isinstance(value, bool)
                    },
                    "packId": result.pack_id,
                    "packVersion": result.pack_version,
                    "primitive": finding.primitive,
                    "confidence": contract.get("confidence", {}).get("level", ""),
                    "corroborated": (
                        contract.get("corroboration", {}).get("status")
                        == "corroborated"
                    ),
                    "corroboration_sources": list(
                        contract.get("corroboration", {}).get("sources", [])
                    ),
                    "metric_value": float(finding.metric_value),
                    "finding_contract": contract,
                },
                provenance_type="observed",
            )
        )
    return adapted


__all__ = [
    "DetectorOutcome",
    "PackExecutionResult",
    "run_detector",
    "run_manifest",
    "to_detector_results",
]
