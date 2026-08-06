"""
2.0-D4 T2 — audit_log immutability (D4-AC2).

AC2: "Audit records cannot be updated or deleted through any application path
(data-layer test)."

That has **two halves**, and they catch different failures. A test that only did the
first would pass on a database where the application role can freely delete audit
rows, which is precisely the state this branch was in:

  1. **Source sweep** — the audit store contains no UPDATE/DELETE statement against
     the table. Catches a future code change. Follows the precedent of
     ``tests/unit/test_opportunity_baseline_immutability.py``.
  2. **Deployed privilege** — the role the application actually connects as does not
     hold UPDATE/DELETE/TRUNCATE. Catches a provisioning script that never ran. That
     half needs a live database, so it lives in
     ``tests/contract/test_audit_log_privileges.py`` where the migrated test DB is
     guaranteed; running it here would have skipped for want of a DATABASE_URL and
     proven nothing.

What the second half found: ``database/models/audit_log.py`` had documented
``REVOKE UPDATE, DELETE ON audit_log FROM app_user`` since AT-82, and it had never
been applied — ``provision.sql`` had no REVOKE for the table at all and the
application role held UPDATE, DELETE **and** TRUNCATE. Migration 0038 applies it.

The ownership caveat is reported, not hidden
--------------------------------------------
In PostgreSQL a table's OWNER can re-grant itself anything, so a REVOKE against the
owning role is advisory. Where the application role owns ``audit_log`` the privilege
test cannot prove immutability, so it says so loudly instead of passing and implying
protection that is not there. See ``docs/audit_export_and_retention.md`` §3.3.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]

#: Modules that may touch audit_log at all. Anything else writing to the table is
#: itself a finding, which `test_only_the_audit_module_writes_to_audit_log` checks.
AUDIT_STORE_FILES = (
    BACKEND / "app" / "middleware" / "audit.py",
    BACKEND / "app" / "audit_export.py",
)

TABLE = "audit_log"


def _sql_literals(path: pathlib.Path):
    """Every string constant in a module that looks like SQL touching the table."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if TABLE in text.lower():
                out.append(" ".join(text.split()))
    return out


# ── half 1: the source sweep ───────────────────────────────────────────────────


class TestNoMutatingStatementInSource:

    @pytest.mark.parametrize("path", AUDIT_STORE_FILES, ids=lambda p: p.name)
    def test_no_update_against_audit_log(self, path):
        for sql in _sql_literals(path):
            assert not re.search(rf"UPDATE\s+{TABLE}", sql, re.I), (
                f"{path.name} must never UPDATE {TABLE}: {sql[:120]}"
            )

    @pytest.mark.parametrize("path", AUDIT_STORE_FILES, ids=lambda p: p.name)
    def test_no_delete_against_audit_log(self, path):
        for sql in _sql_literals(path):
            assert not re.search(rf"DELETE\s+FROM\s+{TABLE}", sql, re.I), (
                f"{path.name} must never DELETE from {TABLE}: {sql[:120]}"
            )

    @pytest.mark.parametrize("path", AUDIT_STORE_FILES, ids=lambda p: p.name)
    def test_no_truncate_against_audit_log(self, path):
        for sql in _sql_literals(path):
            assert not re.search(rf"TRUNCATE\s+(TABLE\s+)?{TABLE}", sql, re.I), (
                f"{path.name} must never TRUNCATE {TABLE}: {sql[:120]}"
            )

    def test_the_only_write_is_an_insert(self):
        """The audit module's sole statement against the table is an INSERT."""
        writes = [
            sql for sql in _sql_literals(AUDIT_STORE_FILES[0])
            if sql.upper().lstrip().startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
        ]
        assert writes, "expected to find the INSERT statement"
        for sql in writes:
            assert sql.upper().lstrip().startswith("INSERT"), sql

    def test_the_export_only_reads(self):
        """The export is a disclosure, not a mutation: SELECT only."""
        for sql in _sql_literals(AUDIT_STORE_FILES[1]):
            if TABLE in sql.lower() and any(
                sql.upper().lstrip().startswith(v) for v in ("INSERT", "UPDATE", "DELETE")
            ):
                pytest.fail(f"audit_export must only SELECT: {sql[:120]}")

    def test_only_the_audit_module_writes_to_audit_log(self):
        """No other module may issue SQL against the table.

        A second writer would be a second place immutability could be broken, and
        this sweep would not be looking at it.
        """
        offenders = []
        for path in sorted((BACKEND / "app").rglob("*.py")):
            if path in AUDIT_STORE_FILES:
                continue
            try:
                literals = _sql_literals(path)
            except SyntaxError:
                continue
            for sql in literals:
                upper = sql.upper()
                if re.search(rf"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE)\s+(TABLE\s+)?{TABLE}", upper):
                    offenders.append(f"{path.name}: {sql[:80]}")
        assert not offenders, (
            "audit_log must be written through app/middleware/audit.py only: "
            f"{offenders}"
        )


# ── retention: documented, and deliberately not in the application ─────────────


class TestRetentionIsDocumentedAndOutsideTheApplication:
    """An append-only table with an in-application retention policy is a
    contradiction. The deletion path is outside the application by design, and that
    design has to be written down or the alternative reading is that the table grows
    forever."""

    def test_the_retention_design_is_documented(self):
        doc = (BACKEND.parent / "docs" / "audit_export_and_retention.md").read_text(
            encoding="utf-8"
        )
        # how long, enforced by what, run as whom — the three questions
        assert "AUDIT_RETENTION_DAYS" in doc
        assert "audit_retention" in doc
        for phrase in ("How long", "Enforced by what", "Run as whom"):
            assert phrase in doc, f"retention doc must answer: {phrase}"

    def test_no_application_module_deletes_audit_rows(self):
        """The design claim, enforced: there is no in-application deletion path."""
        offenders = []
        for path in sorted((BACKEND / "app").rglob("*.py")):
            try:
                literals = _sql_literals(path)
            except SyntaxError:
                continue
            for sql in literals:
                if re.search(rf"DELETE\s+FROM\s+{TABLE}", sql, re.I):
                    offenders.append(path.name)
        assert not offenders, (
            "retention deletion must stay outside the application "
            f"(found a DELETE in {offenders})"
        )
