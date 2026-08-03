"""2.0-A3 T1 — structural proof of what the learning layer may learn from.

The subtask asks for one test in particular: *"a structural test asserting the
layer reads nothing from telemetry.py"*. That is the test this file exists for,
plus the neighbouring structural guarantees that would be equally easy to lose.

**Why structurally rather than behaviourally.** A behavioural test can only prove
the paths it happens to exercise. The failure being guarded against is silent and
attractive: someone adds engagement data — dwell time, expand-clicks, which
findings got opened — because it is right there in ``telemetry.py`` and it
obviously correlates with what a team cares about. Nothing in the product would
look different afterwards. The ranking would simply start following attention
rather than evidence, and the sentence "it learns from what worked, not from what
was clicked" would quietly stop being true.

These tests read the source and fail the build when that happens.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"

#: Every module that constitutes the learning layer. A new one must be added
#: here — and ``test_the_layer_module_list_is_complete`` fails the build if a
#: module named like a learning module is missing from it, so the list cannot
#: silently fall behind the code.
LEARNING_LAYER_MODULES = (
    "learning_signals.py",
    "learning_feedback.py",
    "learning_signal_config.py",
    "routes_learning_feedback.py",
    # 2.0-A3 T2 — the adjustment layer. Added because the guard below demanded
    # it: these are the modules that most need the AC3 "never writes evidence,
    # confidence or corroboration" check, since they are the ones that touch
    # ordering.
    "learning_adjustment.py",
    "learning_adjustment_state.py",
    "routes_learning_adjustment.py",
    # 2.0-A3 T3 — the explainability copy. Added because the completeness guard
    # below demanded it. These matter here for a reason specific to T3: the
    # reason must never imply the learned signal contributed to a finding's
    # credibility, so the "no writes to evidence/confidence/corroboration" check
    # applies to the module that WRITES the customer-facing sentence.
    "learning_reason.py",
    "learning_reason_vocabulary.py",
)

#: The scoring and evidence fields the learning layer must never write.
#: AC3: "Adjustment never modifies a finding's evidence, confidence level, or
#: corroboration status." T1 applies no adjustment at all, so the guard here is
#: the stronger one: the signal set does not even have a path to those fields.
PROTECTED_FIELDS = (
    "confidence",
    "corroboration_sources",
    "corroboration_label",
    "corroboration_rule_ids",
    "triple_corroboration",
    "evidenceIds",
    "evidence_ids",
    "impact",
    "effort",
    "tier",
)


def _module_paths():
    return [APP / name for name in LEARNING_LAYER_MODULES]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_names(tree: ast.Module):
    """Every module name imported, including inside functions."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            # `from . import telemetry` — the module is the alias, not node.module
            if node.level and node.module is None:
                for alias in node.names:
                    names.add(alias.name)
            elif node.level:
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
    return names


# --------------------------------------------------------------------------
# The named requirement: nothing from telemetry.
# --------------------------------------------------------------------------


class TestTheLayerNeverReadsTelemetry:
    @pytest.mark.parametrize("path", _module_paths(), ids=LEARNING_LAYER_MODULES)
    def test_no_learning_module_imports_telemetry(self, path: Path):
        """Not 'does not call a read function' — does not import it at all.

        A weaker guard (no calls to specific telemetry readers) would pass the
        moment someone added a new reader function, which is precisely when it
        needs to fail.
        """
        assert path.exists(), f"{path.name} is missing — has the layer moved?"
        for imported in _imported_names(_tree(path)):
            assert "telemetry" not in imported.lower(), (
                f"{path.name} imports {imported!r}. The learning layer must not "
                "read engagement data: a ranking layer trained on what was "
                "clicked is a recommendation engine wearing an evidence "
                "platform's clothes. Learn from decisions and measured outcomes "
                "only."
            )

    @pytest.mark.parametrize("path", _module_paths(), ids=LEARNING_LAYER_MODULES)
    def test_no_learning_module_names_a_telemetry_symbol(self, path: Path):
        """Belt and braces: catches a late/dynamic import too."""
        source = path.read_text(encoding="utf-8")
        for smell in ("telemetry_events", "record_event(", "TELEMETRY"):
            assert smell not in source, (
                f"{path.name} references {smell!r} — see the sibling test for why "
                "telemetry is excluded from the learning layer entirely"
            )

    @pytest.mark.parametrize("path", _module_paths(), ids=LEARNING_LAYER_MODULES)
    def test_no_executable_code_mentions_telemetry(self, path: Path):
        """Docstrings may discuss telemetry; executable code may not name it.

        The modules explain at length WHY telemetry is excluded, so a plain
        substring search over the file would flag its own rationale. This
        unparses the AST with docstrings stripped, leaving only code.
        """
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body[0].value.value = ""

        code = ast.unparse(tree)
        assert "telemetry" not in code.lower(), (
            f"{path.name} names telemetry in executable code. Only the "
            "docstrings may discuss it — as the reason it is excluded."
        )


