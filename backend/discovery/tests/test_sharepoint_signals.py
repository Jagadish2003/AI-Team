"""
R17-A2 / AT-460 (T3) — tests for SharePoint reach-phase signal extraction.

Covers the acceptance criterion assigned to this subtask:

  AC7 — The connector ingests activity/metadata signal only: no document-body
        content is read. Signals are counts, structure, timing, ownership, and
        pattern-matched markers over the item name (metadata) — never the body.

Signal types are tested as pure functions, then end to end through
``SharePointIngestor`` (records carry a ``signals`` block) and the
``build_sharepoint_signal`` aggregator (library activity, cross-references, and
the active/dormant estate split).
"""
from __future__ import annotations

from discovery.ingest.sharepoint import SharePointIngestor
from discovery.ingest.sharepoint_signals import (
    build_item_signals,
    build_sharepoint_corroboration_payload,
    build_sharepoint_signal,
    extract_cross_reference_markers,
    extract_library_activity,
)


def _all_records():
    return [r for b in SharePointIngestor().ingest_changes("org1", None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference markers (from the item name — metadata, not body)
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_reference_from_item_name():
    markers = extract_cross_reference_markers("Q3-incident-INC-4821.docx")
    assert ("servicenow", "INC-4821") in {(m["system"], m["ref"]) for m in markers}


def test_cross_reference_empty_for_plain_name():
    assert extract_cross_reference_markers("roadmap.docx") == []


# ─────────────────────────────────────────────────────────────────────────────
# Per-item signal block
# ─────────────────────────────────────────────────────────────────────────────
def test_build_item_signals_shape():
    rec = {
        "item_name": "postmortem-INC-4821.md",
        "item_type": "file",
        "size": 8192,
        "parent_path": "/drive/root:/Reports/2026",
    }
    sig = build_item_signals(rec)
    assert set(sig.keys()) == {"cross_references", "activity"}
    assert sig["activity"] == {"item_type": "file", "size": 8192, "depth": 2}
    assert any(m["ref"] == "INC-4821" for m in sig["cross_references"])


# ─────────────────────────────────────────────────────────────────────────────
# Library activity
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_library_activity_structure_and_ownership():
    records = [
        {"item_type": "file", "created_by": "U1", "last_modified_by": "U1",
         "last_modified_at": "2026-06-10T09:00:00Z", "parent_path": "/drive/root:"},
        {"item_type": "file", "created_by": "U2", "last_modified_by": "U1",
         "last_modified_at": "2026-06-10T10:00:00Z", "parent_path": "/drive/root:/Archive"},
        {"item_type": "folder", "created_by": "U2", "last_modified_by": "U2",
         "created_at": "2026-06-10T11:00:00Z", "parent_path": "/drive/root:"},
    ]
    a = extract_library_activity("S/b", "Eng", "Documents", records)
    assert a["item_count"] == 3
    assert a["file_count"] == 2
    assert a["folder_count"] == 1
    assert a["owner_count"] == 2       # U1, U2 (createdBy)
    assert a["editor_count"] == 2      # U1, U2 (lastModifiedBy)
    assert a["max_depth"] == 1         # /Archive is one level under root


# ─────────────────────────────────────────────────────────────────────────────
# AC7 end-to-end — records carry a metadata-only signals block, no content
# ─────────────────────────────────────────────────────────────────────────────
def test_ingestor_records_carry_signals_block_no_content():
    records = _all_records()
    assert records
    for r in records:
        assert "signals" in r
        assert set(r["signals"].keys()) == {"cross_references", "activity"}
        assert "content" not in r  # document body is never read (AC7)


def test_aggregate_signal_activity_and_estates():
    signal = build_sharepoint_signal(_all_records())

    # Both granted libraries appear with structure/ownership signal.
    keys = set(signal["activity"].keys())
    assert "S-eng/b-docs" in keys
    docs = signal["activity"]["S-eng/b-docs"]
    assert docs["file_count"] == 3      # roadmap, budget, postmortem
    assert docs["folder_count"] == 1    # Archive
    assert docs["is_active"] is True

    # Active/dormant estate split is present and consistent with the activity map.
    estates = signal["estates"]
    assert set(estates["active"]) | set(estates["dormant"]) == keys
    assert "S-eng/b-docs" in estates["active"]


def test_dormant_estate_detected():
    """A library whose newest item is far behind the corpus's most-recent
    modification is classified dormant."""
    records = [
        {"site_id": "S", "drive_id": "live", "site_name": "S", "library_name": "Live",
         "item_type": "file", "last_modified_at": "2026-06-11T00:00:00Z",
         "created_by": "u1", "last_modified_by": "u1", "parent_path": "/drive/root:",
         "item_name": "current.docx"},
        {"site_id": "S", "drive_id": "old", "site_name": "S", "library_name": "Old",
         "item_type": "file", "last_modified_at": "2025-01-01T00:00:00Z",
         "created_by": "u2", "last_modified_by": "u2", "parent_path": "/drive/root:",
         "item_name": "ancient.docx"},
    ]
    signal = build_sharepoint_signal(records)
    assert signal["estates"]["active"] == ["S/live"]
    assert signal["estates"]["dormant"] == ["S/old"]


def test_corroboration_payload_is_keyed_as_sharepoint():
    payload = build_sharepoint_corroboration_payload(_all_records())
    assert set(payload.keys()) == {"sharepoint"}
    assert "activity" in payload["sharepoint"]
