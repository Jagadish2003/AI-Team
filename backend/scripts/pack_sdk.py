"""AgentIQ Skills SDK — pack authoring CLI (2.0-C3 T3 / AT-838).

The local authoring loop for a partner pack. Everything here is offline and
read-only against the platform: it validates, lints, and runs a pack's own
fixtures. It never installs, never signs, and never runs partner code — a pack is
configuration, and this tool only ever reads it.

    # Start a new pack project
    python scripts/pack_sdk.py scaffold ./my_pack --pack-id acme_service_desk \
        --name "Acme Service Desk" --author "Acme Ltd" --contact packs@acme.test

    # The loop
    python scripts/pack_sdk.py validate ./my_pack   # well-formed and closed?
    python scripts/pack_sdk.py lint ./my_pack       # holds the non-negotiables?
    python scripts/pack_sdk.py test ./my_pack       # fixtures produce what you expect?
    python scripts/pack_sdk.py check ./my_pack      # all three, as installation runs them

    # Reference
    python scripts/pack_sdk.py primitives           # the primitive library + parameters
    python scripts/pack_sdk.py schema               # the manifest schema reference
    python scripts/pack_sdk.py rules                # the lint rules and floors

Every command exits non-zero on failure, so it drops straight into CI. Add
``--json`` to any of them for a machine-readable report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.packs.sdk.harness import (  # noqa: E402
    FIXTURES_DIRNAME,
    PACK_MANIFEST_FILENAME,
    HarnessError,
    load_cases,
    run_cases,
)
from discovery.packs.sdk.lint import lint_pack, lint_rule_reference  # noqa: E402
from discovery.packs.sdk.manifest import (  # noqa: E402
    ManifestValidationError,
    load_manifest_document,
    manifest_schema_reference,
    validate_manifest,
)
from discovery.packs.sdk.primitives import primitive_catalog  # noqa: E402
from discovery.packs.sdk.scaffold import ScaffoldError, scaffold_pack  # noqa: E402
from discovery.packs.sdk.toolkit import check_pack_directory  # noqa: E402


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))


def _manifest_path(target: Path) -> Path:
    return target if target.is_file() else target / PACK_MANIFEST_FILENAME


def _load_manifest(target: Path):
    """Read + validate, printing every specific error. Returns None on failure."""
    try:
        document = load_manifest_document(_manifest_path(target))
    except ManifestValidationError as exc:
        for error in exc.errors:
            print(f"  {error.path}: {error.message}")
        return None
    result = validate_manifest(document)
    if not result.ok:
        for error in result.errors:
            print(f"  [{error.code}] {error.path}: {error.message}")
        return None
    return result.manifest


def cmd_scaffold(args: argparse.Namespace) -> int:
    try:
        result = scaffold_pack(
            args.directory,
            pack_id=args.pack_id,
            pack_name=args.name or "",
            author_name=args.author or "",
            author_contact=args.contact or "",
            concept=args.concept,
            force=args.force,
        )
    except ScaffoldError as exc:
        print(f"scaffold failed: {exc}")
        return 1
    print(f"Scaffolded pack {args.pack_id!r} in {result.directory}:")
    for path in result.files:
        print(f"  {path}")
    print(
        f"\nNext: python scripts/pack_sdk.py check {result.directory}"
    )
    _emit(result.to_dict(), args.json)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    try:
        document = load_manifest_document(_manifest_path(target))
    except ManifestValidationError as exc:
        print(f"Manifest is not valid ({len(exc.errors)} error(s)):")
        for error in exc.errors:
            print(f"  {error.path}: {error.message}")
        _emit(exc.to_dict(), args.json)
        return 1
    result = validate_manifest(document)
    if not result.ok:
        print(f"Manifest is not valid ({len(result.errors)} error(s)):")
        for error in result.errors:
            print(f"  [{error.code}] {error.path}: {error.message}")
        _emit(result.to_dict(), args.json)
        return 1
    manifest = result.manifest
    print(
        f"Manifest is valid: {manifest.pack_id} v{manifest.pack_version} "
        f"({len(manifest.detectors)} detector(s))"
    )
    _emit(result.to_dict(), args.json)
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    manifest = _load_manifest(Path(args.directory))
    if manifest is None:
        print("Cannot lint: the manifest is not valid (run validate).")
        return 1
    report = lint_pack(manifest)
    for finding in report.findings:
        print(f"  [{finding.severity}] [{finding.rule}] {finding.path}: {finding.message}")
    print(
        f"Lint: {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
        if report.findings
        else "Lint clean."
    )
    _emit(report.to_dict(), args.json)
    return 0 if report.ok else 1


def cmd_test(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    manifest = _load_manifest(target)
    if manifest is None:
        print("Cannot run fixtures: the manifest is not valid (run validate).")
        return 1
    try:
        cases = load_cases(target / FIXTURES_DIRNAME)
    except HarnessError as exc:
        print(f"Cannot run fixtures: {exc}")
        return 1
    result = run_cases(manifest, cases)
    for case in result.cases:
        status = "PASS" if case.passed else "FAIL"
        print(f"  [{status}] {case.name}")
        for failure in case.failures:
            print(f"        {failure}")
    print(
        f"Fixtures: {sum(1 for c in result.cases if c.passed)}/{len(result.cases)} "
        f"case(s) passed"
    )
    _emit(result.to_dict(), args.json)
    return 0 if result.passed else 1


def cmd_check(args: argparse.Namespace) -> int:
    report = check_pack_directory(args.directory)
    if report.ok:
        print(
            f"{report.manifest.pack_id} v{report.manifest.pack_version}: "
            f"validate, fixtures, and lint all pass."
        )
    else:
        print("Pack cannot be activated:")
        for reason in report.reasons():
            print(f"  {reason}")
    _emit(report.to_dict(), args.json)
    return 0 if report.ok else 1


def cmd_primitives(args: argparse.Namespace) -> int:
    catalog = primitive_catalog()
    if args.json:
        print(json.dumps(catalog, indent=2))
        return 0
    print(f"Primitive library {catalog['primitiveLibraryVersion']}:\n")
    for entry in catalog["primitives"]:
        arity = entry["conceptArity"]
        maximum = arity["maximum"]
        span = (
            f"{arity['minimum']}"
            if maximum == arity["minimum"]
            else f"{arity['minimum']}+"
            if maximum is None
            else f"{arity['minimum']}-{maximum}"
        )
        print(f"{entry['primitiveId']}  ({entry['label']}) — {span} concept(s)")
        print(f"  {entry['description']}")
        for parameter in entry["parameters"]:
            flag = "required" if parameter["required"] else "optional"
            bounds = ""
            if "minimum" in parameter or "maximum" in parameter:
                bounds = f" [{parameter.get('minimum', '-')}..{parameter.get('maximum', '-')}]"
            if parameter.get("choices"):
                bounds = f" ({'|'.join(parameter['choices'])})"
            print(f"    - {parameter['name']} ({parameter['kind']}, {flag}){bounds}")
        print()
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    print(json.dumps(manifest_schema_reference(), indent=2))
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    reference = lint_rule_reference()
    if args.json:
        print(json.dumps(reference, indent=2))
        return 0
    print("Lint rules — the platform's non-negotiables:\n")
    for entry in reference["rules"]:
        print(f"{entry['rule']}\n  {entry['requirement']}\n")
    print("Aggregation floors:")
    for primitive, floor in reference["aggregationFloors"].items():
        print(f"  {primitive}: {floor['parameter']} >= {floor['minimum']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pack_sdk",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scaffold = subparsers.add_parser("scaffold", help="create a new pack project")
    scaffold.add_argument("directory", help="target directory")
    scaffold.add_argument("--pack-id", required=True)
    scaffold.add_argument("--name", help="human-readable pack name")
    scaffold.add_argument("--author", help="authoring organisation")
    scaffold.add_argument("--contact", help="author contact")
    scaffold.add_argument("--concept", default="incident_workflow")
    scaffold.add_argument(
        "--force", action="store_true", help="overwrite existing files"
    )
    scaffold.set_defaults(handler=cmd_scaffold)

    for name, handler, help_text in (
        ("validate", cmd_validate, "check the manifest against the schema"),
        ("lint", cmd_lint, "check the platform's non-negotiables"),
        ("test", cmd_test, "run the pack's fixtures through the harness"),
        ("check", cmd_check, "validate + test + lint, as installation runs them"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("directory", nargs="?", default=".", help="pack directory")
        sub.set_defaults(handler=handler)

    subparsers.add_parser(
        "primitives", help="list the detector primitive library"
    ).set_defaults(handler=cmd_primitives)
    subparsers.add_parser(
        "schema", help="print the manifest schema reference"
    ).set_defaults(handler=cmd_schema)
    subparsers.add_parser("rules", help="print the lint rules").set_defaults(
        handler=cmd_rules
    )

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
