"""The authoring check — 2.0-C3 T3 (AT-838).

One function that runs the whole toolkit over a pack project, in the order the
stages actually depend on each other:

    1. **validate** the manifest (AT-836). Nothing downstream can run against a
       document that is not well-formed and closed, so a validation failure stops
       here rather than producing a cascade of derived noise.
    2. **test** the fixtures through the harness — which also produces the
       findings the next stage needs.
    3. **lint** the manifest AND those findings, so the runtime legs of the
       honesty rules run against real output rather than only the document.

This ordering is the reason the check lives in one place instead of being three
CLI calls glued together: lint's runtime legs are strictly better than its static
legs, and they only exist if the harness ran first.

It is also the function installation-time sandbox validation is meant to call, so
"what the author ran locally" and "what the platform runs before activation" are
the same code path rather than two implementations that drift.

Dependency-free of ``app``; reads only the pack directory it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .harness import (
    FIXTURES_DIRNAME,
    PACK_MANIFEST_FILENAME,
    HarnessError,
    HarnessResult,
    load_cases,
    run_cases,
)
from .lint import LintReport, lint_pack
from .manifest import (
    ManifestValidationError,
    PackManifest,
    load_manifest_document,
    validate_manifest,
)


@dataclass
class PackCheckReport:
    """Everything the toolkit found, stage by stage.

    Never raises: a caller (a CLI, or installation) renders the report. ``ok`` is
    the single question "may this pack be activated?", and it is the AND of all
    three stages — a pack whose fixtures fail has not been shown to work, and one
    that fails lint has been shown to break a non-negotiable.
    """

    manifest: Optional[PackManifest] = None
    validation_errors: List[Dict[str, str]] = field(default_factory=list)
    lint: Optional[LintReport] = None
    harness: Optional[HarnessResult] = None
    setup_errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.manifest is not None
            and not self.validation_errors
            and not self.setup_errors
            and (self.harness is None or self.harness.passed)
            and (self.lint is None or self.lint.ok)
        )

    def reasons(self) -> List[str]:
        """Every specific reason this pack cannot be activated, in stage order.

        Specific by requirement: installation must report *what* failed, and
        "validation failed" tells an author nothing they can act on.
        """
        reasons: List[str] = []
        reasons.extend(
            f"manifest {error['path']}: {error['message']}"
            for error in self.validation_errors
        )
        reasons.extend(self.setup_errors)
        if self.harness is not None:
            reasons.extend(f"fixture {failure}" for failure in self.harness.failures)
        if self.lint is not None:
            reasons.extend(
                f"lint [{finding.rule}] {finding.path}: {finding.message}"
                for finding in self.lint.errors
            )
        return reasons

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "packId": self.manifest.pack_id if self.manifest else "",
            "packVersion": self.manifest.pack_version if self.manifest else "",
            "validation": {
                "valid": bool(self.manifest) and not self.validation_errors,
                "errors": list(self.validation_errors),
            },
            "harness": self.harness.to_dict() if self.harness else None,
            "lint": self.lint.to_dict() if self.lint else None,
            "setupErrors": list(self.setup_errors),
            "reasons": self.reasons(),
        }


def check_manifest_document(
    document: Any,
    cases: Optional[List[Dict[str, Any]]] = None,
    *,
    deadline: Optional[float] = None,
) -> PackCheckReport:
    """Run the three stages over an in-memory manifest document.

    ``deadline`` (an absolute :func:`time.monotonic` instant) is passed straight
    to the harness and raises :class:`HarnessTimeout` if the suite runs past it.
    Only a bounded caller supplies one — the authoring loop does not, because an
    author's own slow fixture is their business.
    """
    report = PackCheckReport()
    validation = validate_manifest(document)
    if not validation.ok or validation.manifest is None:
        report.validation_errors = [error.to_dict() for error in validation.errors]
        return report

    manifest = validation.manifest
    report.manifest = manifest
    if cases:
        report.harness = run_cases(manifest, cases, deadline=deadline)
        findings = report.harness.findings
    else:
        findings = []
    report.lint = lint_pack(manifest, findings)
    return report


def check_pack_directory(directory: Any, *, require_fixtures: bool = True) -> PackCheckReport:
    """Run the toolkit over a pack project directory.

    ``require_fixtures`` defaults True: a pack with no test cases has not been
    shown to do anything, and shipping one would make the harness optional in
    practice. A caller wanting a document-only check passes False.
    """
    pack_dir = Path(directory)
    manifest_path = (
        pack_dir if pack_dir.is_file() else pack_dir / PACK_MANIFEST_FILENAME
    )
    report = PackCheckReport()

    try:
        document = load_manifest_document(manifest_path)
    except ManifestValidationError as exc:
        report.validation_errors = [error.to_dict() for error in exc.errors]
        return report

    cases: List[Dict[str, Any]] = []
    if not pack_dir.is_file():
        try:
            cases = load_cases(pack_dir / FIXTURES_DIRNAME)
        except HarnessError as exc:
            if require_fixtures:
                report.setup_errors.append(str(exc))
            cases = []
    elif require_fixtures:
        report.setup_errors.append(
            "a manifest file alone carries no fixtures; point the check at the pack "
            "directory"
        )

    checked = check_manifest_document(document, cases)
    checked.setup_errors.extend(report.setup_errors)
    checked.validation_errors.extend(report.validation_errors)
    return checked


__all__ = ["PackCheckReport", "check_manifest_document", "check_pack_directory"]
