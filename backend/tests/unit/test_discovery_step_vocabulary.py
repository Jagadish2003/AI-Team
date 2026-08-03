"""Discovery step vocabulary: every step the runner emits must be a REAL step.

``db.update_run_step()`` validates its ``step_id`` against
``discovery/steps.py::DISCOVERY_STEP_IDS`` and SKIPS the write for an unknown id
(logging a WARNING). That is the right posture — a typo must not clobber a valid
step — but it means an emission the vocabulary does not know about is silently
dropped, and the Discovery Progress row for that stage never advances. Exactly
that happened to ``sf_fsc``: the runner emitted it after the Financial Services
Cloud ingest while the vocabulary had no such id, so FSC progress never moved.

The structural test below closes that class of bug at build time: it walks
``discovery/runner.py`` with ``ast`` and asserts every literal step id passed to
``update_run_step`` is a member of the canonical set. It needs no DB and cannot
be satisfied by a comment — a new stage emission with no vocabulary entry fails.

The rest pins the two native cloud-event connectors (MSP-B1 AWS / MSP-B2 Azure),
which previously emitted NO step at all: with nothing to be sequential with, the
frontend rendered them as generic rows that all spun simultaneously with
whichever real step was running.

DB-backed persistence of these ids lives in
``tests/contract/test_discovery_step_emission.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set

import pytest

from discovery.steps import DISCOVERY_STEPS, DISCOVERY_STEP_IDS


RUNNER_PATH = Path(__file__).resolve().parents[2] / "discovery" / "runner.py"


def _emitted_step_ids() -> List[str]:
    """Every literal step id passed to update_run_step() in the runner."""
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    emitted: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "update_run_step":
            continue
        # update_run_step(run_id, step_id, ok=...) — the step id is arg 2.
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            value = node.args[1].value
            if isinstance(value, str):
                emitted.append(value)
    return emitted


# ---------------------------------------------------------------------------
# Structural: the runner's emissions and the step vocabulary cannot drift
# ---------------------------------------------------------------------------

def test_every_emitted_step_id_is_in_the_canonical_vocabulary():
    emitted: Set[str] = set(_emitted_step_ids())
    assert emitted, "no update_run_step() emissions found — did the parse break?"
    unknown = sorted(emitted - set(DISCOVERY_STEP_IDS))
    assert not unknown, (
        "discovery/runner.py emits step id(s) that discovery/steps.py does not "
        "declare, so db.update_run_step() drops the write and the Discovery "
        f"Progress row for that stage never advances: {unknown}. Add them to "
        "DISCOVERY_STEPS (in emission order)."
    )


def test_the_native_cloud_connectors_emit_a_step():
    emitted = set(_emitted_step_ids())
    for step_id in ("azure_events", "aws_events"):
        assert step_id in emitted, (
            f"{step_id} must emit a discovery step so the Discovery Progress list "
            "can show it sequentially rather than inferring its state"
        )


def test_fsc_second_pass_emits_its_own_step():
    """sf_fsc is a distinct pass from sf_ncino and needs its own step id."""
    assert "sf_fsc" in DISCOVERY_STEP_IDS
    assert "sf_fsc" in set(_emitted_step_ids())


# ---------------------------------------------------------------------------
# Order: the vocabulary order IS the progress order the frontend renders
# ---------------------------------------------------------------------------

def test_step_order_matches_ingest_order():
    idx = {step: i for i, step in enumerate(DISCOVERY_STEPS)}
    # The native cloud connectors are ingested after Jira and before Slack.
    assert idx["jira"] < idx["azure_events"] < idx["slack"]
    assert idx["azure_events"] < idx["aws_events"] < idx["slack"]
    # Both Salesforce second passes sit after every source and before detection.
    assert idx["dotnet_app"] < idx["sf_ncino"] < idx["sf_fsc"] < idx["detect"]
    assert idx["detect"] < idx["enrich"] < idx["complete"]
    assert DISCOVERY_STEPS[-1] == "complete"
    assert len(DISCOVERY_STEPS) == len(set(DISCOVERY_STEPS)), "duplicate step id"


# ---------------------------------------------------------------------------
# Health status → step outcome
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected_ok",
    [
        ("ok", True),
        # Partial data WAS ingested and the reason is carried in the run's
        # cloudOpsRuntime health block — a red "failed" row would overstate it.
        ("degraded", True),
        ("unavailable", False),
        # Selected for the run but no accounts/subscriptions pinned: the
        # connector delivered nothing, so a green check would be dishonest.
        ("not_configured", False),
        ("", False),
    ],
)
def test_cloud_event_step_ok_mapping(status, expected_ok):
    from discovery.runner import _cloud_event_step_ok

    assert _cloud_event_step_ok({"health": {"status": status}}) is expected_ok


def test_cloud_event_step_ok_handles_a_missing_health_block():
    from discovery.runner import _cloud_event_step_ok

    assert _cloud_event_step_ok({}) is False
    assert _cloud_event_step_ok({"health": None}) is False
