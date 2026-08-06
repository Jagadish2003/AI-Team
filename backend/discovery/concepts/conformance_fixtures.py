"""2.0-B4 T5 — conformance fixture loader (AT-814 / AC4).

AC4: *every connector has conformance fixtures; CI fails if a connector lacks them.*
This module is the reusable loader the CI gate
(`tests/contract/test_r2_0_b4_t5_conformance_fixtures.py`) reads, kept out of the test
so a future surface (a 2.0-C2 certification review, a CLI) can read the same fixtures
the same way.

A conformance fixture is one JSON file per shipped connector under
``fixtures/conformance/<connector_id>.json`` carrying a **locked snapshot** of that
connector's per-concept conformance declaration (status + reason + mapper +
field_gaps), pinned to ``discovery.concepts.conformance.CONFORMANCE`` by the gate — so
a registry change cannot ship without updating its golden, and a newly-shipped
connector that arrives with no fixture fails CI ("a connector ships with its
conformance fixtures or does not ship").

The raw→concept correctness of each ``supported`` mapper is proven by T2's
connector-mapping suite over its golden samples; this gate's job is the per-connector
fixture DISCIPLINE (presence + registry lock), and it checks that every ``supported``
claim in a fixture resolves to a real mapper in T2's registry
(``discovery.concepts.mappers.resolve_mapper``) rather than re-proving the mapping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

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


__all__ = [
    "CONFORMANCE_FIXTURE_DIR",
    "fixture_path",
    "available_fixture_ids",
    "load_fixture",
    "load_all_fixtures",
]
