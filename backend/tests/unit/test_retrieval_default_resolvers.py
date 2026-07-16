"""Unit tests for production retrieval freshness content resolvers (R18-B2).

The refresh worker needs real source-system resolvers at startup; otherwise a
queued ``artifact_changed`` event can only mark chunks stale, never refresh them.
These tests keep the connector reads fake but use the production resolver
functions and artifact-id shapes.
"""
from __future__ import annotations

from types import SimpleNamespace

import discovery.ingest.confluence as confluence_mod
import discovery.ingest.java_app as java_mod
import discovery.ingest.slack as slack_mod

from app.retrieval import default_resolvers as resolvers
from app.retrieval.ingest import ContentArtifact


def test_register_default_content_resolvers_wires_known_sources(monkeypatch):
    registered = {}
    monkeypatch.setattr(
        resolvers.refresh,
        "register_content_resolver",
        lambda source, resolver: registered.setdefault(source, resolver),
    )

    resolvers.register_default_content_resolvers()

    assert {
        "slack",
        "teams",
        "confluence",
        "sharepoint",
        "java_app",
        "dotnet_app",
    } <= set(registered)
    assert all(callable(fn) for fn in registered.values())


def test_registration_skips_resolver_whose_module_fails_to_import(monkeypatch, caplog):
    """A backing module that cannot import is a deployment problem and must surface
    ONCE, loudly, at registration — not as a silent per-artifact resolver failure on
    every refresh tick. The broken source is skipped (its artifacts stay queued as
    ``no_resolver``); every healthy resolver still registers."""
    registered = {}
    monkeypatch.setattr(
        resolvers.refresh,
        "register_content_resolver",
        lambda source, resolver: registered.setdefault(source, resolver),
    )

    real_import = resolvers._import_module

    def broken_import(name):
        if name == "discovery.ingest.teams":
            raise ImportError("simulated missing dependency")
        return real_import(name)

    monkeypatch.setattr(resolvers, "_import_module", broken_import)

    with caplog.at_level("WARNING"):
        resolvers.register_default_content_resolvers()  # must not raise

    assert "teams" not in registered
    # One bad module never blocks the healthy resolvers.
    assert {
        "slack",
        "confluence",
        "sharepoint",
        "java_app",
        "dotnet_app",
    } <= set(registered)
    # The misconfiguration is visible at startup.
    assert any(
        "NOT registering content resolver" in rec.message and "teams" in rec.message
        for rec in caplog.records
    )


def test_every_default_resolver_has_a_backing_module_declared():
    """The registration-time import check only protects sources listed in
    ``_RESOLVER_MODULES`` — a resolver added without its module mapping would crash
    registration with a KeyError. Keep the two maps in lockstep."""
    assert set(resolvers._DEFAULT_RESOLVERS) == set(resolvers._RESOLVER_MODULES)


def test_slack_resolver_returns_whole_thread_artifact(monkeypatch):
    """R18-A4 / AT-596 (T3): the Slack resolver re-extracts the WHOLE thread for a
    thread-level source_artifact, not a single message — so a refresh re-chunks the
    entire thread with author-attributed text."""
    raw = {
        "C123": [
            {"ts": "1700000000.000000", "text": "pricing exception needs review",
             "user": "U1", "reply_count": 2, "reactions": []},
            {"ts": "1700000001.000000", "text": "on it", "user": "U2",
             "thread_ts": "1700000000.000000"},
            {"ts": "1700000002.000000", "text": "fixed", "user": "U3",
             "thread_ts": "1700000000.000000"},
        ]
    }

    class FakeSlackIngestor(slack_mod.SlackIngestor):
        def _raw_messages(self, org_id, channel):
            assert org_id == "org_1"
            return list(raw.get(channel["id"], []))

        def _raw_channels(self, org_id):
            return [{"id": "C123", "name": "ops"}]

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)

    artifact = resolvers._resolve_slack("org_1", "C123:1700000000.000000")

    assert isinstance(artifact, ContentArtifact)
    assert artifact.source_system == "slack"
    assert artifact.source_artifact == "C123:1700000000.000000"  # thread-level id
    assert artifact.content_type == "conversation"
    # Whole thread, author-attributed, oldest first.
    assert artifact.content == "U1: pricing exception needs review\nU2: on it\nU3: fixed"
    assert artifact.provenance["thread_id"] == "1700000000.000000"
    assert artifact.provenance["message_count"] == 3


def test_slack_resolver_returns_empty_artifact_for_vanished_thread(monkeypatch):
    """A thread whose messages are all gone resolves to EMPTY content, so the swap
    removes its chunks (self-cleaning deletion)."""
    class FakeSlackIngestor(slack_mod.SlackIngestor):
        def _raw_messages(self, org_id, channel):
            return []

        def _raw_channels(self, org_id):
            return [{"id": "C123", "name": "ops"}]

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)

    artifact = resolvers._resolve_slack("org_1", "C123:1700000000.000000")
    assert isinstance(artifact, ContentArtifact)
    assert artifact.content == ""
    assert artifact.source_artifact == "C123:1700000000.000000"


