"""2.0-A2 T2 — structural proof that no application path can mutate a baseline.

The definition of done requires "a data-layer test proving no application path can
mutate it". A behavioural test can only prove the paths it happens to call; these
tests read the source and fail the build if a mutating path is ever *added*.

Why this is worth enforcing structurally: the failure is silent and delayed. If a
later run could restate what a finding was born with, nothing in any output would
look wrong — the outcome claim would simply be measured against a moving target,
and it would take a customer audit to notice.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from app import opportunity_baseline

BACKEND = Path(__file__).resolve().parents[2]
STORE = BACKEND / "app" / "opportunity_baseline.py"
DDL = BACKEND / "database" / "models" / "opportunity_baselines.py"
ROUTES = BACKEND / "app" / "routes_opportunity_baseline.py"

TABLES = ("opportunity_baselines",)


def _sql_statements(path: Path) -> list[str]:
    """Every SQL string passed to ``cur.execute()`` in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = {
        t.id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }

    def lit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = lit(node.left), lit(node.right)
            return None if left is None or right is None else left + right
        return None

    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            sql = lit(node.args[0])
            if sql:
                out.append(" ".join(sql.split()))
    return out


class TestNoMutatingSqlExists:
    def test_the_store_issues_no_update_against_the_baseline_table(self):
        for sql in _sql_statements(STORE):
            upper = sql.upper()
            for table in TABLES:
                if table.upper() in upper:
                    assert not re.search(rf"UPDATE\s+{table}", sql, re.I), (
                        f"the baseline store must never UPDATE {table}: {sql[:120]}"
                    )

    def test_the_store_issues_no_delete_against_the_baseline_table(self):
        for sql in _sql_statements(STORE):
            for table in TABLES:
                assert not re.search(rf"DELETE\s+FROM\s+{table}", sql, re.I), (
                    f"the baseline store must never DELETE from {table}: {sql[:120]}"
                )

    def test_the_only_write_is_an_insert_with_do_nothing(self):
        """The clause that IS the immutability guarantee at statement level."""
        writes = [
            s
            for s in _sql_statements(STORE)
            if s.upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert len(writes) == 1, f"expected exactly one write statement, got {writes}"

        insert = writes[0]
        assert insert.upper().startswith("INSERT INTO OPPORTUNITY_BASELINES")
        assert "ON CONFLICT" in insert.upper()
        assert "DO NOTHING" in insert.upper(), (
            "the conflict clause must be DO NOTHING — DO UPDATE would make the "
            "artifact mutable, which is the one thing it must never be"
        )
        assert "DO UPDATE" not in insert.upper()

    def test_no_truncate_or_drop_in_the_store(self):
        source = STORE.read_text(encoding="utf-8").upper()
        for verb in ("TRUNCATE", "DROP TABLE", "ALTER TABLE"):
            assert verb not in source, f"{verb} must not appear in the store"


class TestNoMutatingFunctionsExposed:
    def test_the_store_exposes_no_update_or_delete_function(self):
        names = [n for n in dir(opportunity_baseline) if not n.startswith("_")]
        for name in names:
            lowered = name.lower()
            for verb in ("update", "delete", "modify", "overwrite", "set_", "edit"):
                assert verb not in lowered, (
                    f"opportunity_baseline exposes {name!r}, which suggests a "
                    "mutation path the artifact must not have"
                )

    def test_the_public_api_is_capture_and_read_only(self):
        assert set(opportunity_baseline.__all__) == {
            "BaselineCaptureError",
            "capture_baseline",
            "capture_baselines_for_run",
            "ensure_opportunity_baseline_table",
            "get_baseline",
            "get_baselines_for_run",
            "has_baseline",
            "list_baselines",
        }

    def test_capture_baseline_reports_whether_it_created_anything(self):
        """A no-op must be distinguishable from a fresh freeze.

        Silently returning the same shape either way would hide the case where a
        caller thought it had written a basis and had not.
        """
        source = inspect.getsource(opportunity_baseline.capture_baseline)
        assert '"created"' in source
        assert "rowcount" in source, (
            "creation must be determined from the DB's own report, not inferred"
        )


class TestTheApiExposesNoWriteVerb:
    def test_the_baseline_routes_are_read_only(self):
        source = ROUTES.read_text(encoding="utf-8")
        for verb in ("@router.post", "@router.patch", "@router.put", "@router.delete"):
            assert verb not in source, (
                f"{verb} in the baseline API would hand a client the one capability "
                "the artifact exists to deny"
            )
        assert "@router.get" in source

    def test_every_baseline_route_is_analyst_gated(self):
        """Checked per route via the AST, not by counting strings.

        String counting is fooled by the module docstring, which mentions the
        gate — and a test that can be fooled by a comment is not a gate test.
        """
        tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
        routes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "router"
                ):
                    routes.append((node.name, ast.dump(dec)))

        assert routes, "no @router routes found — has the module moved?"
        for name, decorator in routes:
            assert "require_role" in decorator and "analyst" in decorator, (
                f"route {name!r} is not analyst-gated"
            )

    def test_no_route_reads_an_org_id_from_the_request(self):
        source = ROUTES.read_text(encoding="utf-8")
        assert "get_current_org_id()" in source
        for smell in ("orgId:", "org_id:", "body.orgId", "body.org_id"):
            assert smell not in source


