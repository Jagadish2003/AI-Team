"""2.0-B4 T5 — conformance fixture loader + mapper resolution (AT-814 / AC4).

AC4: *every connector has conformance fixtures; CI fails if a connector lacks them.*
This module is the reusable substrate the CI gate
(`tests/contract/test_r2_0_b4_t5_conformance_fixtures.py`) reads, kept out of the
test so a future surface (a certification review in 2.0-C2, a CLI, the T2 mapper
work) can read the same fixtures the same way.

What a conformance fixture is. One JSON file per shipped connector under
``fixtures/conformance/<connector_id>.json``. It carries two things:

* a **locked snapshot** of the connector's per-concept conformance declaration
  (status + reason + mapper), pinned to ``discovery.concepts.conformance.CONFORMANCE``
  by the gate — so a registry change without a fixture change fails CI, and a new
  shipped connector that arrives with no fixture fails CI (the AC4 discipline: "a
  new connector ships with its conformance fixtures or does not ship"); and
* zero or more **mapping cases** — a raw source sample plus the exact normalised
  concepts a named mapper must produce from it. The gate runs the mapper and
  asserts the output matches, which is how a fixture *proves the mapping is
  correct* rather than merely asserting a status.

Nothing here reaches into a connector's ingest path. A mapping case names its mapper
as a ``"module.path:function"`` string that :func:`resolve_mapper` imports on demand,
so the fixture data and the code that satisfies it stay decoupled — exactly what lets
the T2 per-connector mappers register against these fixtures as they land, flipping a
``declared`` concept to ``supported`` only once a case proves it.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

CONFORMANCE_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "conformance"


def fixture_path(connector_id: str) -> Path:
    """The fixture file for a connector (whether or not it exists)."""
    return CONFORMANCE_FIXTURE_DIR / f"{connector_id}.json"


def available_fixture_ids() -> set[str]:
    """The connector ids that HAVE a conformance fixture on disk.

    This is the set the AC4 presence gate compares against the shipped-connector
    registry: a shipped connector absent here fails CI.
    """
    if not CONFORMANCE_FIXTURE_DIR.is_dir():
        return set()
    return {p.stem for p in CONFORMANCE_FIXTURE_DIR.glob("*.json")}


def load_fixture(connector_id: str) -> Dict[str, Any]:
    """Load and parse one connector's conformance fixture."""
    path = fixture_path(connector_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"no conformance fixture for {connector_id!r} at {path} — every shipped "
            f"connector must have one (2.0-B4 AC4)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_fixtures() -> Dict[str, Dict[str, Any]]:
    """Every conformance fixture on disk, keyed by connector id."""
    return {cid: load_fixture(cid) for cid in sorted(available_fixture_ids())}


def resolve_mapper(dotted: str) -> Callable[..., Any]:
    """Resolve a ``"module.path:function"`` reference to the callable it names.

    A mapping case names its mapper as data; the gate resolves it here rather than
    importing every mapper eagerly, so a fixture for a connector whose mapper is not
    built yet simply carries no case, and one whose mapper exists points at readable
    code a reviewer can open.
    """
    module_path, sep, attr = str(dotted).partition(":")
    if not sep or not module_path.strip() or not attr.strip():
        raise ValueError(
            f"mapper reference must be 'module.path:function', got {dotted!r}"
        )
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:  # project-root execution uses backend as package
        module = importlib.import_module(f"backend.{module_path}")
    mapper = getattr(module, attr, None)
    if not callable(mapper):
        raise ValueError(f"{dotted!r} does not resolve to a callable mapper")
    return mapper


def run_mapping_case(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run a mapping case's mapper on its raw input and return the produced concept
    dicts (each concept's ``to_dict()``), ready to compare against the case's
    ``expected`` golden.
    """
    mapper = resolve_mapper(case["mapper"])
    org_id = case.get("org_id") or "org"
    produced = mapper(case["raw"], org_id=org_id)
    return [concept.to_dict() for concept in produced]


__all__ = [
    "CONFORMANCE_FIXTURE_DIR",
    "fixture_path",
    "available_fixture_ids",
    "load_fixture",
    "load_all_fixtures",
    "resolve_mapper",
    "run_mapping_case",
]
