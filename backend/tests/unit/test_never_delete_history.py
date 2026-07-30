"""2.0-C1 T4 (AT-829) — "never delete history", enforced and swept.

Parent-story criterion:

  **AC4** — No path in disable / rollback / remove deletes findings, evidence, or
  run records — *data-layer test attempting each*.

This file is the **build-breaking static half**. It scans production source and fails
CI if any module issues a ``DELETE``/``TRUNCATE`` against a protected history table,
so the problem surfaces in review rather than as an opaque privilege error in
production. Same spirit as ``test_no_env_credential_reads.py`` and
``test_model_gateway_no_bypass.py``.

The **behavioural half** — actually attempting a disable, a rollback, and a removal
and asserting that findings, evidence, and run records all survive — lives in
``tests/unit/test_never_delete_history_data_layer.py`` (DB-free) and
``tests/contract/test_pack_lifecycle_retention.py`` (over HTTP).

Nothing is enumerated by hand: the module list is discovered by walking the tree at
test time, and the protected-table set is imported from ``app.history_retention``, so
a new module or a newly protected table is swept without editing this file. The
default posture for an unlisted delete is FAIL.

Placed under ``tests/unit/`` rather than beside ``test_no_env_credential_reads.py`` in
``tests/contract/`` because it is pure source inspection with no database need — it
runs anywhere, including on a machine with no contract DB.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from app.history_retention import (
    DELETABLE_TABLE_REASONS,
    PROTECTED_TABLE_REASONS,
    PROTECTED_TABLES,
    find_delete_targets,
)

_BACKEND_DIR = Path(__file__).resolve().parents[2]

# Directories excluded from the production sweep, with the reason.
_EXCLUDED_DIRS = {
    ".venv",          # third-party packages
    "tests",          # test fixtures legitimately clean up their own rows
    "migrations",     # schema DDL — dropping a table on downgrade is a schema op
    "alembic",        # legacy migration dir
    "__pycache__",
    "node_modules",
}

# Files allowed to contain a DELETE against a PROTECTED table, with justification.
# Empty by design: there is currently no legitimate reason for one to exist. An entry
# here is a deliberate, reviewed exception — not a way to quiet the sweep.
_PROTECTED_DELETE_ALLOWLIST: Dict[str, str] = {}


def _production_python_files() -> List[Path]:
    """Every production Python source file under backend/, discovered at test time."""
    files: List[Path] = []
    for path in _BACKEND_DIR.rglob("*.py"):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _docstring_nodes(tree: ast.AST) -> set:
    """ids of the string nodes that are DOCSTRINGS, so prose can be skipped.

    SQL lives in ordinary string literals passed to ``cursor.execute``; prose about
    SQL lives in docstrings and comments. Scanning prose produces nonsense matches
    (``"a DELETE or TRUNCATE against any of these"`` parses "against" as a table), so
    the sweep looks at real string literals only. Comments never reach the AST, so
    they are excluded for free.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return docstrings


