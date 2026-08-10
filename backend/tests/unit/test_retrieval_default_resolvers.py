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
import discovery.ingest.sharepoint as sharepoint_mod
import discovery.ingest.sharepoint_content as sharepoint_content_mod
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


# ─────────────────────────────────────────────────────────────────────────────
# SharePoint — ONE registered resolver serves THREE artifact-id namespaces
#
# refresh.get_content_resolver dispatches on source_system alone, and both the
# reach connector (driveItems) and sharepoint_content (pages/lists) index under
# source_system='sharepoint'. Before this was handled, the resolver understood
# only the reach shape: every page and list resolved to None → 'no_content' →
# mark_failed → parked 'failed' with chunks left is_stale=TRUE and excluded from
# default retrieval. change_runner emits artifact_changed AFTER the synchronous
# hand-off has already written fresh chunks, so this hit a page's FIRST indexing.
# ─────────────────────────────────────────────────────────────────────────────

_SP_PAGE = {
    "id": "pg-runbook",
    "title": "Deployment Runbook",
    "webUrl": "https://contoso.sharepoint.com/sites/eng/SitePages/runbook.aspx",
    "lastModifiedDateTime": "2026-07-09T10:00:00Z",
    "canvasLayout": {
        "horizontalSections": [
            {
                "columns": [
                    {
                        "webparts": [
                            {"innerHtml": "<h2>Restart</h2><p>Run the failover script.</p>"}
                        ]
                    }
                ]
            }
        ]
    },
}

_SP_LIST = {
    "id": "lst-approvals",
    "displayName": "Change Approvals",
    "webUrl": "https://contoso.sharepoint.com/sites/eng/Lists/Approvals",
    "lastModifiedDateTime": "2026-07-09T11:00:00Z",
    "items": [
        {"fields": {"Title": "CR-42", "Summary": "Firewall change", "Modified": "x"}}
    ],
}


def _fake_content_ingestor(monkeypatch, *, sites=None, pages=None, lists=None):
    """Install a SharePointContentIngestor whose source reads are fixtures.

    ``_accessible_sites`` is overridden rather than its two underlying reach calls
    so the fake needs no DB; the real ``build_content_artifact`` (render → record →
    artifact) is exercised end to end.
    """
    granted = sites if sites is not None else [{"id": "S-eng", "displayName": "Engineering"}]

    class FakeContentIngestor(sharepoint_content_mod.SharePointContentIngestor):
        def __init__(self):
            # Never construct the real reach ingestor — nothing here uses it.
            super().__init__(ingestor=SimpleNamespace())

        def _accessible_sites(self, org_id):
            assert org_id == "org_1"
            return list(granted)

        def _raw_pages(self, org_id, site_id):
            return list((pages or {}).get(site_id, []))

        def _raw_lists(self, org_id, site_id):
            return list((lists or {}).get(site_id, []))

    monkeypatch.setattr(
        sharepoint_content_mod, "SharePointContentIngestor", FakeContentIngestor
    )


def test_sharepoint_resolver_returns_rendered_page_artifact(monkeypatch):
    """A ':page:' id resolves to the SAME structure-preserving prose the direct
    hand-off produces — headings intact, so the substrate's prose policy can split
    on them — never a metadata-only stub."""
    _fake_content_ingestor(monkeypatch, pages={"S-eng": [_SP_PAGE]})

    artifact = resolvers._resolve_sharepoint("org_1", "S-eng:page:pg-runbook")

    assert isinstance(artifact, ContentArtifact)
    assert artifact.source_system == "sharepoint"
    assert artifact.source_artifact == "S-eng:page:pg-runbook"
    assert artifact.content_type == "prose"
    assert artifact.content == (
        "# Deployment Runbook\n\n## Restart\n\nRun the failover script."
    )
    assert artifact.source_timestamp == "2026-07-09T10:00:00Z"
    assert artifact.provenance["content_kind"] == "page"
    assert artifact.provenance["site_name"] == "Engineering"
    assert artifact.provenance["page_id"] == "pg-runbook"
    assert artifact.provenance["origin"] == "observed"


def test_sharepoint_resolver_returns_rendered_list_artifact(monkeypatch):
    """A ':list:' id resolves through the same chain; SharePoint system columns
    (``Modified``) stay out of the indexed text."""
    _fake_content_ingestor(monkeypatch, lists={"S-eng": [_SP_LIST]})

    artifact = resolvers._resolve_sharepoint("org_1", "S-eng:list:lst-approvals")

    assert isinstance(artifact, ContentArtifact)
    assert artifact.source_artifact == "S-eng:list:lst-approvals"
    assert artifact.content == "# Change Approvals\n\n## CR-42\n\nFirewall change"
    assert artifact.provenance["content_kind"] == "list"
    assert artifact.provenance["list_id"] == "lst-approvals"
    assert "Modified" not in artifact.content


def test_sharepoint_resolver_still_resolves_reach_drive_item(monkeypatch):
    """REGRESSION: the reach '{site}/{drive}:{item}' path had no coverage at all
    before the page/list namespaces were added, so this pins it against the
    refactor of the id parse."""
    class FakeReachIngestor(sharepoint_mod.SharePointIngestor):
        def _raw_items(self, org_id, library):
            assert library == {"site_id": "S-eng", "id": "b-docs"}
            return [
                {
                    "id": "f400",
                    "name": "runbook.docx",
                    "file": {"mimeType": "application/vnd.openxmlformats"},
                    "webUrl": "https://contoso.sharepoint.com/f400",
                    "lastModifiedDateTime": "2026-07-09T09:00:00Z",
                    "parentReference": {"path": "/drive/root:/Ops"},
                    "lastModifiedBy": {"user": {"displayName": "A. Engineer"}},
                }
            ]

    monkeypatch.setattr(sharepoint_mod, "SharePointIngestor", FakeReachIngestor)

    artifact = resolvers._resolve_sharepoint("org_1", "S-eng/b-docs:f400")

    assert isinstance(artifact, ContentArtifact)
    assert artifact.source_artifact == "S-eng/b-docs:f400"
    assert "runbook.docx" in artifact.content
    assert artifact.provenance["item_id"] == "f400"


