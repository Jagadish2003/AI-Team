"""Sandbox validation for authored packs — 2.0-C3 T6 (AT-841).

The rule this module owns:

    Installing a pack runs its manifest through validation and its fixtures
    through the harness before activation; failures block activation with
    specific reasons.

Three words in that sentence do the work.

**"its fixtures"** — validation is not only a schema check. A manifest can be
perfectly well-formed and still declare detectors that produce nothing, or
produce findings missing a contract part. Running the author's own cases is what
turns "this document parses" into "this pack does what its author claims", and it
is the only evidence the platform has about a pack it did not write.

**"before activation"** — installation already validated (AT-839 gate 2), but
installing is not activating, and the two can be months apart. In between, the
platform moves: a concept can be withdrawn, a primitive's contract can change, a
lint floor can be raised. So activation re-runs the whole check against the
STORED manifest and the STORED fixtures rather than trusting the install-time
verdict. That is why this task persists the fixtures at all — a pack you cannot
re-validate is a pack you have to take on trust at exactly the moment it starts
executing.

**"sandbox"** — the harness runs partner-supplied *data* through platform code,
inside the API process, on a request. Declarative is not the same as costless: a
fixture with fifty thousand records and a depth-3 traversal is a CPU and memory
spike whatever the manifest says. So the run is bounded — case count, records per
case, records in total, fixture bytes, and a time budget — and exceeding a bound
is a NAMED refusal rather than a slow request nobody can explain. The bounds are
the same discipline MSP-B7 applies to event volume: the limit is enforced where
the work happens, and breaching it is loud.

What this is not
----------------
It is not an OS-level sandbox, and it does not need to be: no partner code
executes here (2.0-C3's governing constraint), so there is nothing to isolate
*from*. What there is, is untrusted input sizing a trusted computation, and that
is what these bounds address. The time budget is checked between cases; work
inside one case is bounded by the per-case record cap rather than by a hard kill,
because killing a thread mid-computation is not something Python offers and
pretending otherwise would be worse than saying so.

Tuning
------
``PACK_SANDBOX_MAX_CASES`` / ``_MAX_RECORDS_PER_CASE`` / ``_MAX_TOTAL_RECORDS`` /
``_MAX_FIXTURE_BYTES`` / ``_TIMEOUT_SECONDS``. An unparseable or non-positive
value falls back to the default and logs — a mistyped limit must never silently
remove a bound.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from discovery.packs.platform_capabilities import PLATFORM_VERSION
from discovery.packs.sdk.harness import (
    FIXTURES_DIRNAME,
    PACK_MANIFEST_FILENAME,
    HarnessError,
    HarnessTimeout,
    load_cases,
)
from discovery.packs.sdk.manifest import (
    ManifestValidationError,
    PackManifest,
    load_manifest_document,
)
from discovery.packs.sdk.toolkit import check_manifest_document

logger = logging.getLogger(__name__)

# ── Stages ────────────────────────────────────────────────────────────────────
#
# Which stage a pack got to. Reported so a refusal says *where* it failed: "your
# fixtures do not pass" and "your fixtures are too large to run" are different
# problems with different fixes.

STAGE_ADMISSION = "admission"
STAGE_VALIDATION = "validation"
STAGE_FIXTURES = "fixtures"
STAGE_LINT = "lint"
STAGE_PASSED = "passed"

# ── Limits ────────────────────────────────────────────────────────────────────

DEFAULT_MAX_CASES = 50
DEFAULT_MAX_RECORDS_PER_CASE = 2_000
DEFAULT_MAX_TOTAL_RECORDS = 20_000
DEFAULT_MAX_FIXTURE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


def _positive(env_var: str, default: float, *, cast=int):
    raw = os.getenv(env_var)
    if raw is None or not str(raw).strip():
        return cast(default)
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", env_var, raw, default)
        return cast(default)
    if value <= 0:
        # Deliberately not "0 disables": a bound an operator can remove with a
        # typo is not a bound. Removing one is a code change.
        logger.warning("%s=%r is not positive; using %s", env_var, raw, default)
        return cast(default)
    return value


@dataclass(frozen=True)
class SandboxLimits:
    """What a pack's fixtures may cost to judge."""

    max_cases: int = DEFAULT_MAX_CASES
    max_records_per_case: int = DEFAULT_MAX_RECORDS_PER_CASE
    max_total_records: int = DEFAULT_MAX_TOTAL_RECORDS
    max_fixture_bytes: int = DEFAULT_MAX_FIXTURE_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "SandboxLimits":
        return cls(
            max_cases=_positive("PACK_SANDBOX_MAX_CASES", DEFAULT_MAX_CASES),
            max_records_per_case=_positive(
                "PACK_SANDBOX_MAX_RECORDS_PER_CASE", DEFAULT_MAX_RECORDS_PER_CASE
            ),
            max_total_records=_positive(
                "PACK_SANDBOX_MAX_TOTAL_RECORDS", DEFAULT_MAX_TOTAL_RECORDS
            ),
            max_fixture_bytes=_positive(
                "PACK_SANDBOX_MAX_FIXTURE_BYTES", DEFAULT_MAX_FIXTURE_BYTES
            ),
            timeout_seconds=_positive(
                "PACK_SANDBOX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, cast=float
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "maxCases": self.max_cases,
            "maxRecordsPerCase": self.max_records_per_case,
            "maxTotalRecords": self.max_total_records,
            "maxFixtureBytes": self.max_fixture_bytes,
            "timeoutSeconds": self.timeout_seconds,
        }


# ── Report ────────────────────────────────────────────────────────────────────


@dataclass
class SandboxReport:
    """The verdict, and everything an operator needs to act on it.

    Serialisable and persisted with the installed pack, so "why can this not be
    activated" is answerable without re-uploading the bundle.
    """

    ok: bool = False
    stage: str = STAGE_ADMISSION
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    case_count: int = 0
    record_count: int = 0
    duration_ms: int = 0
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    platform_version: str = PLATFORM_VERSION
    checked_at: str = ""
    #: The validated manifest, when validation got that far, and the cases that
    #: were run. Neither is serialised — the installed record stores both, and
    #: duplicating them inside the report would let the copies disagree. They are
    #: carried so a caller that has just validated a directory does not have to
    #: read and parse it a second time.
    manifest: Optional[PackManifest] = None
    cases: Sequence[Mapping[str, Any]] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "caseCount": self.case_count,
            "recordCount": self.record_count,
            "durationMs": self.duration_ms,
            "limits": self.limits.to_dict(),
            "platformVersion": self.platform_version,
            "checkedAt": self.checked_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_count(case: Mapping[str, Any]) -> int:
    signal = case.get("signal")
    if not isinstance(signal, Mapping):
        return 0
    records = signal.get("records")
    return len(records) if isinstance(records, (list, tuple)) else 0


def _fixture_bytes(cases: Sequence[Mapping[str, Any]]) -> int:
    try:
        return len(json.dumps(list(cases), default=str).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        # Unserialisable content is a case-file problem the harness will report;
        # for sizing purposes treat it as over the line rather than as free.
        return DEFAULT_MAX_FIXTURE_BYTES + 1


def check_admission(
    cases: Sequence[Mapping[str, Any]], limits: SandboxLimits
) -> List[str]:
    """Every limit this fixture suite breaks, before a single case is run.

    Every breach is reported, not the first: an author who shrinks their suite
    once per rejection is an author who submits five times.
    """
    problems: List[str] = []
    if len(cases) > limits.max_cases:
        problems.append(
            f"{len(cases)} fixture cases exceeds the sandbox limit of "
            f"{limits.max_cases}"
        )
    total = 0
    for index, case in enumerate(cases):
        count = _record_count(case)
        total += count
        if count > limits.max_records_per_case:
            name = str(case.get("name") or f"case[{index}]")
            problems.append(
                f"case '{name}' seeds {count} records, above the per-case sandbox "
                f"limit of {limits.max_records_per_case}"
            )
    if total > limits.max_total_records:
        problems.append(
            f"the fixture suite seeds {total} records in total, above the sandbox "
            f"limit of {limits.max_total_records}"
        )
    size = _fixture_bytes(cases)
    if size > limits.max_fixture_bytes:
        problems.append(
            f"the fixture suite is {size} bytes, above the sandbox limit of "
            f"{limits.max_fixture_bytes}"
        )
    return problems


def _stage_of(check) -> str:
    if check.validation_errors or check.manifest is None:
        return STAGE_VALIDATION
    if check.setup_errors:
        return STAGE_ADMISSION
    if check.harness is not None and not check.harness.passed:
        return STAGE_FIXTURES
    if check.lint is not None and not check.lint.ok:
        return STAGE_LINT
    return STAGE_PASSED


def run_sandbox_validation(
    document: Any,
    cases: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    limits: Optional[SandboxLimits] = None,
) -> SandboxReport:
    """Validate a manifest and run its fixtures, bounded. Never raises.

    The report IS the result, in the posture of ``PackCheckReport`` and
    ``BundleVerification``: a caller decides what to do about a refusal, and a
    refusal is data rather than control flow.
    """
    resolved = limits or SandboxLimits.from_env()
    suite = list(cases or [])
    report = SandboxReport(
        limits=resolved,
        case_count=len(suite),
        record_count=sum(_record_count(case) for case in suite),
        checked_at=_now(),
        cases=tuple(suite),
    )

    admission = check_admission(suite, resolved)
    if admission:
        report.stage = STAGE_ADMISSION
        report.reasons = admission
        return report

    if not suite:
        # An installed pack from before fixtures were persisted, or a
        # document-only check. Validate and lint what there is, and SAY that the
        # fixtures were not run — a report that looked identical to a full pass
        # would overstate what was verified.
        report.notes.append(
            "no fixtures were available to run; the manifest and lint checks ran alone"
        )

    started = time.monotonic()
    deadline = started + resolved.timeout_seconds
    try:
        check = check_manifest_document(document, list(suite), deadline=deadline)
    except HarnessTimeout as exc:
        report.stage = STAGE_ADMISSION
        report.duration_ms = int((time.monotonic() - started) * 1000)
        report.reasons = [
            f"{exc} (sandbox time budget: {resolved.timeout_seconds}s)"
        ]
        return report

    report.duration_ms = int((time.monotonic() - started) * 1000)
    report.manifest = check.manifest
    report.stage = _stage_of(check)
    report.ok = check.ok
    report.reasons = [] if check.ok else check.reasons()
    return report


def sandbox_pack_directory(
    directory: Any, *, limits: Optional[SandboxLimits] = None
) -> SandboxReport:
    """Validate an extracted pack project — the install-time entry point.

    Reads the manifest and the fixtures from disk, then delegates. A manifest that
    cannot be read and a fixtures directory that cannot be read are both reported
    as reasons rather than raised, so the caller has one shape to handle.
    """
    resolved = limits or SandboxLimits.from_env()
    pack_dir = Path(directory)

    try:
        document = load_manifest_document(pack_dir / PACK_MANIFEST_FILENAME)
    except ManifestValidationError as exc:
        return SandboxReport(
            stage=STAGE_VALIDATION,
            reasons=[f"manifest {error.path}: {error.message}" for error in exc.errors],
            limits=resolved,
            checked_at=_now(),
        )

    try:
        cases = load_cases(pack_dir / FIXTURES_DIRNAME)
    except HarnessError as exc:
        # A pack with no runnable fixtures has not been shown to do anything, and
        # the platform is not going to activate it on the strength of a document.
        return SandboxReport(
            stage=STAGE_FIXTURES,
            reasons=[str(exc)],
            limits=resolved,
            checked_at=_now(),
        )

    return run_sandbox_validation(document, cases, limits=resolved)


__all__ = [
    "DEFAULT_MAX_CASES",
    "DEFAULT_MAX_FIXTURE_BYTES",
    "DEFAULT_MAX_RECORDS_PER_CASE",
    "DEFAULT_MAX_TOTAL_RECORDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "STAGE_ADMISSION",
    "STAGE_FIXTURES",
    "STAGE_LINT",
    "STAGE_PASSED",
    "STAGE_VALIDATION",
    "SandboxLimits",
    "SandboxReport",
    "check_admission",
    "run_sandbox_validation",
    "sandbox_pack_directory",
]