def _sql_string_literals(path: Path) -> List[Tuple[int, str]]:
    """(line_no, value) for every non-docstring string literal in a module."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
        return []
    if "DELETE" not in text.upper() and "TRUNCATE" not in text.upper():
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - defensive
        return []

    docstrings = _docstring_nodes(tree)
    literals: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            literals.append((getattr(node, "lineno", 0), node.value))
    return literals


def _delete_sites() -> List[Tuple[Path, int, str, str]]:
    """(file, line_no, table, literal) for every delete/truncate in production SQL."""
    sites: List[Tuple[Path, int, str, str]] = []
    for path in _production_python_files():
        for line_no, literal in _sql_string_literals(path):
            for table in find_delete_targets(literal):
                sites.append((path, line_no, table, " ".join(literal.split())[:120]))
    return sites


# ── The sweep ─────────────────────────────────────────────────────────────────


class TestNoProductionDeleteAgainstHistory:
    def test_the_sweep_actually_finds_source_files(self):
        # A guard on the guard: if discovery silently returned nothing, every
        # assertion below would pass vacuously.
        files = _production_python_files()
        assert len(files) > 100, f"sweep found only {len(files)} files — discovery broke"
        assert any(path.name == "db.py" for path in files)

    def test_the_sweep_actually_finds_delete_statements(self):
        # Likewise: the retrieval layer legitimately deletes, so a zero result would
        # mean the matcher stopped working rather than that the codebase is clean.
        tables = {table for _f, _l, table, _line in _delete_sites()}
        assert "retrieval_chunks" in tables, (
            "sweep found no known delete — the matcher is broken, not the codebase"
        )

    def test_no_production_code_deletes_from_a_protected_table(self):
        offenders = [
            (str(path.relative_to(_BACKEND_DIR)), line_no, table, line)
            for path, line_no, table, line in _delete_sites()
            if table in PROTECTED_TABLES
            and str(path.relative_to(_BACKEND_DIR)) not in _PROTECTED_DELETE_ALLOWLIST
        ]
        assert offenders == [], (
            "Production code deletes from a protected run-history table. Run history "
            "is never deleted (2.0-C1 AC4) — soft-delete, disable, or mark the record "
            f"instead. Offenders: {offenders}"
        )

    def test_every_delete_target_is_a_known_table(self):
        """Every table production code deletes from is classified.

        A delete against an UNCLASSIFIED table is a review gap: nobody has decided
        whether it holds history. Failing here forces the decision — add it to
        PROTECTED_TABLE_REASONS or DELETABLE_TABLE_REASONS with a justification.
        """
        known = set(PROTECTED_TABLES) | set(DELETABLE_TABLE_REASONS)
        unclassified = sorted(
            {
                f"{table} ({path.relative_to(_BACKEND_DIR)}:{line_no})"
                for path, line_no, table, _line in _delete_sites()
                if table not in known
            }
        )
        assert unclassified == [], (
            "Production code deletes from a table that is not classified as protected "
            "or deletable. Classify it in app/history_retention.py with a reason: "
            f"{unclassified}"
        )

    def test_delete_run_events_is_still_a_soft_delete(self):
        """``db.delete_run_events`` must remain an UPDATE despite its name.

        It is the one function in the codebase whose name suggests it removes run
        history. Its body is pinned here because a future edit turning it into a real
        DELETE would be both a privilege error in production and a silent loss of the
        run event log in any environment where the REVOKE was not applied.
        """
        db_path = _BACKEND_DIR / "app" / "db.py"
        tree = ast.parse(db_path.read_text(encoding="utf-8"))
        func = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "delete_run_events"
            ),
            None,
        )
        assert func is not None, "delete_run_events not found in app/db.py"

        # Inspect the function's SQL literals only — its docstring legitimately
        # *mentions* DELETE/TRUNCATE while explaining why it must not perform one.
        docstrings = _docstring_nodes(tree)
        literals = [
            node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        sql = "\n".join(literals)
        assert "UPDATE run_events SET is_deleted" in sql
        assert find_delete_targets(sql) == [], (
            "delete_run_events performs a hard delete — it must stay a soft delete "
            "(UPDATE run_events SET is_deleted = TRUE)"
        )


# ── The protected set stays in step with the DDL ───────────────────────────────


class TestProvisioningRevokesEveryProtectedTable:
    """``provision.sql`` mirrors the app-layer protected set.

    ``provision.sql`` is what actually creates and privileges tables in a real
    deployment (``db.init_tables()`` is a no-op), so a table protected in Python but
    missing from the REVOKE block would have no database-level enforcement at all.
    """

    @pytest.fixture
    def provision_sql(self) -> str:
        return (
            _BACKEND_DIR / "database" / "provision" / "provision.sql"
        ).read_text(encoding="utf-8")

    def test_revoke_block_exists(self, provision_sql):
        assert "REVOKE DELETE, TRUNCATE ON TABLE" in provision_sql

    @pytest.mark.parametrize("table", sorted(PROTECTED_TABLES))
    def test_every_protected_table_is_in_the_revoke_block(
        self, provision_sql, table
    ):
        revoke_section = provision_sql.split("REVOKE DELETE, TRUNCATE")[0]
        # The table must appear in the protected_tables array that precedes the
        # REVOKE call in the same DO block.
        assert f"'{table}'" in revoke_section, (
            f"protected table {table!r} is missing from provision.sql's "
            f"protected_tables array — it would have no DB-level enforcement"
        )

    def test_revoke_runs_after_the_grant_all(self, provision_sql):
        # Ordering is load-bearing: GRANT ALL PRIVILEGES includes DELETE, so a REVOKE
        # placed before it would be undone.
        grant_at = provision_sql.find("GRANT ALL PRIVILEGES ON ALL TABLES")
        revoke_at = provision_sql.find("REVOKE DELETE, TRUNCATE ON TABLE")
        assert grant_at != -1 and revoke_at != -1
        assert revoke_at > grant_at, (
            "the REVOKE block must come AFTER GRANT ALL PRIVILEGES, or the grant "
            "hands DELETE straight back"
        )

    def test_no_deletable_table_is_revoked(self, provision_sql):
        # Revoking these would break R18-B2 retrieval freshness and graph pruning.
        revoke_section = provision_sql.split("REVOKE DELETE, TRUNCATE")[0]
        block = revoke_section[revoke_section.rfind("protected_tables text[]"):]
        for table in DELETABLE_TABLE_REASONS:
            assert f"'{table}'" not in block, (
                f"{table!r} is legitimately deletable and must NOT be revoked"
            )

    def test_ddl_generator_matches_the_protected_set(self):
        from database.models.history_retention import ALL_HISTORY_RETENTION_DDL

        ddl = "\n".join(ALL_HISTORY_RETENTION_DDL)
        assert "REVOKE DELETE, TRUNCATE" in ddl
        for table in PROTECTED_TABLES:
            assert f"'{table}'" in ddl, f"{table!r} missing from the migration DDL"
        for table in DELETABLE_TABLE_REASONS:
            assert f"'{table}'" not in ddl

    def test_migration_0033_applies_the_revoke(self):
        migration = (
            _BACKEND_DIR / "migrations" / "versions" / "0033_revoke_history_deletion.py"
        ).read_text(encoding="utf-8")
        assert 'down_revision: Union[str, None] = "0032"' in migration
        assert "ALL_HISTORY_RETENTION_DDL" in migration


class TestProtectedSetIsCoherent:
    def test_protected_and_deletable_do_not_overlap(self):
        assert set(PROTECTED_TABLES) & set(DELETABLE_TABLE_REASONS) == set()

    def test_every_protected_table_has_a_reason(self):
        for table, reason in PROTECTED_TABLE_REASONS.items():
            assert reason.strip(), table

    def test_every_deletable_table_has_a_justification(self):
        for table, reason in DELETABLE_TABLE_REASONS.items():
            assert reason.strip(), table

    def test_the_findings_evidence_and_run_tables_are_all_protected(self):
        # AC4 names three things explicitly. Pin that each has a home in the set, so
        # a future refactor cannot quietly drop one.
        assert "kv" in PROTECTED_TABLES              # findings + evidence artifacts
        assert "opportunity_instances" in PROTECTED_TABLES   # per-instance findings
        assert "runs" in PROTECTED_TABLES             # run records
        assert "run_events" in PROTECTED_TABLES       # run event log
        assert "pack_state_history" in PROTECTED_TABLES      # lifecycle audit trail
