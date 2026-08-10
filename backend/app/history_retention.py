"""Run-history retention — 2.0-C1 T4 (AT-829) "never delete history".

The rule this module owns (parent story AC4):

    No path in disable / rollback / remove deletes findings, evidence, or run
    records — enforced at the data layer, tested.

Single source of truth
----------------------
:data:`PROTECTED_TABLES` is the ONE declaration of which tables hold run history.
Three independent enforcement layers read it, so the guarantee does not rest on
any single mechanism:

1. **Database privileges (the real data-layer enforcement).** ``provision.sql`` and
   migration ``0033`` REVOKE ``DELETE, TRUNCATE`` on every protected table from the
   application login role. A bug, a rogue query, or a future code path physically
   cannot remove a finding — the database refuses it.
2. **A build-breaking static sweep.** ``tests/contract/test_never_delete_history.py``
   scans production source for ``DELETE``/``TRUNCATE`` against a protected table and
   fails CI, so the problem surfaces before deploy rather than as a privilege error
   in production.
3. **This module's runtime guard.** :func:`guard_delete` /
   :func:`assert_no_history_deletion` are the seam any code that *must* touch
   history-adjacent SQL calls, so an intentional new path has to state its case
   explicitly rather than quietly acquiring delete semantics.

Why these tables and not others
-------------------------------
Protected = the *record of what the platform found and did*. Notably NOT protected:

* ``retrieval_chunks`` / ``retrieval_refresh_queue`` — a DERIVED vector index and its
  work queue. R18-B2 freshness deliberately purges chunks when a source artifact
  changes or is deleted; blocking that would break retrieval correctness. Deleting a
  chunk loses no history: the finding, its evidence, and the evidence pointer that
  resolves to the source all survive, and the chunk can be re-embedded.
* ``entity_relationships`` — cross-run graph state, which ``relationship_mapper``
  legitimately prunes when a relationship no longer holds. The graph is a current
  view, not a historical record (this is why ``OppEnrichment.relationships`` is
  documented as reading live rather than from a run-scoped artifact).

Soft delete is not deletion
---------------------------
``db.delete_run_events`` marks ``run_events.is_deleted`` and is deliberately an
UPDATE: ``insert_run_events`` re-activates a rewritten ``(run_id, seq)`` and
``get_run_events`` filters the flag, so rewriting a shrunk event list drops stale
rows from READS while the underlying rows remain. That pattern is compatible with the
REVOKE and is the shape any future "removal" should take.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, FrozenSet, Iterable, Optional

logger = logging.getLogger(__name__)


# ── The protected set ─────────────────────────────────────────────────────────

#: Table → why it is protected. Keys are the authoritative protected-table set;
#: the values are used verbatim in refusal messages and in the provisioning docs.
PROTECTED_TABLE_REASONS: Dict[str, str] = {
    "runs": (
        "run records — the run itself, its pack ids, pack versions, and the "
        "configuration it executed under"
    ),
    "run_events": (
        "the run event log (soft-deleted via is_deleted, never removed)"
    ),
    "kv": (
        "run-scoped artifacts — findings (opps:{run_id}), evidence, clusters, "
        "roadmap, executive report, and enrichment"
    ),
    "opportunity_instances": (
        "per-instance opportunity records carrying the pack id and pack version "
        "stamp that R16-B1 §4 provenance depends on"
    ),
    "opportunity_lifecycle": (
        "the current customer-recorded A2 action state and deployment date; "
        "resetting it by deletion would detach later measurements from the action"
    ),
    "opportunity_lifecycle_history": (
        "the append-only A2 record of every action, dismissal, reopen, monitoring, "
        "measurement, and stalled transition"
    ),
    "opportunity_baselines": (
        "the immutable A2 measurement basis frozen when an opportunity is first "
        "found; deleting it makes the outcome impossible to reproduce"
    ),
    "opportunity_movements": (
        "the stored A2 before/after comparisons, their source run ids, labelled "
        "caveats, and A1 projection-validation results"
    ),
    "opportunity_feedback": (
        "the append-only A3 analyst decision record that explains learned ordering"
    ),
    "ranking_adjustments": (
        "the current bounded A3 ranking state; reset is an audited update to "
        "neutral, never deletion"
    ),
    "ranking_adjustment_history": (
        "the append-only A3 history behind Owner inspection and reset audit"
    ),
    "pack_state_history": (
        "the append-only pack lifecycle audit trail — disable/enable and "
        "rollback/restore transitions (2.0-C1 T2/T3)"
    ),
    "pack_certification_reviews": (
        "the append-only pack certification review trail — who reviewed which "
        "pack version, against which criteria, with what decision (2.0-C2 T2). "
        "A certification decision that can be deleted is not auditable, which is "
        "what 2.0-C2 AC5 requires it to be"
    ),
}

#: The protected tables. A DELETE or TRUNCATE against any of these is refused.
PROTECTED_TABLES: FrozenSet[str] = frozenset(PROTECTED_TABLE_REASONS)

#: Tables that legitimately support deletion, with the justification. Listed
#: EXPLICITLY rather than as "everything not protected" so a reviewer can see that
#: each exemption was a decision, and so the static sweep can tell an intended
#: delete from an accidental one.
DELETABLE_TABLE_REASONS: Dict[str, str] = {
    "retrieval_chunks": (
        "derived vector index — R18-B2 freshness purges chunks when a source "
        "artifact changes or is deleted; re-embeddable, loses no history"
    ),
    "retrieval_refresh_queue": (
        "transient work queue for the refresh worker — not a historical record"
    ),
    "entity_relationships": (
        "cross-run graph state, pruned by relationship_mapper when a relationship "
        "no longer holds; a current view rather than a historical record"
    ),
}

#: The three lifecycle operations AC4 names. Used to label refusals and tests.
OPERATION_DISABLE = "disable"
OPERATION_ROLLBACK = "rollback"
OPERATION_REMOVE = "remove"
LIFECYCLE_OPERATIONS: FrozenSet[str] = frozenset(
    {OPERATION_DISABLE, OPERATION_ROLLBACK, OPERATION_REMOVE}
)


class HistoryDeletionRefused(RuntimeError):
    """Raised when a code path attempts to delete protected run history.

    ``str(exc)`` names the table, why it is protected, and the operation that
    attempted it, so the refusal is actionable rather than a bare permission error.
    """

    def __init__(
        self,
        table: str,
        *,
        operation: Optional[str] = None,
        statement: Optional[str] = None,
    ) -> None:
        self.table = table
        self.operation = operation
        self.statement = statement
        reason = PROTECTED_TABLE_REASONS.get(table, "protected run history")
        attempted = f" attempted by the {operation!r} path" if operation else ""
        super().__init__(
            f"Refusing to delete from {table!r}{attempted}: it holds {reason}. "
            f"Run history is never deleted (2.0-C1 AC4) — disable, roll back, or "
            f"mark the record instead."
        )


# ── Runtime guard ─────────────────────────────────────────────────────────────


def is_protected_table(table: str) -> bool:
    """True when the table holds run history that must never be deleted."""
    return _normalise_table(table) in PROTECTED_TABLES


def protection_reason(table: str) -> Optional[str]:
    """Why a table is protected, or ``None`` if it is not."""
    return PROTECTED_TABLE_REASONS.get(_normalise_table(table))


def guard_delete(table: str, *, operation: Optional[str] = None) -> None:
    """Refuse a delete against a protected table.

    Call this before any statement that would remove rows from a table that might
    hold run history. Raises :class:`HistoryDeletionRefused` for a protected table
    and returns silently otherwise.
    """
    normalised = _normalise_table(table)
    if normalised in PROTECTED_TABLES:
        logger.error(
            "Refused delete against protected history table %r (operation=%s)",
            normalised,
            operation,
        )
        raise HistoryDeletionRefused(normalised, operation=operation)


# Matches ``DELETE FROM <table>`` and ``TRUNCATE [TABLE] <table>``, tolerating
# quoting, schema qualification, and arbitrary whitespace/newlines. Deliberately
# permissive about what follows the table name — the goal is to CATCH candidate
# statements, and a false positive is a cheap, visible failure while a false
# negative would silently lose history.
_DELETE_RE = re.compile(
    r"""
    \b(?P<verb>DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+
    (?:"?[A-Za-z_][A-Za-z0-9_]*"?\s*\.\s*)?   # optional schema qualifier
    "?(?P<table>[A-Za-z_][A-Za-z0-9_]*)"?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Tokens that can follow the verb in valid SQL but are never a table name. Without
# this, a privilege statement like ``REVOKE DELETE, TRUNCATE ON TABLE "kv"`` parses
# "ON" as the target, and ``TRUNCATE ... CASCADE`` variants parse the option word.
# Deliberately only real SQL keywords — not English words — so the exclusion cannot
# quietly swallow a table whose name happens to read like prose.
_NOT_A_TABLE_NAME = frozenset(
    {"on", "table", "from", "to", "cascade", "restart", "continue", "only", "identity"}
)


def find_delete_targets(sql: str) -> list:
    """Every table a SQL string appears to DELETE from or TRUNCATE.

    Shared by the runtime guard and the static CI sweep so the two can never
    disagree about what counts as a delete. Returns lower-cased table names, in the
    order they appear, skipping SQL keywords that cannot be a table.
    """
    if not sql:
        return []
    return [
        table
        for table in (
            match.group("table").lower() for match in _DELETE_RE.finditer(sql)
        )
        if table not in _NOT_A_TABLE_NAME
    ]


def assert_no_history_deletion(
    sql: str, *, operation: Optional[str] = None
) -> None:
    """Refuse a SQL statement that deletes from a protected table.

    Statement-level counterpart to :func:`guard_delete`, for a caller holding raw
    SQL rather than a table name.
    """
    for table in find_delete_targets(sql):
        if table in PROTECTED_TABLES:
            logger.error(
                "Refused statement deleting protected history table %r "
                "(operation=%s)",
                table,
                operation,
            )
            raise HistoryDeletionRefused(
                table, operation=operation, statement=sql
            )


def revoke_statements(roles: Iterable[str]) -> list:
    """``REVOKE DELETE, TRUNCATE`` statements for every protected table.

    The single generator of the privilege enforcement, so ``provision.sql``, the
    migration, and the tests all describe the same thing. Table and role names come
    from this module's own constants (never user input), and each is emitted quoted.
    """
    statements = []
    for role in roles:
        for table in sorted(PROTECTED_TABLES):
            statements.append(
                f'REVOKE DELETE, TRUNCATE ON TABLE "{table}" FROM "{role}"'
            )
    return statements


def _normalise_table(table: str) -> str:
    """Strip quoting/schema qualification and lower-case a table reference."""
    text = str(table or "").strip().strip('"').strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.strip('"').lower()
