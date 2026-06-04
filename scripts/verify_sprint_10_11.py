"""Run focused Track 3 Sprint 10 and Sprint 11 verification suites.

Usage from the AgentIQ repo root:

    python scripts/verify_sprint_10_11.py

By default this script verifies Track 3 temporal storage and trend/baseline
intelligence only. SQL Server operational-signal smoke tests are available via
an explicit opt-in flag so CI does not require live database credentials.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

TRACK3_SPRINT_10_TESTS = [
    "backend/tests/contract/test_signal_snapshots_schema.py",
    "backend/tests/contract/test_signal_snapshot_dataclass.py",
    "backend/tests/contract/test_signal_snapshot_telemetry.py",
    "backend/tests/contract/test_run_signal_snapshot_telemetry.py",
    "backend/tests/contract/test_temporal.py",
    "backend/tests/contract/test_temporal_task3.py",
    "backend/tests/contract/test_temporal_task4.py",
]

TRACK3_SPRINT_11_TESTS = [
    "backend/tests/contract/test_temporal_routes_task10.py",
    "backend/tests/contract/test_trend_engine.py",
    "backend/tests/contract/test_task_t6_llm_enrichment.py",
    "backend/tests/contract/test_t12_sprint11_contract.py",
    "backend/tests/contract/test_stack_builder_temporal_wiring.py",
]

SQLSERVER_SMOKE_TESTS = [
    "backend/tests/contract/test_sqlserver_ingestor.py",
    "backend/tests/contract/test_db_ticket_volume_surge.py",
    "backend/tests/contract/test_db_queue_depth_elevated.py",
    "backend/discovery/tests/test_db_sla_breach_rate.py",
    "backend/tests/contract/test_sqlserver_opsignal_scorer.py",
    "backend/tests/contract/test_sqlserver_opsignal_pack_registration.py",
    "backend/tests/contract/test_db_ingestor_completed_telemetry.py",
]

FRONTEND_TEMPORAL_TESTS = [
    "src/__tests__/T11_BaselineContextPanel_integration.test.tsx",
]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT / "backend"), str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("DEV_JWT", "dev-token-change-me")
    env.setdefault("SEED_DIR", str(REPO_ROOT / "backend" / "database" / "seed"))
    env.setdefault("CORS_ORIGINS", "http://localhost:5173")
    return env


def _run(label: str, command: list[str], *, cwd: Path = REPO_ROOT) -> int:
    print()
    print("=" * 78)
    print(label)
    print("=" * 78)
    print("Working directory:", cwd)
    print("Command:", " ".join(command))
    print("-" * 78)
    sys.stdout.flush()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=_env(),
        check=False,
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


def _run_frontend_temporal_tests() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print()
        print("Frontend temporal test check: FAIL")
        print("npm was not found on PATH. Install Node.js/npm or skip this optional check.")
        return 1

    return _run(
        "Track 3 Sprint 11 frontend Baseline Context verification",
        [npm, "exec", "vitest", "run", *FRONTEND_TEMPORAL_TESTS],
        cwd=REPO_ROOT / "frontend",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Track 3 Sprint 10 temporal storage and Sprint 11 trend/baseline work.",
    )
    parser.add_argument(
        "--sprint",
        choices=("10", "11", "all"),
        default="all",
        help="Choose which Track 3 sprint suite to run. Default: all.",
    )
    parser.add_argument(
        "--include-frontend",
        action="store_true",
        help="Also run the frontend BaselineContextPanel integration test.",
    )
    parser.add_argument(
        "--include-sqlserver-smoke",
        action="store_true",
        help=(
            "Also run SQL Server operational-signal smoke tests. "
            "This is optional because some environments do not have DB drivers or credentials."
        ),
    )
    args = parser.parse_args()

    print("AgentIQ Track 3 Sprint 10 + Sprint 11 verification")
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
                "Track 3 Sprint 10 temporal storage verification",
                _run_pytest(
                    "Track 3 Sprint 10 temporal storage verification",
                    TRACK3_SPRINT_10_TESTS,
                ),
            )
        )

    if args.sprint in ("11", "all"):
        results.append(
            (
                "Track 3 Sprint 11 trend and baseline verification",
                _run_pytest(
                    "Track 3 Sprint 11 trend and baseline verification",
                    TRACK3_SPRINT_11_TESTS,
                ),
            )
        )

    if args.include_frontend:
        results.append(
            (
                "Track 3 Sprint 11 frontend verification",
                _run_frontend_temporal_tests(),
            )
        )

    if args.include_sqlserver_smoke:
        results.append(
            (
                "Optional SQL Server operational-signal smoke verification",
                _run_pytest(
                    "Optional SQL Server operational-signal smoke verification",
                    SQLSERVER_SMOKE_TESTS,
                ),
            )
        )

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
    print("Track 3 Sprint 10 and Sprint 11 verification suites passed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
