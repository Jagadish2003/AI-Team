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


def test_slack_resolver_returns_current_message_artifact(monkeypatch):
    class FakeSlackIngestor:
        def _raw_messages(self, org_id, channel):
            assert org_id == "org_1"
            assert channel == {"id": "C123"}
            return [
                {"ts": "100.000", "text": "old"},
                {
                    "ts": "1700000000.000000",
                    "text": "pricing exception needs review",
                    "edited": {"ts": "1700000001.000000"},
                    "thread_ts": "1700000000.000000",
                    "user": "U1",
                    "reply_count": 2,
                    "reactions": [{"name": "eyes", "count": 1}],
                },
            ]

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)

    artifact = resolvers._resolve_slack("org_1", "C123:1700000000.000000")

    assert isinstance(artifact, ContentArtifact)
    assert artifact.source_system == "slack"
    assert artifact.source_artifact == "C123:1700000000.000000"
    assert artifact.content == "pricing exception needs review"
    assert artifact.content_type == "conversation"
    assert artifact.provenance["thread_ts"] == "1700000000.000000"


def test_confluence_resolver_returns_metadata_artifact(monkeypatch):
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

    monkeypatch.setattr(confluence_mod, "ConfluenceIngestor", FakeConfluenceIngestor)

    artifact = resolvers._resolve_confluence("org_1", "OPS:42")

    assert artifact.source_system == "confluence"
    assert artifact.source_artifact == "OPS:42"
    assert artifact.content_type == "prose"
    assert "Operating model" in artifact.content
    assert "Analyst One" in artifact.content
    assert artifact.source_timestamp == "2026-07-08T12:00:00Z"


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


def test_resolvers_return_none_for_unknown_artifact(monkeypatch):
    class FakeSlackIngestor:
        def _raw_messages(self, org_id, channel):
            return [{"ts": "1.0", "text": "not it"}]

    monkeypatch.setattr(slack_mod, "SlackIngestor", FakeSlackIngestor)

    assert resolvers._resolve_slack("org_1", "C123:2.0") is None
