"""
T41-4a v1.2 — normalization_enrichment.py contract tests

New tests for Issue 3 fix:
  - Partial resolution: one invalid item does not discard valid resolutions
  - fieldId mismatch safely skipped
  - Empty fieldId safely skipped
  - Invalid entity type safely skipped
  - Mixed valid/invalid batch results in partial resolution
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch
from app.normalization_enrichment import enrich_ambiguous_mappings, _call_claude_batch


def mapped_row(i=1, source='ServiceNow'):
    return {
        "id": f"map_{i:03d}", "sourceSystem": source, "sourceType": "CMDB",
        "sourceField": f"field_{i}", "commonEntity": "Application",
        "commonField": "Application.name", "status": "MAPPED",
        "confidence": "HIGH", "sampleValues": ["App A"],
    }

def ambiguous_row(i=99, source='ServiceNow'):
    return {
        "id": f"map_{i:03d}", "sourceSystem": source, "sourceType": "CMDB",
        "sourceField": f"ambig_{i}", "commonEntity": "Application",
        "commonField": "Application.owner", "status": "AMBIGUOUS",
        "confidence": "AMBIGUOUS", "sampleValues": ["Eng Team"],
    }

def mock_db():
    db = MagicMock()
    db.run_kv_get.return_value = {}
    db.run_kv_set.return_value = None
    return db


# ── Core behaviour (carried from v1.1) ───────────────────────────────────────

def test_returns_same_length():
    rows = [mapped_row(1), ambiguous_row(2), mapped_row(3)]
    result = enrich_ambiguous_mappings("run_001", rows, mock_db())
    assert len(result) == len(rows)

def test_non_ambiguous_pass_through_unchanged():
    row = mapped_row(1)
    result = enrich_ambiguous_mappings("run_001", [row], mock_db())
    assert result[0]["status"] == "MAPPED"
    assert result[0]["confidence"] == "HIGH"

def test_empty_input_returns_empty():
    assert enrich_ambiguous_mappings("run_001", [], mock_db()) == []

def test_kv_write_failure_does_not_raise():
    db = mock_db()
    db.run_kv_set.side_effect = Exception("KV down")
    enrich_ambiguous_mappings("run_001", [mapped_row()], db)  # must not raise

def test_ambiguous_stays_when_no_api_key():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        result = enrich_ambiguous_mappings("run_001", [ambiguous_row()], mock_db())
    assert result[0]["status"] == "AMBIGUOUS"

def test_batch_call_not_made_when_no_ambiguous():
    db = mock_db()
    enrich_ambiguous_mappings("run_001", [mapped_row()], db)
    stored = db.run_kv_set.call_args[0][2]
    assert stored["batchCallMade"] is False

def test_all_ambiguous_fall_back_together_when_no_api_key():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    rows = [ambiguous_row(1), ambiguous_row(2), ambiguous_row(3)]
    with patch.dict(os.environ, env, clear=True):
        result = enrich_ambiguous_mappings("run_001", rows, mock_db())
    assert all(r["status"] == "AMBIGUOUS" for r in result)


# ── Issue 3: fieldId-based matching ──────────────────────────────────────────

def test_call_claude_batch_returns_none_without_api_key():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        result = _call_claude_batch([ambiguous_row()])
    assert result is None

def test_call_claude_batch_returns_none_for_empty_fields():
    result = _call_claude_batch([])
    assert result is None

def test_fieldid_mismatch_does_not_affect_valid_resolutions():
    """
    If Claude returns a response where one fieldId is unknown, that item
    is silently skipped. Valid resolutions for known fieldIds are preserved.
    This is the core Issue 3 correctness guarantee.
    """
    fields = [ambiguous_row(1), ambiguous_row(2)]
    # Simulate Claude response: one valid, one with unknown fieldId
    mock_response_text = json_array([
        {"fieldId": "map_001", "entity_type": "Application", "confidence": "HIGH", "reasoning": "App name."},
        {"fieldId": "map_UNKNOWN", "entity_type": "Service", "confidence": "HIGH", "reasoning": "Unknown field."},
    ])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            wrap_claude_response(mock_response_text)
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = _call_claude_batch(fields)
    # Only map_001 should be in the result — map_UNKNOWN is ignored
    assert result is not None
    assert "map_001" in result
    assert "map_UNKNOWN" not in result

def test_invalid_entity_type_safely_skipped():
    """Item with invalid entity_type is skipped; other valid items preserved."""
    fields = [ambiguous_row(1), ambiguous_row(2)]
    mock_response_text = json_array([
        {"fieldId": "map_001", "entity_type": "Application", "confidence": "HIGH", "reasoning": "Valid."},
        {"fieldId": "map_002", "entity_type": "INVALID_TYPE", "confidence": "HIGH", "reasoning": "Bad."},
    ])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            wrap_claude_response(mock_response_text)
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = _call_claude_batch(fields)
    assert result is not None
    assert "map_001" in result
    assert "map_002" not in result

def test_missing_fieldid_safely_skipped():
    """Item missing fieldId key is skipped."""
    fields = [ambiguous_row(1)]
    mock_response_text = json_array([
        {"entity_type": "Application", "confidence": "HIGH", "reasoning": "No fieldId."},
    ])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            wrap_claude_response(mock_response_text)
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = _call_claude_batch(fields)
    # All items were invalid — result is empty dict, not None
    assert result == {}

def test_partial_resolution_does_not_discard_valid_resolutions():
    """
    enrich_ambiguous_mappings: when Claude resolves field A but not field B
    (field B has invalid entity), field A should be promoted and field B
    should remain AMBIGUOUS.
    """
    rows = [ambiguous_row(1), ambiguous_row(2)]
    mock_response_text = json_array([
        {"fieldId": "map_001", "entity_type": "Application", "confidence": "HIGH", "reasoning": "Valid."},
        # map_002 deliberately omitted
    ])
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value.read.return_value = (
            wrap_claude_response(mock_response_text)
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = enrich_ambiguous_mappings("run_001", rows, mock_db())

    resolved = next((r for r in result if r["id"] == "map_001"), None)
    still_ambiguous = next((r for r in result if r["id"] == "map_002"), None)
    assert resolved is not None and resolved["status"] == "MAPPED"
    assert still_ambiguous is not None and still_ambiguous["status"] == "AMBIGUOUS"


# ── Helpers ───────────────────────────────────────────────────────────────────

import json

def json_array(items):
    return json.dumps(items).encode()

def wrap_claude_response(content_bytes: bytes) -> bytes:
    """Wrap raw content in Anthropic API response shape."""
    return json.dumps({
        "content": [{"type": "text", "text": content_bytes.decode()}]
    }).encode()
