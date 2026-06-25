"""Contract tests for R16-A1 / AT-382 (T6) — delete / tombstone handling (§5, AC6).

Verifies:
  * the delete vocabulary — ``ChangeKind`` constants and the ``tombstone()`` helper;
  * AC6 part 1 — a delete-supporting connector's tombstone propagates through the
    runner as a ``change_kind='deleted'`` event;
  * AC6 part 2 — a source that cannot report deletes declares the limitation
    explicitly in code (``reports_deletes = False``, documented in its docstring).

The runner is driven with fake connectors and ``record_event`` is captured
(monkeypatched), so the assertions are hermetic — no DB needed.
"""
from __future__ import annotations

import pytest

from discovery.ingest.base import (
    CHANGE_KINDS,
    ChangeBasedIngestor,
    ChangeKind,
    DeltaBatch,
    tombstone,
)
from discovery.ingest import change_runner

EVENT = "ingestion.artifact_changed"


class _Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


class DeleteAwareConnector(ChangeBasedIngestor):
    """A source that NATIVELY reports deletions (e.g. Confluence's trash feed)."""

    connector_id = "confluence"
    reports_deletes = True

    def __init__(self, records):
        self._records = records

    def ingest_changes(self, org_id, since):
        yield DeltaBatch(records=self._records, next_checkpoint="cp1", is_complete=True)


class AppendOnlyConnector(ChangeBasedIngestor):
    """A source that CANNOT report deletions.

    Models an append-only event/audit API that only ever surfaces new rows and
    never signals a removal. Per R16-A1 §5 / AT-382 this limitation is declared
    explicitly — ``reports_deletes`` stays False — so the foundation does not
    silently pretend deletions are caught here.
    """

    connector_id = "append_only_log"
    # reports_deletes inherited as False — the explicit "no delete detection" flag.

    def ingest_changes(self, org_id, since):
        yield DeltaBatch(
            records=[{"artifact_id": "row-1", "change_kind": ChangeKind.CREATED}],
            next_checkpoint="cp1",
            is_complete=True,
        )


@pytest.fixture
def captured(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _drive(connector):
    store = _Store()
    return change_runner.ingest_with_checkpoint(
        connector, "org-1", read_checkpoint=store.read, save_checkpoint=store.save
    )


def _artifacts(captured):
    return [p for (e, p) in captured if e == EVENT]


# --------------------------------------------------------------------------
# Delete vocabulary: ChangeKind constants + tombstone() helper.
# --------------------------------------------------------------------------
def test_change_kind_constants():
    assert (ChangeKind.CREATED, ChangeKind.UPDATED, ChangeKind.DELETED) == (
        "created",
        "updated",
        "deleted",
    )
    assert CHANGE_KINDS == {"created", "updated", "deleted"}


def test_tombstone_builds_a_delete_record():
    assert tombstone("X1") == {"artifact_id": "X1", "change_kind": "deleted"}
    # extra connector metadata is merged in; the two key fields are always set.
    t = tombstone("X2", source_path="/wiki/x2")
    assert t["artifact_id"] == "X2"
    assert t["change_kind"] == "deleted"
    assert t["source_path"] == "/wiki/x2"


def test_tombstone_rejects_empty_artifact_id():
    with pytest.raises(ValueError):
        tombstone("")


def test_tombstone_fixed_keys_cannot_be_overridden_by_fields():
    # artifact_id is a NAMED parameter, so a caller cannot also smuggle it through
    # **fields — Python raises TypeError rather than silently overriding it. This
    # is a stronger guarantee than dict-merge priority.
    with pytest.raises(TypeError):
        tombstone("real_id", artifact_id="wrong")
    # change_kind, by contrast, is not a named parameter, so it lands in **fields —
    # but the explicit "change_kind": DELETED after {**fields} always wins. Pins
    # that merge order against an accidental reorder that would let a caller
    # mislabel a delete.
    assert tombstone("X", change_kind="created")["change_kind"] == "deleted"


# --------------------------------------------------------------------------
# AC6 part 1: a deletion from a delete-supporting source propagates as a
# change_kind='deleted' event.
# --------------------------------------------------------------------------
def test_delete_supporting_connector_propagates_deleted(captured):
    conn = DeleteAwareConnector(
        [
            tombstone("PAGE-9"),                                   # removed page
            {"artifact_id": "PAGE-10", "change_kind": ChangeKind.UPDATED},
        ]
    )
    assert conn.reports_deletes is True

    _drive(conn)

    kinds = {a["artifact_id"]: a["change_kind"] for a in _artifacts(captured)}
    assert kinds["PAGE-9"] == "deleted"   # the deletion propagated
    assert kinds["PAGE-10"] == "updated"


# --------------------------------------------------------------------------
# AC6 part 2: a source that cannot report deletes declares the limitation in code.
# --------------------------------------------------------------------------
def test_reports_deletes_defaults_false():
    # The contract default is "this source does NOT report deletes" — a connector
    # must opt in deliberately, so the gap is never silent.
    assert ChangeBasedIngestor.reports_deletes is False


def test_source_without_delete_support_declares_limitation():
    assert AppendOnlyConnector.reports_deletes is False
    # The limitation is documented explicitly in the connector's docstring (§5).
    assert "cannot" in (AppendOnlyConnector.__doc__ or "").lower()