class TestDdlDeclaresTheImmutabilityPosture:
    def test_the_table_is_keyed_on_org_and_identity(self):
        """The primary key is what makes a rewrite a conflict rather than an edit."""
        ddl = DDL.read_text(encoding="utf-8")
        assert "PRIMARY KEY (org_id, opportunity_identity)" in ddl

    def test_the_ddl_documents_the_revoke_grants(self):
        """Production removes the capability entirely, not just the code path."""
        ddl = DDL.read_text(encoding="utf-8")
        assert "REVOKE UPDATE, DELETE ON opportunity_baselines" in ddl
        assert "GRANT INSERT, SELECT ON opportunity_baselines" in ddl

    def test_the_ddl_contains_no_update_trigger_or_rule(self):
        ddl = DDL.read_text(encoding="utf-8").upper()
        assert "CREATE RULE" not in ddl
        assert "CREATE TRIGGER" not in ddl


class TestTheArtifactIsNotStoredInTheRunKvBlob:
    """The hazard the subtask calls out by name.

    ``opps`` / ``evidence`` are rewritten wholesale by materialization and by
    replay — ``replay.py`` resets ``decision`` on replay. A baseline a replay can
    rewrite is not a baseline.
    """

    def test_the_store_never_touches_run_scoped_kv(self):
        source = STORE.read_text(encoding="utf-8")
        for hazard in ("run_kv_set", "run_kv_get", 'kv_set("opps"', "kv_set(f\"opps"):
            assert hazard not in source, (
                f"{hazard!r} in the baseline store would put the artifact in a blob "
                "that materialization and replay rewrite wholesale"
            )

    def test_replay_does_not_touch_the_baseline_table(self):
        replay = (BACKEND / "app" / "replay.py").read_text(encoding="utf-8")
        assert "opportunity_baseline" not in replay
        assert "opportunity_baselines" not in replay


class TestPipelineCaptureIsWriteOnceAndNonBlocking:
    def test_both_materialization_paths_capture_baselines(self):
        for module in ("materialize_t2.py", "routes_sprint4_t1.py"):
            text = (BACKEND / "app" / module).read_text(encoding="utf-8")
            assert "capture_baselines_for_run" in text, (
                f"{module} must freeze a basis for the findings it creates"
            )

    def test_neither_path_can_rewrite_an_existing_baseline(self):
        """The pipeline calls only the write-once entry point."""
        for module in ("materialize_t2.py", "routes_sprint4_t1.py"):
            text = (BACKEND / "app" / module).read_text(encoding="utf-8")
            for forbidden in ("update_baseline", "delete_baseline", "overwrite_baseline"):
                assert forbidden not in text

    def test_capture_is_wrapped_non_blocking_in_both_paths(self):
        """A baseline failure must never fail a discovery run."""
        for module in ("materialize_t2.py", "routes_sprint4_t1.py"):
            text = (BACKEND / "app" / module).read_text(encoding="utf-8")
            idx = text.index("capture_baselines_for_run")
            window = text[max(0, idx - 900) : idx + 900]
            assert "try:" in window and "except Exception" in window, (
                f"{module}'s baseline capture is not wrapped non-blocking"
            )


class TestBackfillIsOutOfScope:
    def test_there_is_no_backfill_helper(self):
        """Explicitly out of scope.

        A finding created before this shipped has no baseline and is therefore
        never measurable — the honest outcome, not a gap to paper over with a
        reconstructed basis.
        """
        names = [n.lower() for n in dir(opportunity_baseline)]
        for name in names:
            assert "backfill" not in name
        source = STORE.read_text(encoding="utf-8").lower()
        assert "backfill" not in source or "out of scope" in source