# --------------------------------------------------------------------------
# The two permitted sources, and only those.
# --------------------------------------------------------------------------


class TestTheLayerReadsOnlyItsTwoSources:
    def test_the_signal_set_reads_decisions_and_movements_and_nothing_else(self):
        """Every cross-module data read in learning_signals, enumerated.

        Two imports are permitted that are not data sources, and the distinction
        matters: the A1 signal registry supplies the signal CONCEPT used for
        similarity (not a signal value), and ``projection_validation`` supplies
        the verdict VOCABULARY (a constant — duplicating the string locally would
        be worse, since a rename there would silently stop matching here).
        Anything else is a new learning input and must be a deliberate decision,
        not a convenient import.
        """
        permitted = {
            "learning_feedback",
            "learning_signal_config",
            "opportunity_movement",
            "projection_validation",
            "discovery.projection.signal_registry",
        }
        tree = _tree(APP / "learning_signals.py")
        app_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                app_imports.add(node.module)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("discovery") or node.module.startswith("app"):
                    app_imports.add(node.module)

        unexpected = app_imports - permitted
        assert not unexpected, (
            f"learning_signals reads from {sorted(unexpected)}. The learning "
            "signal set has exactly two sources — analyst decisions and A2 "
            "outcome measurements. Adding a third changes what the feature IS "
            "and belongs in the story, not in an import."
        )

    def test_the_store_reads_no_scoring_module(self):
        tree = _tree(APP / "learning_feedback.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "scorer" not in node.module, (
                    "the feedback store must not import a scorer — it records "
                    "decisions, it does not score"
                )


# --------------------------------------------------------------------------
# AC3 groundwork — no path to evidence, confidence or corroboration.
# --------------------------------------------------------------------------


class TestTheLayerCannotTouchEvidenceConfidenceOrCorroboration:
    @pytest.mark.parametrize("path", _module_paths(), ids=LEARNING_LAYER_MODULES)
    def test_no_learning_module_assigns_a_protected_field(self, path: Path):
        """AC3, enforced one level stronger than the AC states it.

        AC3 says adjustment must not modify evidence, confidence or
        corroboration. T1 ships no adjustment, so the guarantee available here is
        better: no module in the layer contains an assignment to any of those
        fields, so the capability does not exist to be misused later.
        """
        tree = _tree(path)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                name = None
                if isinstance(target, ast.Subscript) and isinstance(
                    target.slice, ast.Constant
                ):
                    name = target.slice.value
                elif isinstance(target, ast.Attribute):
                    name = target.attr
                if name in PROTECTED_FIELDS:
                    raise AssertionError(
                        f"{path.name} assigns to {name!r} at line {node.lineno}. "
                        "Learning adjusts ORDERING only — never a finding's "
                        "evidence, confidence or corroboration."
                    )

    @pytest.mark.parametrize("path", _module_paths(), ids=LEARNING_LAYER_MODULES)
    def test_no_learning_module_writes_to_the_opportunity_stores(self, path: Path):
        """No path to the finding records themselves."""
        source = path.read_text(encoding="utf-8")
        for hazard in ("run_kv_set", "upsert(", "kv_set("):
            assert hazard not in source, (
                f"{path.name} uses {hazard!r}. The learning layer must not write "
                "to opportunity storage: it observes decisions and outcomes, and "
                "T2 adjusts ordering at serve time. Rewriting a stored finding "
                "would make the base score unrecoverable, which AC1 forbids."
            )


