"""Regression: a null-timestamp stream must not suppress a connector's stall alert.

PostgreSQL orders ``DESC`` as NULLS FIRST. ``_read_stream_checkpoint`` takes
``rows[0]`` as the newest stream, so a per-stream checkpoint row with a NULL
``captured_at`` sorted to the front and became "newest" — the connector then
reported ``checkpoint_age_seconds = None``, and ``_checkpoint_attention_items``
skips a connector whose captured_at is None. Net effect: a connector whose every
real stream had been quiet for days looked fine and raised no stall alert.

A NULL ``captured_at`` is not hypothetical here. ServiceNow's optional SecOps
tables (``sn_si_incident``, ``sn_vul_*``) exist only where the plugins are
activated and readable; an absent or unreadable stream records a row with no
timestamp.

Both halves are pinned: the ordering is asserted in the SQL (so the guarantee
survives a refactor that changes the fake's row order) and the behaviour is
asserted through the real function against a fake cursor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import health_aggregation as health

ORG = "org-a"
CONNECTOR = "servicenow"

NEWEST = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
OLDER = NEWEST - timedelta(hours=30)


class _FakeCursor:
    """Records the SQL, and returns rows ordered the way PostgreSQL would."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        nulls_last = "NULLS LAST" in sql.upper()

        def key(row):
            captured = row[2]
            if captured is None:
                # NULLS LAST pushes them to the end; the default DESC pulls them
                # to the front. This is the behaviour under test.
                return (1, NEWEST) if nulls_last else (0, NEWEST)
            return (0 if nulls_last else 1, captured)

        # DESC on captured_at within each null-group.
        self._rows = sorted(self._rows, key=key, reverse=not nulls_last)
        if nulls_last:
            dated = sorted(
                [r for r in self._rows if r[2] is not None],
                key=lambda r: r[2],
                reverse=True,
            )
            self._rows = dated + [r for r in self._rows if r[2] is None]

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def rows_with_a_null_stream():
    """Two real streams plus an unavailable SecOps table carrying no timestamp."""
    return [
        (f"{CONNECTOR}:sn_si_incident", None, None),
        (f"{CONNECTOR}:cmdb_ci", "2026-08-06T12:00:00Z", NEWEST),
        (f"{CONNECTOR}:cmdb_rel_ci", "2026-08-05T06:00:00Z", OLDER),
    ]


def test_sql_orders_nulls_last(monkeypatch, rows_with_a_null_stream):
    """The ordering is stated in the query, not left to PostgreSQL's default."""
    conn = _FakeConn(rows_with_a_null_stream)
    monkeypatch.setattr(health.db, "connect", lambda: conn)

    health._read_stream_checkpoint(ORG, CONNECTOR)

    sql = " ".join(conn.cur.sql.split()).upper()
    assert "ORDER BY CAPTURED_AT DESC NULLS LAST" in sql, (
        "captured_at DESC without NULLS LAST lets a null-timestamp stream "
        "become the reported 'newest', which suppresses the stall alert"
    )


def test_null_stream_does_not_become_the_newest(monkeypatch, rows_with_a_null_stream):
    """The reported stream is the newest one that actually has a timestamp."""
    monkeypatch.setattr(
        health.db, "connect", lambda: _FakeConn(rows_with_a_null_stream)
    )

    result = health._read_stream_checkpoint(ORG, CONNECTOR)

    assert result is not None
    assert result["captured_at"] == NEWEST
    assert result["stream_id"] == f"{CONNECTOR}:cmdb_ci"
    # Every stream still counts toward the reported total, including the
    # unavailable one — the count answers "how many streams", not "how many live".
    assert result["stream_count"] == 3


def test_age_is_reported_so_a_stalled_connector_can_alert(
    monkeypatch, rows_with_a_null_stream
):
    """The whole point: a usable captured_at reaches the attention rules.

    ``_checkpoint_attention_items`` skips any connector whose captured_at is
    None, so a null here is indistinguishable from "no checkpoint" and the stall
    alert never fires.
    """
    monkeypatch.setattr(
        health.db, "connect", lambda: _FakeConn(rows_with_a_null_stream)
    )

    result = health._read_stream_checkpoint(ORG, CONNECTOR)

    assert result["captured_at"] is not None


def test_all_streams_null_reports_no_timestamp_rather_than_inventing_one(monkeypatch):
    """Honest degradation: nothing readable must not become a fabricated time."""
    rows = [
        (f"{CONNECTOR}:sn_vul_entry", None, None),
        (f"{CONNECTOR}:sn_si_incident", None, None),
    ]
    monkeypatch.setattr(health.db, "connect", lambda: _FakeConn(rows))

    result = health._read_stream_checkpoint(ORG, CONNECTOR)

    assert result is not None
    assert result["captured_at"] is None
    assert result["stream_count"] == 2


def test_no_rows_returns_none(monkeypatch):
    monkeypatch.setattr(health.db, "connect", lambda: _FakeConn([]))
    assert health._read_stream_checkpoint(ORG, CONNECTOR) is None
