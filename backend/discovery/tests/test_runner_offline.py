"""
Smoke test: runner executes end-to-end in offline mode without error.
"""
import os
os.environ["INGEST_MODE"] = "offline"


def test_runner_offline_no_error():
    from discovery.runner import run
    result = run("offline")
    assert isinstance(result, list)
    # Stubs return 0 opportunities — that is correct at SF-2.1
    # SF-2.5 detectors will populate this list
