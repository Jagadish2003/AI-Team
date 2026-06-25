"""
R16-A1 / AT-377 contract tests for the change-based ingestion foundation.

Covers the acceptance criteria assigned to this subtask:

  AC1 — A connector implementing ChangeBasedIngestor, given a checkpoint, returns
        only records changed since that checkpoint. An unchanged source returns
        an empty delta.
  AC5 — The checkpoint value is opaque to the runner. Verified by two connectors
        using different checkpoint shapes (an ISO timestamp and a sequence id)
        both working unchanged with the same runner-style driver.

Two deliberately different connector implementations are defined here so the
"opaque value" guarantee is proven by construction, not asserted in prose.
"""

import dataclasses

import pytest

from discovery.ingest.base import Checkpoint, DeltaBatch, ChangeBasedIngestor


# ─────────────────────────────────────────────────────────────────────────────
# Two connectors with DIFFERENT opaque checkpoint shapes (AC5)
# ─────────────────────────────────────────────────────────────────────────────


class TimestampConnector(ChangeBasedIngestor):
    """Checkpoint value is an ISO timestamp string (Salesforce SystemModstamp-style)."""

    connector_id = "timestamp_source"

    def __init__(self, records):
        # records: list of {"id": str, "updated_at": iso-str, ...}
        self._records = records

    def ingest_changes(self, org_id, since):
        if since is None:
            changed = list(self._records)
        else:
            changed = [r for r in self._records if r["updated_at"] > since.value]
        max_ts = max((r["updated_at"] for r in self._records), default="")
        next_value = max_ts or (since.value if since else "")
        yield DeltaBatch(records=changed, next_checkpoint=next_value, is_complete=True)


class SequenceConnector(ChangeBasedIngestor):
    """Checkpoint value is a monotonically increasing sequence id (DB change-tracking-style).

    The value is stored as a *string* (opaque to the runner) and parsed back to
    an int only inside the connector that owns the shape.
    """

    connector_id = "sequence_source"

    def __init__(self, records):
        # records: list of {"id": str, "seq": int, ...}
        self._records = records

    def ingest_changes(self, org_id, since):
        if since is None:
            changed = list(self._records)
        else:
            cutoff = int(since.value)
            changed = [r for r in self._records if r["seq"] > cutoff]
        max_seq = max((r["seq"] for r in self._records), default=0)
        next_value = str(max_seq) if self._records else (since.value if since else "0")
        yield DeltaBatch(records=changed, next_checkpoint=next_value, is_complete=True)


def _drive(ingestor, org_id, since):
    """Minimal runner-style driver: opaque to checkpoint contents (AC5).

    It only persists/returns next_checkpoint as a string — it never parses or
    branches on the value. Returns (records, next_checkpoint_or_None).
    """
    all_records = []
    last_value = None
    for batch in ingestor.ingest_changes(org_id, since):
        assert isinstance(batch, DeltaBatch)
        all_records.extend(batch.records)
        if batch.is_complete:
            last_value = batch.next_checkpoint  # persisted verbatim, never interpreted
    if last_value is None:
        return all_records, None
    return all_records, Checkpoint.create(ingestor.connector_id, org_id, last_value)


# ─────────────────────────────────────────────────────────────────────────────
# Data-structure tests
# ─────────────────────────────────────────────────────────────────────────────


def test_checkpoint_is_frozen():
    cp = Checkpoint(connector_id="c", org_id="o", value="v", captured_at="2026-06-23T00:00:00+00:00")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cp.value = "mutated"  # type: ignore[misc]


def test_checkpoint_create_stamps_captured_at():
    cp = Checkpoint.create("c", "o", "abc123")
    assert cp.connector_id == "c"
    assert cp.value == "abc123"
    assert cp.captured_at  # stamped, ISO string


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(connector_id="", org_id="o", value="v", captured_at="t"),
        dict(connector_id="c", org_id="", value="v", captured_at="t"),
        dict(connector_id="c", org_id="o", value=123, captured_at="t"),  # value not str
        dict(connector_id="c", org_id="o", value="v", captured_at=""),
    ],
)
def test_checkpoint_validation_rejects_bad_fields(kwargs):
    with pytest.raises(ValueError):
        Checkpoint(**kwargs)