def test_confluence_resolver_returns_full_rendered_page_artifact(monkeypatch):
    """R18-A5 / AT-603 (T4): the refresh-worker resolver must render the SAME
    full page body confluence_content.py's direct hand-off does — not a
    metadata-only stub — so a freshness-driven re-chunk of an edited page is
    never a downgrade (AC3)."""

    class FakeConfluenceIngestor:
        def _raw_content(self, org_id, space):
            assert org_id == "org_1"
            assert space == {"key": "OPS"}
            return [
                {"id": "41", "title": "Other"},
                {
                    "id": "42",
                    "type": "page",
                    "title": "Operating model",
                    "status": "current",
                    "version": {
                        "number": 3,
                        "when": "2026-07-08T12:00:00Z",
                        "by": {"displayName": "Analyst One"},
                    },
                    "_links": {"webui": "/wiki/spaces/OPS/pages/42"},
                },
            ]

        def _raw_page_body(self, org_id, space_key, content_id):
            assert org_id == "org_1"
            assert space_key == "OPS"
            assert content_id == "42"
            return {
                "body": {"storage": {"value": "<h1>Operating Model</h1><p>How the team runs day to day.</p>"}},
                "metadata": {"labels": {"results": [{"prefix": "global", "name": "process"}]}},
            }

    monkeypatch.setattr(confluence_mod, "ConfluenceIngestor", FakeConfluenceIngestor)

    artifact = resolvers._resolve_confluence("org_1", "OPS:42")

    assert artifact.source_system == "confluence"
    assert artifact.source_artifact == "OPS:42"
    assert artifact.content_type == "prose"
    # Full rendered body with headings preserved — not a metadata line.
    assert artifact.content.startswith("# Operating Model")
    assert "How the team runs day to day." in artifact.content
    assert artifact.source_timestamp == "2026-07-08T12:00:00Z"
    assert artifact.provenance["labels"] == ["process"]
    assert artifact.provenance["url"] == "/wiki/spaces/OPS/pages/42"


def test_confluence_resolver_returns_none_for_trashed_or_archived_page(monkeypatch):
    """R18-A5 / AT-603 (T4): a page whose status is no longer current must not
    be resolved into fresh content — the connector's own known-id diff already
    handles this case as a deletion (remove_artifact), not an upsert."""

    class FakeConfluenceIngestor:
        def _raw_content(self, org_id, space):
            return [
                {
                    "id": "42",
                    "type": "page",
                    "status": "trashed",
                    "version": {"number": 4, "when": "2026-07-09T00:00:00Z"},
                },
            ]

        def _raw_page_body(self, org_id, space_key, content_id):  # pragma: no cover
            raise AssertionError("must not fetch a body for a non-current page")

    monkeypatch.setattr(confluence_mod, "ConfluenceIngestor", FakeConfluenceIngestor)

    assert resolvers._resolve_confluence("org_1", "OPS:42") is None


def test_operational_resolver_returns_log_artifact(monkeypatch):
    class FakeJavaIngestor:
        def _load_targets(self, org_id):
            assert org_id == "org_1"
            return [SimpleNamespace(app_id="loan-api", service="Loan API")]

        def _read_operational(self, org_id, target, cursor):
            assert cursor == {}
            return (
                [
                    {
                        "artifact_id": "loan-api:log:9",
                        "artifact_kind": "log",
                        "app_id": "loan-api",
                        "service": "Loan API",
                        "observed_ts": "2026-07-08T12:01:00Z",
                        "log_offset": 9,
                        "level": "ERROR",
                        "logger": "pricing",
                        "message": "pricing timeout",
                    }
                ],
                {},
            )

    monkeypatch.setattr(java_mod, "JavaAppIngestor", FakeJavaIngestor)

    artifact = resolvers._resolve_java_app("org_1", "loan-api:log:9")

    assert artifact.source_system == "java_app"
    assert artifact.source_artifact == "loan-api:log:9"
    assert artifact.content_type == "prose"
    assert "pricing timeout" in artifact.content
    assert artifact.source_timestamp == "2026-07-08T12:01:00Z"


def test_resolver_returns_empty_for_unknown_thread(monkeypatch):
    """A thread id not present in the channel resolves to empty content (its chunks,
    if any, are swapped out) rather than a spurious single-message artifact."""
    class FakeSlackIngestor(slack_mod.SlackIngestor):
        def _raw_messages(self, org_id, channel):
            return [{"ts": "1.0", "text": "not it", "user": "U1"}]

        def _raw_channels(self, org_id):
            return [{"id": "C123", "name": "ops"}]

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)

    artifact = resolvers._resolve_slack("org_1", "C123:2.0")
    assert isinstance(artifact, ContentArtifact)
    assert artifact.content == ""
    assert artifact.source_artifact == "C123:2.0"


def test_resolver_returns_none_for_malformed_artifact_id(monkeypatch):
    """An id with no thread separator can't be resolved → None (stays queued)."""
    class FakeSlackIngestor(slack_mod.SlackIngestor):
        def _raw_messages(self, org_id, channel):  # pragma: no cover - never reached
            return []

        def _raw_channels(self, org_id):  # pragma: no cover - never reached
            return []

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)
    assert resolvers._resolve_slack("org_1", "no-separator") is None
