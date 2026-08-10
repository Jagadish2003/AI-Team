"""COR-04's producer — the documentation probe (ENT-2 rule, finally reachable).

COR-04 has been in the corroboration registry since ENT-2 and has never fired in a
real run: its check reads ``covenant_documentation_present`` (falling back to
``documentation_gap``) and nothing produced either key.

The rule this suite defends is the one that makes the feature safe rather than
merely present: **"we searched and found nothing" and "we could not search" are the
same empty result set and opposite conclusions.** COR-04 ELEVATES on absence, so
conflating them would promote a misconfigured embedding provider into a HIGH-
confidence claim that a customer's process is undocumented.
"""
from __future__ import annotations

import pytest

from discovery.ingest.documentation_probe import (
    ABSENT,
    COVENANT_REVIEW,
    DOCUMENTED,
    UNKNOWN,
    DocumentationProbe,
    apply_to_confluence_block,
    probe_documentation,
)


class _Chunk:
    def __init__(self, source_system="confluence", source_artifact="ENG:100"):
        self.source_system = source_system
        self.source_artifact = source_artifact


def _probe(result, topic=COVENANT_REVIEW):
    def _retrieve(*_a, **_k):
        if isinstance(result, Exception):
            raise result
        return result

    return probe_documentation("org_1", topic, retrieve_fn=_retrieve)


# ── The distinction the whole module exists for ──────────────────────────────
def test_searched_and_empty_is_a_gap():
    p = _probe([])
    assert p.status == ABSENT
    assert p.is_gap is True
    assert p.conclusive is True


def test_could_not_search_is_never_a_gap():
    """The failure mode this prevents: an embedding provider outage reported to a
    customer as 'your covenant review process is undocumented' — at HIGH."""
    p = _probe(RuntimeError("provider down"))
    assert p.status == UNKNOWN
    assert p.is_gap is False
    assert p.conclusive is False
    assert p.detail == "RuntimeError"


def test_found_documentation_is_not_a_gap():
    p = _probe([_Chunk()])
    assert p.status == DOCUMENTED
    assert p.is_documented is True
    assert p.is_gap is False
    assert p.top_source_artifact == "ENG:100"


def test_unknown_topic_is_unknown_not_absent():
    """A typo'd topic must not elevate a finding — an undeclared question was never
    asked, so it cannot have been answered 'no'."""
    p = _probe([], topic="not_a_declared_topic")
    assert p.status == UNKNOWN
    assert p.is_gap is False


def test_missing_org_is_unknown():
    assert probe_documentation("", COVENANT_REVIEW, retrieve_fn=lambda *a, **k: []).status == UNKNOWN


# ── Corpus scoping ───────────────────────────────────────────────────────────
def test_sharepoint_file_metadata_does_not_count_as_documentation():
    """'Name: covenants.xlsx / Type: file' is a filename card, not a documented
    process. Counting it would silently SUPPRESS a real gap."""
    p = _probe([_Chunk(source_system="sharepoint", source_artifact="S-eng/b-docs:f400")])
    assert p.status == ABSENT


def test_sharepoint_page_content_does_count():
    p = _probe([_Chunk(source_system="sharepoint", source_artifact="S-eng:page:pg-1")])
    assert p.status == DOCUMENTED


# ── The hand-off onto the corroboration block ────────────────────────────────
def test_absent_probe_marks_documentation_not_present():
    block = apply_to_confluence_block({"activity": {}}, _probe([]))
    assert block["covenant_documentation_present"] is False
    assert block["activity"] == {}          # existing block content preserved


def test_documented_probe_marks_documentation_present():
    block = apply_to_confluence_block({}, _probe([_Chunk()]))
    assert block["covenant_documentation_present"] is True


def test_unknown_probe_leaves_the_key_absent():
    """check_cor04 reads None -> falls back to documentation_gap -> False. Writing
    False here would mean 'documentation is missing', which is not what we found."""
    block = apply_to_confluence_block({}, _probe(RuntimeError("down")))
    assert "covenant_documentation_present" not in block


def test_every_probe_travels_with_its_evidence():
    """Including UNKNOWN — a reviewer must be able to see what was asked."""
    for result in ([], [_Chunk()], RuntimeError("down")):
        block = apply_to_confluence_block({}, _probe(result))
        assert block["documentation_probe"]["topic"] == COVENANT_REVIEW
        assert "status" in block["documentation_probe"]


# ── End to end: the rule can finally fire ────────────────────────────────────
def test_cor04_fires_from_a_probe_produced_block():
    """The whole point. Before this producer existed, COR-04 evaluated False on
    every run because the fact it reads was never written by anything."""
    from app.corroboration_engine import check_cor04_confluence_doc_gap

    run_data = {
        "connected_systems": ["salesforce", "confluence"],
        "confluence": apply_to_confluence_block({}, _probe([])),
    }
    assert check_cor04_confluence_doc_gap("COVENANT_TRACKING_GAP", run_data) is True


def test_cor04_does_not_fire_when_the_corpus_could_not_be_searched():
    from app.corroboration_engine import check_cor04_confluence_doc_gap

    run_data = {
        "connected_systems": ["salesforce", "confluence"],
        "confluence": apply_to_confluence_block({}, _probe(RuntimeError("down"))),
    }
    assert check_cor04_confluence_doc_gap("COVENANT_TRACKING_GAP", run_data) is False


def test_cor04_does_not_fire_when_documentation_exists():
    from app.corroboration_engine import check_cor04_confluence_doc_gap

    run_data = {
        "connected_systems": ["salesforce", "confluence"],
        "confluence": apply_to_confluence_block({}, _probe([_Chunk()])),
    }
    assert check_cor04_confluence_doc_gap("COVENANT_TRACKING_GAP", run_data) is False


# ── Runner wiring ────────────────────────────────────────────────────────────
def test_runner_probes_only_for_ncino_runs(monkeypatch):
    """COR-04 is gated on COVENANT_TRACKING_GAP, so a run with no nCino pack must
    not pay for a retrieval query whose answer it cannot use."""
    from discovery import runner
    from discovery.ingest import documentation_probe as dp

    calls = []
    monkeypatch.setattr(
        dp, "probe_documentation",
        lambda org, topic, **kw: (calls.append(topic), DocumentationProbe(topic, ABSENT))[1],
    )
    # change_runner is imported INSIDE the function, so patch it at its own module.
    from discovery.ingest import change_runner

    monkeypatch.setattr(
        change_runner, "ingest_with_checkpoint",
        lambda *a, **k: change_runner.IngestionResult(
            org_id="org_1", connector_id="confluence"
        ),
    )

    runner._ingest_confluence_corroboration("org_1", "run_1")
    assert calls == []

    runner._ingest_confluence_corroboration(
        "org_1", "run_1", probe_covenant_documentation=True
    )
    assert calls == [COVENANT_REVIEW]
