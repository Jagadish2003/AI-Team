"""Fixture-based test harness — 2.0-C3 T3 (AT-838).

The author's inner loop: *supply seeded signal, assert findings.* A pack author
writes a case file describing signal and what they expect from it; the harness
runs the manifest's detectors over that signal and reports, per expectation,
exactly what did or did not happen.

Why the harness asserts things the author did not ask for
----------------------------------------------------------
Every case additionally checks the four-part contract on every finding produced,
whether or not the case mentions it. Evidence completeness is not a property an
author opts into — a pack whose fixtures pass while emitting a contract-incomplete
finding would sail through authoring and fail at the pack boundary in a customer's
run, which is the worst place to discover it.

Why an unknown detector id in a case is a FAILURE, not a skip
--------------------------------------------------------------
A typo'd detector id would otherwise make a case silently assert nothing and pass
forever — the classic green-but-empty test. Naming a detector the manifest does
not declare fails the case and says so.

Deterministic by construction: ``asOf`` may be pinned per case, and otherwise
comes from the latest record in the case's own signal. Nothing reads the clock.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .execution import PackExecutionResult, run_manifest
from .manifest import PackManifest
from .primitive_library import PrimitiveFinding
from .signals import SignalError, parse_timestamp, signal_set_from_dicts

#: A pack project is a directory: the manifest, plus a directory of case files.
PACK_MANIFEST_FILENAME = "pack.json"
FIXTURES_DIRNAME = "fixtures"

#: Case-file vocabulary. Closed, for the same reason the manifest schema is: an
#: ignored key is an assertion the author believes they wrote and never ran.
CASE_FIELDS = ("name", "description", "asOf", "signal", "expect")
EXPECT_FIELDS = ("detectors", "noOtherDetectorsFire", "findingCount")
DETECTOR_EXPECT_FIELDS = (
    "fires",
    "findingCount",
    "subjects",
    "minMetric",
    "maxMetric",
    "confidence",
    "corroboration",
    "statementContains",
)


class HarnessError(ValueError):
    """A case file cannot be read or is not a well-formed case."""


class HarnessTimeout(HarnessError):
    """A suite ran past the deadline it was given.

    Raised only when a caller supplies one (2.0-C3 T6 / AT-841's sandbox does).
    It is deliberately NOT a case failure: a suite that ran out of time has not
    been shown to fail, it has been shown to be too expensive to judge, and those
    are different verdicts.
    """


@dataclass(frozen=True)
class CaseResult:
    """What one case did. ``failures`` is empty exactly when it passed."""

    name: str
    passed: bool
    failures: Sequence[str] = ()
    findings: Sequence[PrimitiveFinding] = ()
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failures": list(self.failures),
            "findingCount": len(self.findings),
            "source": self.source,
        }


@dataclass
class HarnessResult:
    """The verdict over a whole case suite."""

    cases: List[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def findings(self) -> List[PrimitiveFinding]:
        return [finding for case in self.cases for finding in case.findings]

    @property
    def failures(self) -> List[str]:
        return [
            f"{case.name}: {failure}"
            for case in self.cases
            for failure in case.failures
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "caseCount": len(self.cases),
            "failureCount": len(self.failures),
            "cases": [case.to_dict() for case in self.cases],
        }


# ── Case loading ──────────────────────────────────────────────────────────────


def _unknown_fields(block: Mapping[str, Any], allowed: Sequence[str], path: str) -> List[str]:
    return [
        f"{path}.{key} is not a recognised field (allowed: {', '.join(allowed)})"
        for key in block
        if key not in allowed
    ]


def validate_case(document: Any) -> List[str]:
    """Structural problems with a case document — empty list when well-formed."""
    if not isinstance(document, dict):
        return ["a test case is a JSON object"]
    problems = _unknown_fields(document, CASE_FIELDS, "case")
    if not str(document.get("name") or "").strip():
        problems.append("case.name is required")
    signal = document.get("signal")
    if not isinstance(signal, dict) or not signal.get("records"):
        problems.append("case.signal.records is required — a case seeds its own signal")
    expect = document.get("expect")
    if not isinstance(expect, dict) or not expect:
        problems.append("case.expect is required — a case with no expectation asserts nothing")
        return problems
    problems.extend(_unknown_fields(expect, EXPECT_FIELDS, "case.expect"))
    detectors = expect.get("detectors") or {}
    if not isinstance(detectors, dict):
        problems.append("case.expect.detectors must be an object keyed by detector id")
        return problems
    for detector_id, expectation in detectors.items():
        if not isinstance(expectation, dict):
            problems.append(f"case.expect.detectors.{detector_id} must be an object")
            continue
        problems.extend(
            _unknown_fields(
                expectation, DETECTOR_EXPECT_FIELDS, f"case.expect.detectors.{detector_id}"
            )
        )
    return problems


def load_case(path: Any) -> Dict[str, Any]:
    """Read one case file, raising :class:`HarnessError` with a specific reason."""
    file_path = Path(path)
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HarnessError(f"{file_path.name}: cannot be read ({exc.strerror or exc})") from exc
    except json.JSONDecodeError as exc:
        raise HarnessError(
            f"{file_path.name}: is not valid JSON ({exc.msg}, line {exc.lineno})"
        ) from exc
    problems = validate_case(document)
    if problems:
        raise HarnessError(f"{file_path.name}: {'; '.join(problems)}")
    return document


def load_cases(directory: Any) -> List[Dict[str, Any]]:
    """Load every ``*.json`` case in a fixtures directory, in filename order.

    Filename order, not filesystem order, so a suite runs the same way on every
    machine. Each case carries its source filename for reporting.
    """
    fixtures = Path(directory)
    if not fixtures.is_dir():
        raise HarnessError(f"no fixtures directory at {fixtures}")
    cases: List[Dict[str, Any]] = []
    for path in sorted(fixtures.glob("*.json")):
        document = load_case(path)
        document.setdefault("_source", path.name)
        cases.append(document)
    if not cases:
        raise HarnessError(
            f"{fixtures} contains no test cases — a pack without fixtures has not "
            f"been shown to do anything"
        )
    return cases


# ── Running ───────────────────────────────────────────────────────────────────


def _check_detector(
    detector_id: str,
    expectation: Mapping[str, Any],
    result: PackExecutionResult,
    declared: Sequence[str],
) -> List[str]:
    if detector_id not in declared:
        return [
            f"expects detector {detector_id!r}, which the manifest does not declare "
            f"(declared: {', '.join(declared)})"
        ]

    findings = result.findings_for(detector_id)
    failures: List[str] = []

    expects_fire = expectation.get("fires", True)
    if expects_fire and not findings:
        failures.append(f"{detector_id}: expected to fire, produced no findings")
        return failures
    if not expects_fire and findings:
        failures.append(
            f"{detector_id}: expected not to fire, produced {len(findings)} finding(s) "
            f"on {', '.join(sorted(f.subject for f in findings))}"
        )
        return failures
    if not expects_fire:
        return failures

    expected_count = expectation.get("findingCount")
    if expected_count is not None and len(findings) != int(expected_count):
        failures.append(
            f"{detector_id}: expected {expected_count} finding(s), got {len(findings)}"
        )

    expected_subjects = expectation.get("subjects")
    if expected_subjects is not None:
        actual = sorted(finding.subject for finding in findings)
        if actual != sorted(str(subject) for subject in expected_subjects):
            failures.append(
                f"{detector_id}: expected subjects {sorted(expected_subjects)}, got {actual}"
            )

    minimum = expectation.get("minMetric")
    if minimum is not None and not any(f.metric_value >= float(minimum) for f in findings):
        failures.append(
            f"{detector_id}: expected a finding with metric >= {minimum}, highest was "
            f"{max(f.metric_value for f in findings)}"
        )
    maximum = expectation.get("maxMetric")
    if maximum is not None and not all(f.metric_value <= float(maximum) for f in findings):
        failures.append(
            f"{detector_id}: expected every metric <= {maximum}, highest was "
            f"{max(f.metric_value for f in findings)}"
        )

    expected_confidence = expectation.get("confidence")
    if expected_confidence is not None:
        levels = sorted({finding.confidence_level for finding in findings})
        if levels != [str(expected_confidence).upper()]:
            failures.append(
                f"{detector_id}: expected confidence {expected_confidence}, got "
                f"{', '.join(levels)}"
            )

    expected_corroboration = expectation.get("corroboration")
    if expected_corroboration is not None:
        statuses = sorted(
            {
                str(finding.contract.get("corroboration", {}).get("status", ""))
                for finding in findings
            }
        )
        if statuses != [str(expected_corroboration)]:
            failures.append(
                f"{detector_id}: expected corroboration {expected_corroboration!r}, got "
                f"{', '.join(statuses)}"
            )

    fragment = expectation.get("statementContains")
    if fragment and not any(str(fragment).lower() in f.statement.lower() for f in findings):
        failures.append(
            f"{detector_id}: no finding's statement contains {fragment!r}"
        )
    return failures


def _check_contracts(result: PackExecutionResult) -> List[str]:
    """The floor every case gets whether it asked for it or not."""
    from .contract import FOUR_PART_CONTRACT_FIELDS

    failures: List[str] = []
    for finding in result.findings:
        missing = [
            part for part in FOUR_PART_CONTRACT_FIELDS if not finding.contract.get(part)
        ]
        if missing:
            failures.append(
                f"{finding.detector_id}: finding on {finding.subject!r} is missing "
                f"contract part(s) {missing}"
            )
    return failures


def run_case(manifest: PackManifest, case: Mapping[str, Any]) -> CaseResult:
    """Run one case and report every expectation that did not hold.

    Reports ALL failures in the case, not just the first — an author fixing one
    assertion per run is an author who stops running the harness.
    """
    name = str(case.get("name") or "unnamed case")
    source = str(case.get("_source") or "")
    problems = validate_case({k: v for k, v in case.items() if k != "_source"})
    if problems:
        return CaseResult(name=name, passed=False, failures=problems, source=source)

    try:
        signals = signal_set_from_dicts(case.get("signal") or {})
    except SignalError as exc:
        # Admission refusals are the author's most common early failure (an
        # individual field, an unknown concept), so they read as a case failure
        # with the reason rather than a traceback.
        return CaseResult(
            name=name, passed=False, failures=[f"signal rejected: {exc}"], source=source
        )

    as_of = parse_timestamp(case.get("asOf")) if case.get("asOf") else None
    if case.get("asOf") and as_of is None:
        return CaseResult(
            name=name,
            passed=False,
            failures=[f"case.asOf {case.get('asOf')!r} is not a parseable timestamp"],
            source=source,
        )

    result = run_manifest(manifest, signals, as_of=as_of)
    declared = [declaration.detector_id for declaration in manifest.detectors]

    expect = case.get("expect") or {}
    failures: List[str] = []
    for detector_id, expectation in (expect.get("detectors") or {}).items():
        failures.extend(_check_detector(detector_id, expectation, result, declared))

    total = expect.get("findingCount")
    if total is not None and len(result.findings) != int(total):
        failures.append(
            f"expected {total} finding(s) in total, got {len(result.findings)}"
        )

    if expect.get("noOtherDetectorsFire"):
        named = set((expect.get("detectors") or {}).keys())
        unexpected = sorted(
            outcome.detector_id
            for outcome in result.outcomes
            if outcome.fired and outcome.detector_id not in named
        )
        if unexpected:
            failures.append(
                f"detectors fired that the case did not expect: {', '.join(unexpected)}"
            )

    failures.extend(_check_contracts(result))
    return CaseResult(
        name=name,
        passed=not failures,
        failures=failures,
        findings=tuple(result.findings),
        source=source,
    )


def run_cases(
    manifest: PackManifest,
    cases: Sequence[Mapping[str, Any]],
    *,
    deadline: Optional[float] = None,
) -> HarnessResult:
    """Run a whole suite. Every case runs even after one fails.

    ``deadline`` is an absolute :func:`time.monotonic` instant, checked BETWEEN
    cases; passing it raises :class:`HarnessTimeout`. The check lives here because
    this loop is the only place that can make it — a caller wanting a bounded run
    would otherwise have to re-implement the loop, which is exactly the drift the
    toolkit exists to prevent. Work *inside* one case is bounded by the caller's
    record limits rather than by this deadline.
    """
    results: List[CaseResult] = []
    for case in cases:
        if deadline is not None and time.monotonic() >= deadline:
            raise HarnessTimeout(
                f"fixture suite exceeded its time budget after {len(results)} "
                f"of {len(cases)} case(s)"
            )
        results.append(run_case(manifest, case))
    return HarnessResult(cases=results)


def run_pack_directory(directory: Any, manifest: Optional[PackManifest] = None) -> HarnessResult:
    """Run the fixtures of a pack project directory (``pack.json`` + ``fixtures/``)."""
    from .manifest import load_manifest

    pack_dir = Path(directory)
    resolved = manifest or load_manifest(pack_dir / PACK_MANIFEST_FILENAME)
    return run_cases(resolved, load_cases(pack_dir / FIXTURES_DIRNAME))


__all__ = [
    "CASE_FIELDS",
    "DETECTOR_EXPECT_FIELDS",
    "EXPECT_FIELDS",
    "FIXTURES_DIRNAME",
    "PACK_MANIFEST_FILENAME",
    "CaseResult",
    "HarnessError",
    "HarnessResult",
    "HarnessTimeout",
    "load_case",
    "load_cases",
    "run_case",
    "run_cases",
    "run_pack_directory",
    "validate_case",
]