def test_sharepoint_resolver_refuses_page_in_ungranted_site(monkeypatch):
    """R18-A5 §4 — scope is re-verified at depth. A site that has lost its grant (or
    left the org's saved selection) resolves to None: the artifact keeps its
    existing state rather than being re-read from a site we may no longer read."""
    _fake_content_ingestor(
        monkeypatch,
        sites=[{"id": "S-other", "displayName": "Other"}],
        pages={"S-eng": [_SP_PAGE]},
    )

    assert resolvers._resolve_sharepoint("org_1", "S-eng:page:pg-runbook") is None


def test_sharepoint_resolver_returns_none_for_unknown_page(monkeypatch):
    """A page id no longer present in a granted site resolves to None — removal is
    remove_artifact's job, not a resolver's, so nothing is fabricated."""
    _fake_content_ingestor(monkeypatch, pages={"S-eng": [_SP_PAGE]})

    assert resolvers._resolve_sharepoint("org_1", "S-eng:page:pg-gone") is None


def test_sharepoint_content_ids_parse_without_colliding_with_drive_items():
    """The ':page:'/':list:' infix is what distinguishes the namespaces; a reach
    driveItem id carries neither and must fall through to the driveItem branch."""
    assert resolvers._parse_sharepoint_content_id("S-eng:page:pg-1") == (
        "S-eng",
        "page",
        "pg-1",
    )
    assert resolvers._parse_sharepoint_content_id("S-eng:list:lst-1") == (
        "S-eng",
        "list",
        "lst-1",
    )
    assert resolvers._parse_sharepoint_content_id("S-eng/b-docs:f400") is None
    assert resolvers._parse_sharepoint_content_id("no-separator") is None


def test_sharepoint_content_freshness_is_attributed_to_the_substrate_source_system(
    monkeypatch,
):
    """The freshness subscriber keys on (org_id, source_system, source_artifact).

    ``SharePointContentIngestor.connector_id`` is 'sharepoint_content' (its own
    checkpoint namespace) while its pages are INDEXED under 'sharepoint'. Attributing
    the change event to the connector id marked zero chunks stale and filed the queue
    row under a source system with no registered resolver — ``no_resolver``, pending
    forever. The telemetry event must still carry the real connector id.
    """
    from discovery.ingest import change_runner

    notified: list = []
    emitted: list = []
    monkeypatch.setattr(change_runner, "_notify_freshness", notified.append)
    monkeypatch.setattr(
        change_runner,
        "record_event",
        lambda *a, **k: None,
        raising=False,
    )

    class _FakeTelemetry:
        @staticmethod
        def record_event(event_type, payload):
            emitted.append((event_type, payload))

    monkeypatch.setitem(
        __import__("sys").modules, "app.telemetry", _FakeTelemetry
    )

    change_runner._emit_artifact_changed(
        "org_1",
        "sharepoint_content",
        [{"artifact_id": "S-eng:page:pg-1", "change_kind": "updated"}],
        retrieval_source_system="sharepoint",
    )

    assert notified, "freshness must still be notified"
    # Freshness is told the SUBSTRATE identity, so the stale-mark can match.
    assert notified[0]["connector_id"] == "sharepoint"
    assert notified[0]["artifact_id"] == "S-eng:page:pg-1"
    # Telemetry keeps the real connector id — which connector observed the change is
    # a different question from which partition holds the chunks.
    assert emitted and emitted[0][1]["connector_id"] == "sharepoint_content"


def test_connectors_without_an_override_are_unchanged(monkeypatch):
    """The override is opt-in: every other connector keeps connector_id as its
    freshness source system, so this change is byte-identical for them."""
    from discovery.ingest import change_runner

    notified: list = []
    monkeypatch.setattr(change_runner, "_notify_freshness", notified.append)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.telemetry",
        type("_T", (), {"record_event": staticmethod(lambda *a, **k: None)}),
    )

    change_runner._emit_artifact_changed(
        "org_1", "confluence", [{"artifact_id": "ENG:100", "change_kind": "updated"}]
    )

    assert notified and notified[0]["connector_id"] == "confluence"


def test_sharepoint_content_ingestor_declares_the_substrate_source_system():
    """Pins the declaration itself, so the ingestor cannot quietly lose it."""
    assert (
        sharepoint_content_mod.SharePointContentIngestor.retrieval_source_system
        == "sharepoint"
    )
    # And the two really are different — which is why the override is needed at all.
    assert sharepoint_content_mod.SharePointContentIngestor.connector_id != "sharepoint"


def test_sharepoint_declares_both_backing_modules():
    """Both connectors serving source_system='sharepoint' must be import-checked at
    startup. A lazily-imported second module would fail INSIDE the resolver, which
    surfaces as no_content/resolver_error and parks the row — the exact silent
    failure the registration-time check exists to prevent."""
    assert set(resolvers._RESOLVER_MODULES["sharepoint"]) == {
        "discovery.ingest.sharepoint",
        "discovery.ingest.sharepoint_content",
    }
    # Every entry is a tuple, so the registration loop never iterates a bare string.
    assert all(
        isinstance(mods, tuple) for mods in resolvers._RESOLVER_MODULES.values()
    )
