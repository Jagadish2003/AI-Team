"""Track B invocation adapter for Track A.

Two execution strategies:
1) in_process(): import Track B runner and call it directly
2) subprocess(): call a CLI and parse JSON output

Returns:
  {
    "opportunities": [...],
    "evidence": [...],
    "score_debug": {...}  # optional
  }
"""

import json
import os
import subprocess
from typing import Any, Dict, List, Optional

DEFAULT_SYSTEMS = ["salesforce", "servicenow", "jira"]

def _runner_mode() -> str:
    return os.getenv("TRACKB_RUNNER_MODE", "in_process").lower().strip()

def run_trackb(*, mode: str, systems: Optional[List[str]] = None, run_context: Optional[Dict[str, Any]] = None, pack: Optional[str] = None) -> Dict[str, Any]:
    systems = systems or DEFAULT_SYSTEMS
    run_context = run_context or {}
    if _runner_mode() == "subprocess":
        return _run_subprocess(mode=mode, systems=systems, run_context=run_context, pack=pack)
    return _run_in_process(mode=mode, systems=systems, run_context=run_context, pack=pack)

def _run_in_process(*, mode: str, systems: List[str], run_context: Dict[str, Any], pack: Optional[str] = None) -> Dict[str, Any]:
    try:
        # Adjust import path to your Track B runner
        from discovery.runner import run  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Track B in-process runner import failed. "
            "Set TRACKB_RUNNER_MODE=subprocess or fix PYTHONPATH."
        ) from e

    payload = run(mode=mode, systems=systems, run_id=run_context.get("runId"), pack=pack)
    return payload

def _run_subprocess(*, mode: str, systems: List[str], run_context: Dict[str, Any]) -> Dict[str, Any]:
    cmd = [
        os.getenv("TRACKB_PYTHON", "python"),
        "-m",
        "discovery.runner",
        "--mode",
        mode,
        "--systems",
        ",".join(systems),
    ]
    if run_context.get("runId"):
        cmd.extend(["--run-id", str(run_context["runId"])])
    env = os.environ.copy()
    ctx_path = env.get("TRACKB_RUN_CONTEXT_PATH", "/tmp/trackb_run_context.json")
    with open(ctx_path, "w", encoding="utf-8") as f:
        json.dump(run_context, f)
    env["TRACKB_RUN_CONTEXT_PATH"] = ctx_path

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"Track B subprocess failed: {r.stderr.strip() or r.stdout.strip()}")
    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError("Track B subprocess did not return valid JSON") from e
