"""2.0-A3 T2 — structural proof that base scoring learns nothing.

The subtask's defining constraint: the adjustment is a LAYER, not an edit.
``discovery/scorer.py`` — specifically ``_compute_impact()`` and
``_rescale_impact()``, which map a detector result onto the 1-10 impact score —
must not learn anything and must not change.

**Why structurally.** A behavioural test can show that today's base score is
unaffected. It cannot stop someone reaching into the scorer next quarter to
"just nudge impact by the accepted count", which is the tempting and wrong fix
the moment learning looks too weak. Once base scoring learns, "what would this
have ranked without learning?" becomes unanswerable — there is no untouched
number left to answer it with — and every promise in this story collapses
quietly.

The companion guard is ``test_learning_signal_isolation.py``, which enforces the
other direction (the learning layer must not write scoring fields). Together
they pin the boundary from both sides.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCORER = BACKEND / "discovery" / "scorer.py"
CLOUD_OPS_SCORER = BACKEND / "discovery" / "packs" / "cloud_ops_scorer.py"

#: Every base-scoring module. These produce the thing the layer adjusts, so none
#: of them may consume a learning input.
BASE_SCORING_MODULES = (
    SCORER,
    CLOUD_OPS_SCORER,
    BACKEND / "discovery" / "lending_scorer.py",
    BACKEND / "discovery" / "strs_benefits_scorer.py",
    BACKEND / "discovery" / "packs" / "financial_services_cloud_scorer.py",
    BACKEND / "discovery" / "packs" / "github_engineering_scorer.py",
    BACKEND / "discovery" / "packs" / "security_ops_scorer.py",
)

#: Anything that would mean the base scorer had started learning.
LEARNING_TOKENS = (
    "learning_signals",
    "learning_feedback",
    "learning_adjustment",
    "opportunity_feedback",
    "ranking_adjustment",
    "adjust_ranking",
    "collect_learning_signals",
    "GroupAdjustment",
)


def _existing(paths):
    return [p for p in paths if p.exists()]


class TestBaseScorersDoNotImportTheLearningLayer:
    @pytest.mark.parametrize(
        "path", _existing(BASE_SCORING_MODULES), ids=lambda p: p.name
    )
    def test_no_base_scorer_references_a_learning_module(self, path: Path):
        source = path.read_text(encoding="utf-8")
        for token in LEARNING_TOKENS:
            assert token not in source, (
                f"{path.name} references {token!r}. Base scoring must learn "
                "nothing: the moment it does, the base score stops being the "
                "answer to 'what would this have ranked without learning?' and "
                "there is no untouched number left to recover."
            )

    @pytest.mark.parametrize(
        "path", _existing(BASE_SCORING_MODULES), ids=lambda p: p.name
    )
    def test_no_base_scorer_reads_the_adjustment_tables(self, path: Path):
        source = path.read_text(encoding="utf-8").lower()
        for table in ("ranking_adjustments", "ranking_adjustment_history"):
            assert table not in source


class TestTheImpactFunctionsAreUnchanged:
    """The two functions the subtask names by name.

    Pinned by BEHAVIOUR rather than by a source hash: a hash would fail on a
    comment edit and teach everyone to update it without reading, which is worse
    than no guard. These assert the mapping itself.
    """

    def test_rescale_impact_still_maps_onto_one_to_ten(self):
        from discovery.scorer import _RAW_IMPACT_MAX, _RAW_IMPACT_MIN, _rescale_impact

        assert _rescale_impact(_RAW_IMPACT_MIN) == 1
        assert _rescale_impact(_RAW_IMPACT_MAX) == 10
        for raw in (-100.0, 0.0, 3.0, 100.0):
            assert 1 <= _rescale_impact(raw) <= 10

    def test_rescale_impact_is_monotonic(self):
        from discovery.scorer import _rescale_impact

        values = [_rescale_impact(raw / 10.0) for raw in range(0, 80)]
        assert values == sorted(values)

    def test_rescale_impact_is_deterministic(self):
        from discovery.scorer import _rescale_impact

        assert len({_rescale_impact(4.2) for _ in range(20)}) == 1

    def test_neither_impact_function_takes_a_learning_argument(self):
        """A learned input cannot reach base scoring through its signature."""
        from discovery.scorer import _compute_impact, _rescale_impact

        for fn in (_compute_impact, _rescale_impact):
            params = set(inspect.signature(fn).parameters)
            for smell in ("org_id", "adjustment", "learning", "feedback", "signals"):
                assert not any(smell in p for p in params), (
                    f"{fn.__name__} accepts {smell!r} — base scoring must be a "
                    "pure function of the detector result and its source weight"
                )

    def test_compute_impact_depends_only_on_the_detector_result(self):
        """No org, no DB, no clock reachable from the impact computation."""
        source = inspect.getsource(
            __import__("discovery.scorer", fromlist=["_compute_impact"])._compute_impact
        )
        for hazard in ("db.", "connect(", "datetime.now", "os.environ", "getenv"):
            assert hazard not in source


class TestTheScorerModuleStaysPure:
    def test_the_scorer_does_not_import_the_app_package(self):
        """The layer lives in ``app``; the base scorer must not reach into it.

        This is the import that would make a learned adjustment reachable from
        base scoring without any of the named tokens appearing.
        """
        tree = ast.parse(SCORER.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name if node.names else None
            if module and module.split(".")[0] == "app":
                raise AssertionError(
                    f"discovery/scorer.py imports {module!r}; base scoring must "
                    "not be able to reach the adjustment layer"
                )


class TestTheLayerIsAppliedInExactlyOnePlace:
    """One adjustment function, many call sites — never a second implementation.

    Ordering is decided in several places already (base impact, roadmap stage
    membership, the cloud_ops pack rank, the served list). A learned adjustment
    added to more than one of them would compound into movement nobody could
    explain, which is why this is asserted rather than left to review.
    """

    def test_only_one_module_defines_an_adjustment_function(self):
        defining = []
        for path in (BACKEND / "app").glob("*.py"):
            # utf-8-sig, not utf-8: some modules in this tree carry a BOM, which
            # plain utf-8 preserves as ﻿ and ast.parse rejects.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "adjust_ranking":
                    defining.append(path.name)
        assert defining == ["learning_adjustment.py"], (
            f"adjust_ranking is defined in {defining}; there must be exactly one "
            "implementation of the learned adjustment"
        )

    def test_no_discovery_module_defines_an_adjustment_function(self):
        for path in (BACKEND / "discovery").rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text(encoding="utf-8-sig")
            assert "def adjust_ranking" not in source, (
                f"{path} defines its own adjustment — the layer belongs above "
                "discovery, not inside it"
            )

    @pytest.mark.parametrize(
        "module,function",
        [
            ("app/main.py", "_apply_learned_ranking"),
            ("app/roadmap_engine.py", "apply_learned_adjustment"),
        ],
    )
    def test_each_ordering_surface_calls_the_shared_function(self, module, function):
        source = (BACKEND / module).read_text(encoding="utf-8")
        assert function in source
        assert "adjust_ranking" in source, (
            f"{module} must route through the shared adjustment function"
        )

    def test_build_roadmap_itself_never_adjusts(self):
        """The roadmap is BUILT in base order and stored that way.

        ``build_roadmap`` runs during materialization and its result is persisted
        (``run_kv_set("roadmap", …)``). Adjusting inside it would bake the
        learned order into storage — and then disabling learning could not
        restore what was stored, so "what would this have ranked without
        learning?" would have no answer for the roadmap surface. Materialization
        also runs without a request-scoped tenancy context, so the org would be
        wrong or absent.
        """
        import inspect

        from app.roadmap_engine import build_roadmap

        source = inspect.getsource(build_roadmap)
        for hazard in ("adjust_ranking", "apply_learned_adjustment", "get_adjustments"):
            assert hazard not in source, (
                f"build_roadmap references {hazard!r}; the learned adjustment "
                "belongs on the SERVE path, not in the function whose output is "
                "written to storage"
            )

    def test_the_materialization_paths_store_an_unadjusted_roadmap(self):
        for module in ("app/materialize_t2.py", "app/routes_sprint4_t1.py"):
            source = (BACKEND / module).read_text(encoding="utf-8")
            assert "apply_learned_adjustment" not in source, (
                f"{module} materializes and STORES the roadmap; applying learning "
                "there would persist an adjusted order as if it were the base"
            )

    def test_the_cloud_ops_pack_rank_is_not_an_adjustment_point(self):
        """It produces a BASE rank, so learning applies above it, not inside it."""
        source = CLOUD_OPS_SCORER.read_text(encoding="utf-8")
        assert "ops_impact_rank" in source, "the base pack rank still exists"
        assert "adjust_ranking" not in source
