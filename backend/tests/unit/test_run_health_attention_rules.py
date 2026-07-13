"""Hermetic rule tests for R18-C2 T3 run-health attention items."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import health_aggregation as health


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _connector(connector_id: str, *, checkpoint_at: str | None = None) -> dict:
    return {
        "connector_id": connector_id,
        "name": connector_id.title(),
        "checkpoint_captured_at": checkpoint_at,
    }


def test_seeded_expired_auth_and_stalled_checkpoint_are_ordered_and_linked(monkeypatch):
    expired_at = (NOW - timedelta(hours=1)).isoformat()
    stalled_at = (NOW - timedelta(days=2)).isoformat()
    connectors = [
        _connector("salesforce"),
        _connector("servicenow", checkpoint_at=stalled_at),
    ]
    monkeypatch.setattr(
        health,
        "_credential_metadata",
        lambda org_id, connector_id: {
            "kind": "oauth",
            "expires_at": expired_at,
            "refresh_failed": False,
            "has_refresh_token": False,
            "updated_at": expired_at,
        } if org_id == "org-a" and connector_id == "salesforce" else None,
    )

    items = [
        *health._auth_attention_items("org-a", connectors, [], NOW),
        *health._checkpoint_attention_items(connectors, NOW),
    ]
    ordered = sorted(items, key=health._attention_sort_key)

    assert [item["id"] for item in ordered] == [
        "auth:salesforce",
        "checkpoint:servicenow",
    ]
    assert [item["severity"] for item in ordered] == ["critical", "high"]
    assert ordered[0]["panel"] == "connectors"
    assert ordered[0]["href"].endswith("connector=salesforce")
    assert ordered[1]["panel"] == "connectors"
    assert ordered[1]["details"]["checkpoint_age_seconds"] == 2 * 24 * 60 * 60


def test_equal_severity_ties_use_source_time_then_identifier():
    captured = (NOW - timedelta(days=2)).isoformat()
    items = health._checkpoint_attention_items(
        [
            _connector("servicenow", checkpoint_at=captured),
            _connector("jira", checkpoint_at=captured),
        ],
        NOW,
    )
    assert [item["id"] for item in sorted(items, key=health._attention_sort_key)] == [
        "checkpoint:jira",
        "checkpoint:servicenow",
    ]


def test_growing_embedding_backlog_uses_existing_record_span(monkeypatch):
    from app.retrieval import store

    monkeypatch.setattr(
        store,
        "pending_embedding_backlog",
        lambda org_id: {
            "count": 75,
            "oldest_created_at": (NOW - timedelta(hours=1)).isoformat(),
            "newest_created_at": (NOW - timedelta(minutes=30)).isoformat(),
        },
    )
    item = health._backlog_attention_items("org-a", NOW)[0]
    assert item["condition"] == "growing_embedding_backlog"
    assert item["severity"] == "medium"
    assert item["panel"] == "content"
    assert item["href"] == "/run-health?panel=content"


def test_embedding_backlog_record_query_is_org_scoped(monkeypatch):
    from app.retrieval import store

    captured: dict = {}

    class Cursor:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return (2, NOW - timedelta(hours=1), NOW)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(store.db, "connect", lambda: Connection())
    result = store.pending_embedding_backlog("org-a")
    assert "WHERE org_id = %s" in captured["sql"]
    assert captured["params"] == ("org-a",)
    assert result["count"] == 2


def test_repeated_stage_failure_is_promoted_but_single_failure_is_not(monkeypatch):
    one_run = [
        {
            "run_id": "run-1",
            "started_at": (NOW - timedelta(hours=2)).isoformat(),
            "updated_at": (NOW - timedelta(hours=1)).isoformat(),
            "degraded_stages": [{"stage": "roadmap", "reason": "boom"}],
        }
    ]
    monkeypatch.setattr(health, "runs_view", lambda org_id, limit: one_run)
    assert health._stage_failure_attention_items("org-a") == []

    repeated = [
        {
            **one_run[0],
            "run_id": "run-2",
            "updated_at": NOW.isoformat(),
            "degraded_stages": [{"stage": "roadmap", "reason": "boom again"}],
        },
        one_run[0],
    ]
    monkeypatch.setattr(health, "runs_view", lambda org_id, limit: repeated)
    item = health._stage_failure_attention_items("org-a")[0]
    assert item["condition"] == "repeated_stage_failure"
    assert item["run_id"] == "run-2"
    assert item["panel"] == "runs"
    assert item["details"]["failure_count"] == 2


def test_expired_but_refreshable_token_does_not_require_user_action(monkeypatch):
    monkeypatch.setattr(
        health,
        "_credential_metadata",
        lambda org_id, connector_id: {
            "kind": "oauth",
            "expires_at": (NOW - timedelta(hours=1)).isoformat(),
            "refresh_failed": False,
            "has_refresh_token": True,
            "updated_at": NOW.isoformat(),
        },
    )
    assert health._auth_attention_items(
        "org-a", [_connector("salesforce")], [], NOW
    ) == []