def test_delta_batch_defaults_to_empty_complete():
    b = DeltaBatch()
    assert b.records == []
    assert b.is_complete is True
    assert b.is_empty is True


def test_delta_batch_rejects_non_dict_records():
    with pytest.raises(ValueError):
        DeltaBatch(records=["not a dict"])


def test_change_based_ingestor_cannot_be_instantiated():
    with pytest.raises(TypeError):
        ChangeBasedIngestor()  # abstract


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — only-changed-since-checkpoint, and unchanged → empty delta
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_timestamp_connector_returns_only_changed():
    records = [
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-10T00:00:00+00:00"},
        {"id": "c", "updated_at": "2026-06-20T00:00:00+00:00"},
    ]
    conn = TimestampConnector(records)

    # First run (no checkpoint) → full load.
    first, cp = _drive(conn, "org1", None)
    assert {r["id"] for r in first} == {"a", "b", "c"}
    assert cp is not None and cp.value == "2026-06-20T00:00:00+00:00"

    # Incremental run from a mid checkpoint → only later records.
    mid = Checkpoint.create("timestamp_source", "org1", "2026-06-05T00:00:00+00:00")
    changed, _ = _drive(conn, "org1", mid)
    assert {r["id"] for r in changed} == {"b", "c"}


def test_ac1_unchanged_source_returns_empty_delta():
    records = [{"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"}]
    conn = TimestampConnector(records)
    # Checkpoint already at/after the latest record → nothing changed.
    cp = Checkpoint.create("timestamp_source", "org1", "2026-06-01T00:00:00+00:00")
    changed, next_cp = _drive(conn, "org1", cp)
    assert changed == []  # empty delta
    # Position does not regress.
    assert next_cp is not None and next_cp.value == "2026-06-01T00:00:00+00:00"


def test_ac1_sequence_connector_returns_only_changed():
    records = [{"id": "a", "seq": 1}, {"id": "b", "seq": 2}, {"id": "c", "seq": 3}]
    conn = SequenceConnector(records)

    since = Checkpoint.create("sequence_source", "org1", "1")
    changed, cp = _drive(conn, "org1", since)
    assert {r["id"] for r in changed} == {"b", "c"}
    assert cp is not None and cp.value == "3"

    # Unchanged: checkpoint at the max sequence → empty delta.
    at_head = Checkpoint.create("sequence_source", "org1", "3")
    none_changed, _ = _drive(conn, "org1", at_head)
    assert none_changed == []


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — checkpoint value is opaque to the runner/driver
# ─────────────────────────────────────────────────────────────────────────────


def test_ac5_runner_treats_value_as_opaque_across_shapes():
    """Same driver, two completely different value shapes, both work unchanged.

    The driver (_drive) never parses ``next_checkpoint`` — it persists the string
    verbatim. The timestamp connector encodes an ISO string; the sequence
    connector encodes a stringified integer. Both round-trip through the same
    runner-style loop with no shape-specific branching.
    """
    ts_conn = TimestampConnector([
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-02T00:00:00+00:00"},
    ])
    seq_conn = SequenceConnector([{"id": "x", "seq": 10}, {"id": "y", "seq": 11}])

    _, ts_cp = _drive(ts_conn, "org1", None)
    _, seq_cp = _drive(seq_conn, "org1", None)

    # The runner holds two opaque string values of entirely different shapes.
    assert ts_cp.value == "2026-06-02T00:00:00+00:00"
    assert seq_cp.value == "11"
    assert isinstance(ts_cp.value, str) and isinstance(seq_cp.value, str)

    # Feeding each opaque checkpoint straight back yields an empty delta — the
    # driver did not need to understand either shape to do so.
    ts_again, _ = _drive(ts_conn, "org1", ts_cp)
    seq_again, _ = _drive(seq_conn, "org1", seq_cp)
    assert ts_again == []
    assert seq_again == []
