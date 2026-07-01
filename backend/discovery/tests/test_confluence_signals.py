"""
R17-A2 / AT-460 (T3) — tests for Confluence reach-phase signal extraction.

Covers the acceptance criterion assigned to this subtask:

  AC7 — The connector ingests activity/metadata signal only: no page-body
        content is read. Signals are counts, timing, contributor patterns, and
        pattern-matched markers over the title (metadata) — never the body.

Signal types are tested as pure functions, then end to end through
``ConfluenceIngestor`` (records carry a ``signals`` block) and the
``build_confluence_signal`` aggregator (space activity, cross-references, and the
stale-but-load-bearing set).
"""
from __future__ import annotations

from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.confluence_signals import (
    LOAD_BEARING_MIN_VERSIONS,
    build_confluence_corroboration_payload,
    build_confluence_signal,
    build_page_signals,
    extract_cross_reference_markers,
    extract_space_activity,
)


def _all_records():
    return [r for b in ConfluenceIngestor().ingest_changes("org1", None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference markers (from the title — metadata, not body)
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_reference_from_title():
    markers = extract_cross_reference_markers("Incident INC-4821 postmortem")
    assert ("servicenow", "INC-4821") in {(m["system"], m["ref"]) for m in markers}


def test_cross_reference_empty_when_no_marker():
    assert extract_cross_reference_markers("On-call rota") == []


# ─────────────────────────────────────────────────────────────────────────────
# Per-page signal block
# ─────────────────────────────────────────────────────────────────────────────
def test_build_page_signals_shape_and_churn():
    sig = build_page_signals({"title": "Fix INC-4821", "version_number": LOAD_BEARING_MIN_VERSIONS})
    assert set(sig.keys()) == {"cross_references", "activity"}
    assert sig["activity"]["version_number"] == LOAD_BEARING_MIN_VERSIONS
    assert sig["activity"]["high_churn"] is True
    assert any(m["ref"] == "INC-4821" for m in sig["cross_references"])


def test_build_page_signals_low_churn():
    sig = build_page_signals({"title": "Runbook", "version_number": 1})
    assert sig["activity"]["high_churn"] is False
    assert sig["cross_references"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Space activity
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_space_activity_counts_and_contributors():
    records = [
        {"content_type": "page", "modified_at": "2026-06-10T09:00:00Z", "modified_by": "u1", "version_number": 1},
        {"content_type": "page", "modified_at": "2026-06-10T10:00:00Z", "modified_by": "u2", "version_number": 3},
        {"content_type": "blogpost", "modified_at": "2026-06-11T09:00:00Z", "modified_by": "u1", "version_number": 1},
    ]
    a = extract_space_activity("ENG", "Engineering", records)
    assert a["content_count"] == 3
    assert a["page_count"] == 2
    assert a["blogpost_count"] == 1
    assert a["contributor_count"] == 2  # u1, u2
    assert a["churn_avg"] == round((1 + 3 + 1) / 3, 4)


# ─────────────────────────────────────────────────────────────────────────────
# AC7 end-to-end — records carry a metadata-only signals block, no body
# ─────────────────────────────────────────────────────────────────────────────
def test_ingestor_records_carry_signals_block_no_body():
    records = _all_records()
    assert records
    for r in records:
        assert "signals" in r
        assert set(r["signals"].keys()) == {"cross_references", "activity"}
        assert "body" not in r  # page body is never read (AC7)


def test_aggregate_signal_extracts_activity_and_cross_references():
    signal = build_confluence_signal(_all_records())

    # Space activity present for the two granted spaces.
    assert set(signal["activity"].keys()) == {"ENG", "OPS"}
    assert signal["activity"]["ENG"]["content_count"] == 4  # 3 pages + 1 blogpost
    assert signal["activity"]["ENG"]["is_active"] is True

    # Cross-reference markers come from titles (INC-4821, PR-2290).
    refs = {(m["system"], m["ref"]) for m in signal["cross_references"]}
    assert ("servicenow", "INC-4821") in refs
    assert any(m["system"] == "github" for m in signal["cross_references"])


def test_stale_but_load_bearing_flagged():
    """A rarely-updated page with a high accumulated edit count is flagged as the
    early 'important but neglected' signal (metadata-only proxy for load-bearing)."""
    records = [
        {"artifact_id": "ENG:1", "space_key": "ENG", "content_type": "page",
         "title": "Legacy auth design", "version_number": LOAD_BEARING_MIN_VERSIONS + 3,
         "modified_at": "2025-01-01T00:00:00Z", "modified_by": "u1"},
        {"artifact_id": "ENG:2", "space_key": "ENG", "content_type": "page",
         "title": "Recent notes", "version_number": LOAD_BEARING_MIN_VERSIONS + 3,
         "modified_at": "2026-06-11T00:00:00Z", "modified_by": "u2"},
    ]
    signal = build_confluence_signal(records)
    flagged = {p["artifact_id"] for p in signal["stale_load_bearing"]}
    assert flagged == {"ENG:1"}  # old + heavily edited; the recent page is excluded


def test_stale_load_bearing_empty_for_fresh_low_churn_corpus():
    # The fixture pages are all recent and low-churn → nothing stale-load-bearing.
    signal = build_confluence_signal(_all_records())
    assert signal["stale_load_bearing"] == []


def test_corroboration_payload_is_keyed_as_confluence():
    payload = build_confluence_corroboration_payload(_all_records())
    assert set(payload.keys()) == {"confluence"}
    assert "activity" in payload["confluence"]


def test_ac7_title_used_verbatim_not_body():
    """Reach phase reads the title (metadata) verbatim for marker scanning and
    never a page body — proof no deep content is applied."""
    rec = next(r for r in _all_records() if r["artifact_id"] == "ENG:200")
    assert rec["title"] == "Incident INC-4821 postmortem"
    assert "body" not in rec
