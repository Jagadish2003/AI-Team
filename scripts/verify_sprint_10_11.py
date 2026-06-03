"""Run the focused Sprint 10 and Sprint 11 verification suites.

Usage from Git Bash, opened at the AgentIQ repo root:

    python scripts/verify_sprint_10_11.py

The script uses relative repo paths and the active Python interpreter, so it is
portable across Windows laptops as long as the app dependencies are installed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

SPRINT_10_TESTS = [
    "backend/tests/contract/test_db_connectivity.py",
    "backend/tests/contract/test_query_guard.py",
    "backend/tests/contract/test_execute_query.py",
    "backend/tests/contract/test_db_connector_routes.py",
    "backend/connectors/db/tests",
    "backend/tests/unit/test_telemetry_t1_s10_c.py",
    "backend/tests/unit/test_audit_t1_s10_b.py",
]

SPRINT_11_TESTS = [
    "backend/tests/contract/test_sqlserver_ingestor.py",
    "backend/tests/contract/test_db_ticket_volume_surge.py",
    "backend/tests/contract/test_db_queue_depth_elevated.py",
    "backend/discovery/tests/test_db_sla_breach_rate.py",
    "backend/tests/contract/test_sqlserver_opsignal_scorer.py",
    "backend/tests/contract/test_sqlserver_opsignal_pack_registration.py",
    "backend/tests/contract/test_db_ingestor_completed_telemetry.py",
]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT / "backend"), str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def _run(label: str, command: list[str]) -> int:
    print()
    print("=" * 78)
    print(label)
    print("=" * 78)
    print("Working directory:", REPO_ROOT)
    print("Command:", " ".join(command))
    print("-" * 78)
    sys.stdout.flush()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_env(),
        check=False,
        stderr=subprocess.STDOUT,
    )
    print("-" * 78)
    if completed.returncode == 0:
        print(f"{label}: PASS")
    else:
        print(f"{label}: FAIL (exit code {completed.returncode})")
    return completed.returncode


def _run_pytest(label: str, tests: list[str]) -> int:
    missing = [path for path in tests if not (REPO_ROOT / path).exists()]
    if missing:
        print()
        print(f"{label}: FAIL")
        print("Missing expected test files/folders:")
        for path in missing:
            print(f"  - {path}")
        return 1
    return _run(label, [sys.executable, "-m", "pytest", *tests, "-q"])


def _run_frontend_build() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print()
        print("Frontend build check: FAIL")
        print("npm was not found on PATH. Install Node.js/npm or skip this optional check.")
        return 1
    return _run(
        "Optional Sprint 11 frontend build check",
        [npm, "--prefix", "frontend", "run", "build"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Sprint 10 DB connectivity and Sprint 11 SQL Server ingestor work.",
    )
    parser.add_argument(
        "--sprint",
        choices=("10", "11", "all"),
        default="all",
        help="Choose which sprint suite to run. Default: all.",
    )
    parser.add_argument(
        "--include-frontend-build",
        action="store_true",
        help="Also run npm run build in frontend/ to verify Sprint 11 UI wiring.",
    )
    args = parser.parse_args()

    print("AgentIQ Sprint 10 + Sprint 11 verification")
    print("Repo root:", REPO_ROOT)
    print("Python:", sys.executable)
    print("PYTHONPATH will include:")
    print("  -", REPO_ROOT / "backend")
    print("  -", REPO_ROOT)
    sys.stdout.flush()

    results: list[tuple[str, int]] = []

    if args.sprint in ("10", "all"):
        results.append(
            (
                "Sprint 10 DB connectivity verification",
                _run_pytest("Sprint 10 DB connectivity verification", SPRINT_10_TESTS),
            )
        )

    if args.sprint in ("11", "all"):
        results.append(
            (
                "Sprint 11 SQL Server ingestor verification",
                _run_pytest("Sprint 11 SQL Server ingestor verification", SPRINT_11_TESTS),
            )
        )

    if args.include_frontend_build:
        results.append(("Sprint 11 frontend build verification", _run_frontend_build()))

    print()
    print("=" * 78)
    print("FINAL SUMMARY")
    print("=" * 78)
    for label, code in results:
        status = "PASS" if code == 0 else "FAIL"
        print(f"{label}: {status}")

    failed = [label for label, code in results if code != 0]
    if failed:
        print()
        print("OVERALL: FAIL")
        print("These checks failed:")
        for label in failed:
            print(f"  - {label}")
        return 1

    print()
    print("OVERALL: PASS")
    print("Sprint 10 and Sprint 11 verification suites passed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