# --------------------------------------------------------------------------
# AC6 groundwork — org scoping at the SQL layer.
# --------------------------------------------------------------------------


class TestOrgScopingIsInTheQuery:
    def test_every_feedback_query_filters_on_org_id(self):
        """Isolation has to hold in the WHERE clause or it does not hold.

        Filtering after the fact would still isolate — until someone adds a
        pagination limit above the filter and quietly truncates one org's rows
        away with another's.
        """
        tree = _tree(APP / "learning_feedback.py")
        consts = {
            t.id: n.value.value
            for n in tree.body
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }

        def literal(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return consts.get(node.id)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left, right = literal(node.left), literal(node.right)
                return None if left is None or right is None else left + right
            if isinstance(node, ast.JoinedStr):
                parts = []
                for value in node.values:
                    if isinstance(value, ast.Constant):
                        parts.append(str(value.value))
                    else:
                        parts.append("?")
                return "".join(parts)
            return None

        statements = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
            ):
                sql = literal(node.args[0])
                if sql:
                    statements.append(" ".join(sql.split()))

        selects = [s for s in statements if s.upper().startswith("SELECT")]
        assert selects, "no SELECT statements found — has the store moved?"
        for sql in selects:
            assert "org_id = %s" in sql, (
                f"a feedback SELECT is not org-scoped in SQL: {sql[:140]}"
            )

    def test_no_route_reads_an_org_id_from_the_request(self):
        source = (APP / "routes_learning_feedback.py").read_text(encoding="utf-8")
        assert "get_current_org_id()" in source
        for smell in ("orgId:", "org_id:", "body.orgId", "body.org_id"):
            assert smell not in source


# --------------------------------------------------------------------------
# The append-only guarantee.
# --------------------------------------------------------------------------


class TestTheDecisionRecordIsAppendOnly:
    def test_the_store_issues_no_update_or_delete(self):
        source = (APP / "learning_feedback.py").read_text(encoding="utf-8").upper()
        for verb in ("UPDATE OPPORTUNITY_FEEDBACK", "DELETE FROM OPPORTUNITY_FEEDBACK"):
            assert verb not in source, (
                f"{verb} in the feedback store: a decision record that can be "
                "rewritten cannot answer 'why was this ranked higher last month?'"
            )
        assert "ON CONFLICT" not in source, (
            "an ON CONFLICT clause implies a key that could collide; every "
            "decision is a new row with its own id, by design"
        )

    def test_the_store_exposes_no_mutation_function(self):
        from app import learning_feedback

        for name in learning_feedback.__all__:
            lowered = name.lower()
            for verb in ("update", "delete", "overwrite", "amend"):
                assert verb not in lowered, (
                    f"learning_feedback exposes {name!r}, which suggests a "
                    "mutation path an append-only record must not have"
                )

    def test_the_ddl_documents_the_production_revoke(self):
        ddl = (
            BACKEND / "database" / "models" / "opportunity_feedback.py"
        ).read_text(encoding="utf-8")
        assert "REVOKE UPDATE, DELETE ON opportunity_feedback" in ddl
        assert "GRANT INSERT, SELECT ON opportunity_feedback" in ddl


# --------------------------------------------------------------------------
# The guard on the guard.
# --------------------------------------------------------------------------


class TestTheGuardListIsComplete:
    def test_the_layer_module_list_is_complete(self):
        """A new learning module must be added to LEARNING_LAYER_MODULES.

        Without this, adding ``learning_adjustment.py`` in T2 would create a
        module that none of the guards above apply to — and it would be the
        module that most needs them.
        """
        on_disk = {
            p.name
            for p in APP.glob("*.py")
            if p.name.startswith("learning_") or p.name.startswith("routes_learning")
        }
        missing = on_disk - set(LEARNING_LAYER_MODULES)
        assert not missing, (
            f"{sorted(missing)} look like learning-layer modules but are not "
            "covered by the isolation guards. Add them to LEARNING_LAYER_MODULES."
        )

    def test_every_listed_module_exists(self):
        for path in _module_paths():
            assert path.exists(), f"{path.name} is listed but missing"
