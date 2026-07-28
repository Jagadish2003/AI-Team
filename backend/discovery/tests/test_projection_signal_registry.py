"""2.0-A1 T1 — structural tests keeping the projection signal registry honest.

The projection's whole claim to honesty is that it names signals the platform
ACTUALLY measures. That holds only if the registry and the detectors stay in
lockstep, so these tests walk the detector tree at test time (no hardcoded
module list) and fail the build on drift:

  * every signal a profile names must appear in the owning detector's own
    ``SIGNAL_METRICS`` list — otherwise a projection cites a field that is never
    measured, never snapshotted, and never re-measurable by 2.0-A2;
  * every detector that declares ``SIGNAL_METRICS`` must have a profile — so a
    newly-added detector cannot silently ship with no projection.

Both are unlisted-by-construction: adding a detector or renaming a metric fails
here without any test edit.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Dict, Set

import pytest

from discovery.projection.signal_registry import (
    SIGNAL_CONCEPTS,
    get_detector_profile,
    known_detector_ids,
)

_DETECTOR_DIR = pathlib.Path(__file__).resolve().parents[1] / "detectors"


def _string_items(node: ast.AST) -> Set[str]:
    """Collect string constants from a list/tuple/dict literal AST node."""
    names: Set[str] = set()
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                names.add(element.value)
    elif isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    return names


def _parse_detector_modules() -> Dict[str, Set[str]]:
    """Map DETECTOR_ID -> declared SIGNAL_METRICS names, read from source.

    Parsed with ``ast`` rather than imported so this test cannot be affected by
    import-time side effects, and so a module with several SIGNAL_METRICS
    assignments contributes all of them (several detectors declare a documented
    dict that a list then shadows — the union is the honest superset for a
    "is this a real measured field" check).
    """
    detectors: Dict[str, Set[str]] = {}
    for path in sorted(_DETECTOR_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        detector_id = None
        metrics: Set[str] = set()
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "DETECTOR_ID" and isinstance(
                    node.value, ast.Constant
                ):
                    detector_id = node.value.value
                elif target.id == "SIGNAL_METRICS" and node.value is not None:
                    metrics |= _string_items(node.value)
        if detector_id:
            # approval_delay.py and approval_bottleneck.py share a DETECTOR_ID;
            # union their metric names.
            detectors.setdefault(str(detector_id), set()).update(metrics)
    return detectors


@pytest.fixture(scope="module")
def detector_metrics() -> Dict[str, Set[str]]:
    parsed = _parse_detector_modules()
    assert parsed, "no detector modules parsed — has the detectors package moved?"
    return parsed


def test_every_profiled_signal_is_a_real_declared_metric(detector_metrics):
    """A projection may only cite a field the detector actually measures."""
    problems = []
    for detector_id in known_detector_ids():
        declared = detector_metrics.get(detector_id)
        if declared is None:
            problems.append(
                f"{detector_id}: profile exists but no detector module declares it"
            )
            continue
        profile = get_detector_profile(detector_id)
        for label, name in (
            ("movement_signal", profile.movement_signal),
            ("volume_signal", profile.volume_signal),
            ("instance_signal", profile.instance_signal),
        ):
            if name and name not in declared:
                problems.append(
                    f"{detector_id}.{label}={name!r} is not in that detector's "
                    f"SIGNAL_METRICS {sorted(declared)}"
                )
    assert not problems, "projection registry drifted from the detectors:\n" + "\n".join(
        problems
    )


def test_every_detector_with_signal_metrics_has_a_projection_profile(
    detector_metrics,
):
    """A new detector cannot ship without a projection profile."""
    unprofiled = sorted(
        detector_id
        for detector_id, metrics in detector_metrics.items()
        if metrics and get_detector_profile(detector_id) is None
    )
    assert not unprofiled, (
        "these detectors declare SIGNAL_METRICS but have no projection profile in "
        "discovery/projection/signal_registry.py — add one (or the finding will "
        f"carry no projection): {unprofiled}"
    )


def test_no_stale_profiles(detector_metrics):
    """A profile for a detector that no longer exists must be removed."""
    stale = sorted(
        detector_id
        for detector_id in known_detector_ids()
        if detector_id not in detector_metrics
    )
    assert not stale, f"projection profiles for non-existent detectors: {stale}"


def test_every_profile_is_internally_complete():
    for detector_id in known_detector_ids():
        profile = get_detector_profile(detector_id)
        assert profile.concept in SIGNAL_CONCEPTS, (
            f"{detector_id} uses unknown concept {profile.concept!r}"
        )
        assert profile.movement_signal, f"{detector_id} has no movement_signal"
        assert profile.manual_step, f"{detector_id} has no manual_step"
        assert profile.unit in ("count", "days", "hours", "ratio", "pct"), (
            f"{detector_id} has unknown unit {profile.unit!r}"
        )
        # A count/duration detector must resolve an instance field; a rate-based
        # one legitimately has none (see instance_field's docstring).
        if profile.unit in profile.RATE_UNITS:
            assert profile.instance_field is None or profile.instance_signal, (
                f"{detector_id} is rate-based but falls back to its rate as an "
                "instance count"
            )
        else:
            assert profile.instance_field, (
                f"{detector_id} resolves no instance field"
            )


def test_no_rate_field_is_used_as_a_count(detector_metrics):
    """A rate/share/average can never stand in for an affected-instance count.

    This is the defect class that made a real 42%-breach-rate finding project
    "no material change": a value like 0.42 read as 0.42 observed instances.
    """
    rateish = ("_pct", "_rate", "_ratio", "_score", "_avg", "vs_baseline")
    problems = []
    for detector_id in known_detector_ids():
        profile = get_detector_profile(detector_id)
        for label, name in (
            ("instance_signal", profile.instance_signal),
            ("volume_signal", profile.volume_signal),
        ):
            if name and any(token in name for token in rateish):
                problems.append(
                    f"{detector_id}.{label}={name!r} names a rate/average but is "
                    "used as a countable population"
                )
    assert not problems, "\n".join(problems)


def test_manual_steps_describe_a_manual_step_not_a_saving():
    """The manual step is intervention language, never savings language."""
    for detector_id in known_detector_ids():
        step = get_detector_profile(detector_id).manual_step.lower()
        assert not step.endswith("."), f"{detector_id}: manual_step is a phrase"
        for forbidden in ("save", "saving", "roi", "guarantee", "%"):
            assert forbidden not in step, (
                f"{detector_id}: manual_step contains {forbidden!r} — it must "
                "describe the manual step, not a projected benefit"
            )


def test_detector_id_lookup_is_case_and_whitespace_tolerant():
    profile = get_detector_profile("HANDOFF_FRICTION")
    assert get_detector_profile("  handoff_friction  ") is profile
    assert get_detector_profile("") is None
    assert get_detector_profile(None) is None
