"""Every materialisation path must persist the evidence-pointer trail.

The bug this guards against, found by walking 2.0-B1's Source Trace panel against
a real run: the platform has TWO near-identical materialisation functions —

  * ``materialize_t2.run_trackb_and_persist``      (POST /api/runs/start)
  * ``routes_sprint4_t1._run_trackb_and_persist``  (POST /api/runs/{id}/compute)

— and only the first called ``store_evidence_pointers``. The product starts runs
through ``/compute``, so in practice **no run wrote pointers at all**. The
consequence was silent and looked like a data problem rather than a code one:
``load_finding_trace`` loaded an empty pointer list, built no ``source_record``
hops, and every finding reported ``complete: false / no_source_record``. The chain
appeared to stop at the evidence layer because the provenance was never written,
not because the finding was thin.

That is exactly the failure mode 2.0-B1 AC1 exists to prevent — "any finding
expands to a complete chain TERMINATING IN SOURCE RECORDS" — defeated one layer
below the code that measures it.

So this is an AST guard over the SOURCE of both functions rather than a
behavioural test: the two implementations are ~300 lines each with heavy DB and
pipeline dependencies, and the property worth pinning is structural — *this call
is present in every path* — not what one path returns on one fixture. Consolidating
the two functions is the real fix and a separate change; until then this stops them
drifting apart again, and it will fail loudly if a THIRD path appears.

Run: python -m pytest tests/unit/test_evidence_pointers_written_by_every_run_path.py
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Dict, List, Set

import pytest

from app import materialize_t2, routes_sprint4_t1

#: (module, function) for every function that materialises a run's artifacts.
#: A new one must be added here AND must store pointers — see
#: ``test_no_unlisted_materialisation_path_exists``.
MATERIALISATION_PATHS = [
    (materialize_t2, "run_trackb_and_persist"),
    (routes_sprint4_t1, "_run_trackb_and_persist"),
]

#: The KV writes that mark a function as materialising a run's artifacts.
_ARTIFACT_KEYS = {"opps", "evidence"}


def _function_node(module, name: str) -> ast.AST:
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{module.__name__}.{name} not found")


def _called_names(node: ast.AST) -> Set[str]:
    """Every simple/attribute call name inside ``node``."""
    names: Set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _kv_keys_written(node: ast.AST) -> Set[str]:
    """The run-scoped KV keys a function writes, read from literal first args."""
    keys: Set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "run_kv_set"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, str)
        ):
            keys.add(child.args[0].value)
    return keys


@pytest.mark.parametrize(
    "module,name", MATERIALISATION_PATHS, ids=lambda v: getattr(v, "__name__", v)
)
def test_every_materialisation_path_stores_evidence_pointers(module, name):
    """The guard. A path that writes findings but not their provenance produces a
    run whose every finding is permanently un-traceable to its sources."""
    called = _called_names(_function_node(module, name))
    assert "store_evidence_pointers" in called, (
        f"{module.__name__}.{name} writes a run's artifacts but never calls "
        f"store_evidence_pointers, so findings from this path can never reach "
        f"their source records (2.0-B1 AC1)."
    )


@pytest.mark.parametrize(
    "module,name", MATERIALISATION_PATHS, ids=lambda v: getattr(v, "__name__", v)
)
def test_each_listed_path_really_does_materialise(module, name):
    """Keeps the registry above honest — a listed function that no longer writes
    the artifacts would make the guard pass while guarding nothing."""
    written = _kv_keys_written(_function_node(module, name))
    assert _ARTIFACT_KEYS <= written, (
        f"{module.__name__}.{name} is registered as a materialisation path but "
        f"writes {sorted(written)}; expected at least {sorted(_ARTIFACT_KEYS)}."
    )


def test_no_unlisted_materialisation_path_exists():
    """A THIRD path writing both artifact keys must be registered above.

    Without this, the next duplicated materialisation function reintroduces the
    same hole and both guards above still pass.

    Scoped to functions writing BOTH ``opps`` and ``evidence``: the analyst
    decision/override handlers in ``main.py`` rewrite the ``opps`` blob alone, and
    they are not materialisation — pointers are keyed separately and survive a
    decision untouched.
    """
    listed = {(m.__name__, n) for m, n in MATERIALISATION_PATHS}
    offenders: List[str] = []
    app_dir = Path(inspect.getfile(materialize_t2)).parent

    for path in sorted(app_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a patch/scratch file
            continue
        module_name = f"app.{path.stem}"
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (_ARTIFACT_KEYS <= _kv_keys_written(node)):
                continue
            if (module_name, node.name) in listed:
                continue
            offenders.append(f"{module_name}.{node.name}")

    assert not offenders, (
        f"unregistered materialisation path(s) {offenders} write both 'opps' and "
        f"'evidence'. Add them to MATERIALISATION_PATHS — and make sure they store "
        f"evidence pointers, or their findings will never reach their source records."
    )


def test_the_two_paths_pass_the_same_arguments():
    """A pointer index built from different inputs would trace differently
    depending on which endpoint started the run — a difference no user could see
    and nobody would think to look for."""
    signatures: Dict[str, Set[str]] = {}
    for module, name in MATERIALISATION_PATHS:
        for child in ast.walk(_function_node(module, name)):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "store_evidence_pointers"
            ):
                signatures[module.__name__] = {
                    kw.arg for kw in child.keywords if kw.arg
                } | {f"positional:{len(child.args)}"}
    assert len(signatures) == len(MATERIALISATION_PATHS), (
        f"could not read a store_evidence_pointers call from every path: {signatures}"
    )
    distinct = {frozenset(v) for v in signatures.values()}
    assert len(distinct) == 1, (
        f"the materialisation paths call store_evidence_pointers differently: "
        f"{signatures}"
    )
