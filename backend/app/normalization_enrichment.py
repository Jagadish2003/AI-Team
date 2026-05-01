"""
T41-4a — Normalization Claude Enrichment v1.2

Changes from v1.1:
  Issue 3 fix: fieldId-based matching, not positional array index.
    Each ambiguous field is identified by its id in the prompt.
    Claude is instructed to return {fieldId, entity_type, confidence, reasoning}.
    Results are matched by fieldId via a dictionary lookup.
    If Claude returns a malformed item or an unknown fieldId, that specific
    field falls back to AMBIGUOUS — other correctly resolved fields are unaffected.
    This eliminates the risk of resolutions attaching to the wrong field.

Still one Claude call per run (batched) — unchanged from v1.1.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

KV_NORMALIZATION_ENRICHMENT = "normalization_enrichment"

_CANONICAL_ENTITIES = [
    "Application",
    "Service",
    "Workflow",
    "DataObject",
    "User",
    "Other",
]


def _build_batch_prompt(fields: List[Dict[str, Any]]) -> str:
    """
    Build a single prompt for all AMBIGUOUS fields.

    Issue 3 fix: each field is identified by its fieldId, not array index.
    Claude is instructed to return fieldId in each response object.
    """
    entities_str = ", ".join(_CANONICAL_ENTITIES)

    lines = []
    for f in fields:
        samples = ", ".join(f'"{v}"' for v in f.get("sampleValues", [])[:3]) or "none"
        lines.append(
            f'fieldId="{f["id"]}" field="{f["sourceField"]}" '
            f'source="{f["sourceSystem"]}" type="{f.get("sourceType", "")}" '
            f"samples=[{samples}]"
        )

    fields_block = "\n".join(lines)

    return (
        f"Classify each field below into one of these entity types: {entities_str}.\n\n"
        f"Fields to classify:\n{fields_block}\n\n"
        f"Respond ONLY with a JSON array. No markdown, no preamble.\n"
        f"Each object must have exactly these keys:\n"
        f'  "fieldId"    — the exact fieldId string from the input above\n'
        f'  "entity_type" — one of: {entities_str}\n'
        f'  "confidence" — HIGH, MEDIUM, or LOW\n'
        f'  "reasoning"  — one sentence\n\n'
        f"Example for 2 fields:\n"
        f'[{{"fieldId":"map_001","entity_type":"Application","confidence":"HIGH",'
        f'"reasoning":"Field contains application names."}},\n'
        f'{{"fieldId":"map_003","entity_type":"User","confidence":"MEDIUM",'
        f'"reasoning":"Values appear to be team or user identifiers."}}]'
    )


def _call_claude_batch(
    fields: List[Dict[str, Any]],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    One Claude API call for all AMBIGUOUS fields.

    Issue 3 fix: returns a dict keyed by fieldId, not a positional list.
    Invalid items (unknown entity type, missing fieldId, unknown fieldId)
    are skipped — other valid resolutions are preserved.

    Returns None on total failure (API unavailable, JSON parse error, etc).
    Returns {} if Claude responded but all items were invalid.
    Returns {fieldId: result_dict} for valid resolutions.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not fields:
        return None

    valid_ids = {f["id"] for f in fields}
    prompt = _build_batch_prompt(fields)

    payload = json.dumps(
        {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data.get("content", [{}])[0].get("text", "").strip()
            text = text.replace("```json", "").replace("```", "").strip()
            results = json.loads(text)

            if not isinstance(results, list):
                return None

            # Issue 3 fix: match by fieldId, skip invalid/unknown items
            by_field_id: Dict[str, Dict[str, Any]] = {}
            for item in results:
                if not isinstance(item, dict):
                    continue
                field_id = item.get("fieldId", "")
                if not field_id or field_id not in valid_ids:
                    continue  # unknown or missing fieldId — skip safely
                if item.get("entity_type") not in _CANONICAL_ENTITIES:
                    continue  # invalid entity type — skip safely
                if item.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
                    continue  # invalid confidence — skip safely
                by_field_id[field_id] = item

            return by_field_id

    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        ValueError,
        TypeError,
    ):
        return None


def enrich_ambiguous_mappings(
    run_id: str,
    raw_mappings: List[Dict[str, Any]],
    db: Any,
) -> List[Dict[str, Any]]:
    """
    One Claude call per run (batched) to resolve AMBIGUOUS field mappings.

    Issue 3 fix: results matched by fieldId, not array position.
    Each field falls back independently if Claude did not return a valid
    resolution for it. Valid resolutions are never lost due to one bad item.
    """
    if not raw_mappings:
        return []

    ambiguous = [
        m
        for m in raw_mappings
        if m.get("status") == "AMBIGUOUS" or m.get("confidence") == "AMBIGUOUS"
    ]
    non_ambiguous = [
        m
        for m in raw_mappings
        if m.get("status") != "AMBIGUOUS" and m.get("confidence") != "AMBIGUOUS"
    ]

    enrichment_log: List[Dict[str, Any]] = []
    resolved_mappings: List[Dict[str, Any]] = []

    if ambiguous:
        # One call, fieldId-keyed results
        by_field_id = _call_claude_batch(ambiguous)

        for mapping in ambiguous:
            field_id = mapping.get("id", "")
            result = (by_field_id or {}).get(field_id)

            if result:
                enriched = {
                    **mapping,
                    "commonEntity": result["entity_type"],
                    "confidence": result["confidence"],
                    "status": "MAPPED",
                    "notes": (
                        (mapping.get("notes") or "")
                        + f" [Claude: {result['reasoning']}]"
                    ).strip(),
                    "_enrichedByClaude": True,
                }
                resolved_mappings.append(enriched)
                enrichment_log.append(
                    {
                        "fieldId": field_id,
                        "field": mapping.get("sourceField"),
                        "source": mapping.get("sourceSystem"),
                        "resolvedEntity": result["entity_type"],
                        "resolvedConfidence": result["confidence"],
                        "reasoning": result.get("reasoning", ""),
                    }
                )
            else:
                # Claude did not resolve this specific field — stays AMBIGUOUS
                resolved_mappings.append(mapping)
    else:
        resolved_mappings = []

    updated = non_ambiguous + resolved_mappings

    try:
        existing = db.run_kv_get(KV_NORMALIZATION_ENRICHMENT, run_id, {})
        db.run_kv_set(
            KV_NORMALIZATION_ENRICHMENT,
            run_id,
            {
                **(existing or {}),
                "resolvedFields": enrichment_log,
                "resolvedCount": len(enrichment_log),
                "remainingAmbiguous": sum(
                    1 for m in updated if m.get("status") == "AMBIGUOUS"
                ),
                "batchCallMade": bool(ambiguous),
                "claudeAvailable": bool(os.environ.get("ANTHROPIC_API_KEY")),
            },
        )
    except Exception:
        pass

    return updated
