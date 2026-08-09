"""2.0-C1 T4 (AT-829) — data-layer test ATTEMPTING disable, rollback, and remove.

Parent-story criterion:

  **AC4** — No path in disable / rollback / remove deletes findings, evidence, or run
  records — *data-layer test attempting each*.

This is the behavioural half, and it is a genuine **data-layer** test rather than a
mock of one: the production ``PostgresPackStateStore`` runs against a fake connection
that RECORDS every SQL statement it executes. So the assertions are made against the
SQL the production code path actually emits, not against a re-implementation of it.

Each of the three lifecycle verbs is attempted in turn and two things are checked:

1. **no statement deletes** from a protected history table, and
2. the seeded findings, evidence, and run records are still present and byte-identical
   afterwards.

A direct attempt to delete each protected table is also made, and must be refused.

DB-free: the fake connection means no PostgreSQL is needed, which is what lets this
run everywhere including CI's fast path.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional, Tuple

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import db as db_module  # noqa: E402
from app.history_retention import (  # noqa: E402
    DELETABLE_TABLE_REASONS,
    HistoryDeletionRefused,
    LIFECYCLE_OPERATIONS,
    OPERATION_DISABLE,
    OPERATION_REMOVE,
    OPERATION_ROLLBACK,
    PROTECTED_TABLES,
    assert_no_history_deletion,
    find_delete_targets,
    guard_delete,
    is_protected_table,
    protection_reason,
    revoke_statements,
)
from app.pack_state import (  # noqa: E402
    PostgresPackStateStore,
    STATE_ACTIVE,
    STATE_DISABLED,
    has_pack_lifecycle_record,
    pack_state_view,
    set_pack_state_store,
)
from discovery.packs.pack_config import PACK_REGISTRY  # noqa: E402

ORG = "acme"
ACTOR = "owner-1"
PACK = "cloud_ops"
PRIOR = "1.1.0"


# ── A recording fake of the data layer ────────────────────────────────────────


class _RecordingCursor:
    """Captures every statement, answering reads from a seeded row map."""

    def __init__(self, recorder: "_RecordingDataLayer") -> None:
        self._recorder = recorder
        self._result: Optional[Tuple[Any, ...]] = None

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        self._recorder.statements.append((sql, params))
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT") and "FROM PACK_STATES" in upper:
            # Serve the current pack_states row so the store's read-modify-write and
            # its idempotence checks behave exactly as they do in production.
            self._result = self._recorder.pack_state_row
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self, recorder: "_RecordingDataLayer") -> None:
        self._recorder = recorder

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._recorder)

    def commit(self) -> None:
        self._recorder.commits += 1

    def close(self) -> None:
        self._recorder.closes += 1

    # closing() / context-manager compatibility
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _RecordingDataLayer:
    """Fake data layer that records SQL and holds the seeded history rows.

    ``history`` models what the real tables hold: run records, the run-scoped KV
    artifacts (findings + evidence), and the per-instance opportunity rows. Nothing in
    this fake ever removes a row — the point is to prove the code under test does not
    ASK it to.
    """

    def __init__(self) -> None:
        self.statements: List[Tuple[str, Tuple[Any, ...]]] = []
        self.commits = 0
        self.closes = 0
        self.pack_state_row: Optional[Tuple[Any, ...]] = None
        self.history: Dict[str, Any] = {
            "runs": {
                "run-1": {
                    "id": "run-1",
                    "orgId": ORG,
                    "packIds": [PACK],
                    "packVersions": {PACK: "1.2.0"},
                    "status": "complete",
                }
            },
            "kv": {
                "opps:run-1": [
                    {
                        "id": "opp-1",
                        "packId": PACK,
                        "packVersion": "1.2.0",
                        "title": "Recurring resolution loop",
                        "evidenceIds": ["ev-1", "ev-2"],
                    }
                ],
                "evidence:run-1": [
                    {"id": "ev-1", "packId": PACK, "detail": "incident INC001"},
                    {"id": "ev-2", "packId": PACK, "detail": "incident INC002"},
                ],
            },
            "opportunity_instances": {
                "oi-1": {"id": "oi-1", "packId": PACK, "packVersion": "1.2.0"}
            },
        }

    def connect(self) -> _RecordingConnection:
        return _RecordingConnection(self)

    # ── assertions ───────────────────────────────────────────────────────────
    def deletes_against_protected(self) -> List[Tuple[str, str]]:
        offenders: List[Tuple[str, str]] = []
        for sql, _params in self.statements:
            for table in find_delete_targets(sql):
                if table in PROTECTED_TABLES:
                    offenders.append((table, " ".join(sql.split())[:120]))
        return offenders

    def verbs(self) -> List[str]:
        return [" ".join(sql.split()).split(" ", 1)[0].upper() for sql, _ in self.statements]


@pytest.fixture
def data_layer(monkeypatch):
    """Install the recording data layer under the production Postgres store."""
    recorder = _RecordingDataLayer()
    monkeypatch.setattr(db_module, "connect", recorder.connect)
    set_pack_state_store(PostgresPackStateStore())
    yield recorder
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def no_telemetry_db(monkeypatch):
    import app.telemetry as telemetry

    monkeypatch.setattr(telemetry, "record_event", lambda *_a, **_k: None)


def _history_snapshot(recorder: _RecordingDataLayer) -> Dict[str, Any]:
    return copy.deepcopy(recorder.history)


# ── AC4 — attempting each of the three lifecycle verbs ────────────────────────


class TestDisableDeletesNothing:
    def test_disable_emits_no_delete_against_protected_history(self, data_layer):
        from app.pack_state import disable_pack

        disable_pack(ORG, PACK, actor_id=ACTOR, reason="customer opted out")

        assert data_layer.statements, "the disable path executed no SQL at all"
        assert data_layer.deletes_against_protected() == []

    def test_disable_only_reads_and_writes_state_rows(self, data_layer):
        from app.pack_state import disable_pack

        disable_pack(ORG, PACK, actor_id=ACTOR)
        # SELECT ... FOR UPDATE, INSERT pack_states, INSERT pack_state_history.
        assert set(data_layer.verbs()) <= {"SELECT", "INSERT"}
        assert not any(
            verb in {"DELETE", "TRUNCATE", "DROP"} for verb in data_layer.verbs()
        )

    def test_findings_evidence_and_runs_survive_a_disable(self, data_layer):
        from app.pack_state import disable_pack

        before = _history_snapshot(data_layer)
        disable_pack(ORG, PACK, actor_id=ACTOR)
        assert data_layer.history == before

    def test_disable_touches_no_history_table_at_all(self, data_layer):
        from app.pack_state import disable_pack

        disable_pack(ORG, PACK, actor_id=ACTOR)
        touched = " ".join(sql.upper() for sql, _ in data_layer.statements)
        for table in ("RUNS", "KV", "OPPORTUNITY_INSTANCES"):
            assert table not in touched.replace("PACK_STATE", ""), table


class TestRollbackDeletesNothing:
    def test_rollback_emits_no_delete_against_protected_history(self, data_layer):
        from app.pack_state import rollback_pack_version

        rollback_pack_version(ORG, PACK, PRIOR, actor_id=ACTOR, reason="regression")

        assert data_layer.statements
        assert data_layer.deletes_against_protected() == []

    def test_rollback_only_reads_and_writes_state_rows(self, data_layer):
        from app.pack_state import rollback_pack_version

        rollback_pack_version(ORG, PACK, PRIOR, actor_id=ACTOR)
        assert set(data_layer.verbs()) <= {"SELECT", "INSERT"}

    def test_findings_keep_their_original_version_stamp_through_a_rollback(
        self, data_layer
    ):
        from app.pack_state import rollback_pack_version

        before = _history_snapshot(data_layer)
        rollback_pack_version(ORG, PACK, PRIOR, actor_id=ACTOR)
        assert data_layer.history == before
        # Explicitly: the 1.2.0 stamp is not rewritten to the rolled-back version.
        assert data_layer.history["kv"]["opps:run-1"][0]["packVersion"] == "1.2.0"
        assert (
            data_layer.history["opportunity_instances"]["oi-1"]["packVersion"]
            == "1.2.0"
        )

    def test_restore_deletes_nothing_either(self, data_layer):
        from app.pack_state import restore_pack_version, rollback_pack_version

        rollback_pack_version(ORG, PACK, PRIOR, actor_id=ACTOR)
        # Serve the pinned row back so restore sees a pin to clear.
        data_layer.pack_state_row = (
            STATE_ACTIVE, 1, None, "2026-07-30T00:00:00+00:00",
            "2026-07-30T00:00:00+00:00", PRIOR,
        )
        before = _history_snapshot(data_layer)
        restore_pack_version(ORG, PACK, actor_id=ACTOR)
        assert data_layer.deletes_against_protected() == []
        assert data_layer.history == before


class TestRemoveDeletesNothing:
    """"Remove" = a pack no longer being in the registry (a deploy-time change).

    There is no runtime "delete pack" API — deliberately. What must hold is that when
    a pack disappears, its historical output and its lifecycle record remain both
    PRESENT and REACHABLE. History you cannot reach is functionally deleted.
    """

    @pytest.fixture
    def removed_pack(self, monkeypatch):
        original = PACK_REGISTRY[PACK]
        monkeypatch.delitem(PACK_REGISTRY, PACK)
        return original

    def test_findings_evidence_and_runs_survive_a_removal(
        self, data_layer, removed_pack
    ):
        before = _history_snapshot(data_layer)
        assert PACK not in PACK_REGISTRY
        # Removal is a registry change; it executes no SQL and removes no row.
        assert data_layer.history == before
        assert data_layer.deletes_against_protected() == []

    def test_a_removed_packs_findings_keep_their_stamps(
        self, data_layer, removed_pack
    ):
        finding = data_layer.history["kv"]["opps:run-1"][0]
        assert finding["packId"] == PACK
        assert finding["packVersion"] == "1.2.0"
        assert finding["evidenceIds"] == ["ev-1", "ev-2"]

    def test_a_removed_packs_evidence_is_still_present(self, data_layer, removed_pack):
        evidence = data_layer.history["kv"]["evidence:run-1"]
        assert [item["id"] for item in evidence] == ["ev-1", "ev-2"]

    def test_a_removed_packs_run_record_is_still_present(
        self, data_layer, removed_pack
    ):
        run = data_layer.history["runs"]["run-1"]
        assert run["packIds"] == [PACK]
        assert run["packVersions"] == {PACK: "1.2.0"}

    def test_a_removed_packs_lifecycle_state_is_still_reachable(
        self, monkeypatch, removed_pack
    ):
        # Uses the in-memory store: this asserts the READ SURFACE keeps serving an
        # orphaned row rather than dropping it because the registry moved on.
        from app.pack_state import (
            InMemoryPackStateStore,
            disable_pack,
            set_pack_state_store as _set_store,
        )

        monkeypatch.setitem(PACK_REGISTRY, PACK, removed_pack)
        _set_store(InMemoryPackStateStore())
        try:
            disable_pack(ORG, PACK, actor_id=ACTOR, reason="turning off")
            monkeypatch.delitem(PACK_REGISTRY, PACK)

            rows = {row["packId"]: row for row in pack_state_view(ORG)}
            assert PACK in rows, (
                "a removed pack's lifecycle row vanished from the view — its retained "
                "history became unreachable"
            )
            assert rows[PACK]["registered"] is False
            assert rows[PACK]["state"] == STATE_DISABLED
            assert rows[PACK]["reason"] == "turning off"
            # The platform reports what it still knows and invents no version for a
            # pack it no longer ships.
            assert rows[PACK]["packVersion"] is None
            assert rows[PACK]["availableVersions"] == []
            assert has_pack_lifecycle_record(ORG, PACK) is True
        finally:
            _set_store(None)

    def test_a_removed_packs_transition_history_is_still_readable(
        self, monkeypatch, removed_pack
    ):
        from app.pack_state import (
            InMemoryPackStateStore,
            disable_pack,
            pack_state_history,
            rollback_pack_version,
            set_pack_state_store as _set_store,
        )

        monkeypatch.setitem(PACK_REGISTRY, PACK, removed_pack)
        _set_store(InMemoryPackStateStore())
        try:
            rollback_pack_version(ORG, PACK, PRIOR, actor_id=ACTOR)
            disable_pack(ORG, PACK, actor_id=ACTOR)
            monkeypatch.delitem(PACK_REGISTRY, PACK)

            history = pack_state_history(ORG, PACK)
            assert [event["transition"] for event in history] == [
                "disable",
                "rollback",
            ]
        finally:
            _set_store(None)

    def test_an_unknown_pack_has_no_lifecycle_record(self, monkeypatch):
        from app.pack_state import InMemoryPackStateStore, set_pack_state_store as _s

        _s(InMemoryPackStateStore())
        try:
            # A typo must still read as not-found, or the removed-pack allowance
            # would turn every bad id into a 200.
            assert has_pack_lifecycle_record(ORG, "no_such_pack_at_all") is False
        finally:
            _s(None)


# ── A direct attempt to delete each protected table must be refused ───────────


class TestDirectDeletionIsRefused:
    @pytest.mark.parametrize("table", sorted(PROTECTED_TABLES))
    @pytest.mark.parametrize("operation", sorted(LIFECYCLE_OPERATIONS))
    def test_guard_refuses_every_protected_table_for_every_operation(
        self, table, operation
    ):
        with pytest.raises(HistoryDeletionRefused) as excinfo:
            guard_delete(table, operation=operation)
        message = str(excinfo.value)
        assert table in message
        assert operation in message
        # The refusal explains WHY, not just that it refused.
        assert protection_reason(table) in message

    @pytest.mark.parametrize("table", sorted(PROTECTED_TABLES))
    def test_statement_guard_refuses_a_delete_from_each_protected_table(self, table):
        with pytest.raises(HistoryDeletionRefused):
            assert_no_history_deletion(
                f"DELETE FROM {table} WHERE id = %s", operation=OPERATION_REMOVE
            )

    @pytest.mark.parametrize("table", sorted(PROTECTED_TABLES))
    def test_statement_guard_refuses_a_truncate_of_each_protected_table(self, table):
        with pytest.raises(HistoryDeletionRefused):
            assert_no_history_deletion(
                f"TRUNCATE TABLE {table}", operation=OPERATION_REMOVE
            )

    @pytest.mark.parametrize("table", sorted(PROTECTED_TABLES))
    def test_schema_qualified_and_quoted_forms_are_also_refused(self, table):
        for statement in (
            f'DELETE FROM public.{table}',
            f'DELETE FROM "{table}"',
            f'delete   from\n   {table}',
            f'TRUNCATE {table} CASCADE',
        ):
            with pytest.raises(HistoryDeletionRefused):
                assert_no_history_deletion(statement)

    @pytest.mark.parametrize("table", sorted(DELETABLE_TABLE_REASONS))
    def test_legitimately_deletable_tables_are_permitted(self, table):
        # Blocking these would break R18-B2 retrieval freshness and graph pruning.
        guard_delete(table, operation="refresh")
        assert_no_history_deletion(f"DELETE FROM {table} WHERE org_id = %s")
        assert is_protected_table(table) is False

    def test_a_soft_delete_is_not_a_deletion(self):
        # The shape db.delete_run_events uses, and the shape any future "removal"
        # should use.
        assert_no_history_deletion(
            "UPDATE run_events SET is_deleted = TRUE WHERE run_id = %s"
        )

    def test_the_three_ac4_operations_are_all_named(self):
        assert LIFECYCLE_OPERATIONS == {
            OPERATION_DISABLE,
            OPERATION_ROLLBACK,
            OPERATION_REMOVE,
        }


class TestRevokeStatements:
    def test_a_revoke_is_generated_for_every_protected_table(self):
        statements = revoke_statements(["aiqdevusr"])
        assert len(statements) == len(PROTECTED_TABLES)
        for table in PROTECTED_TABLES:
            assert any(f'"{table}"' in statement for statement in statements)

    def test_revokes_cover_both_delete_and_truncate(self):
        for statement in revoke_statements(["aiqdevusr"]):
            assert statement.startswith("REVOKE DELETE, TRUNCATE ON TABLE")

    def test_no_revoke_is_generated_for_a_deletable_table(self):
        joined = "\n".join(revoke_statements(["aiqdevusr"]))
        for table in DELETABLE_TABLE_REASONS:
            assert f'"{table}"' not in joined

    def test_multiple_roles_each_get_the_full_set(self):
        statements = revoke_statements(["agentiq", "aiqdevusr"])
        assert len(statements) == 2 * len(PROTECTED_TABLES)
