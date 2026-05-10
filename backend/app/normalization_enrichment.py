"""
SHARED-2 — Sprint 5 — nCino Lending Entity Extension
normalization_enrichment.py

Extends the normalization enrichment module with five nCino lending
canonical entity types. The six original Service Cloud entity types
are unchanged.

Issue 2 fix (SHARED-2 review):
  pack_domain parameter gates which entity types Claude can classify into.
  'ncino' → all 11 types (6 SC + 5 lending).
  'service_cloud' or None → 6 original types only.
  Prevents lending entities from appearing in Service Cloud runs before
  SHARED-1 (pack selector) exists.

Issue 1 fix (SHARED-2 review):
  KV_NORMALIZATION_ENRICHMENT renamed to KV_NORMALIZATION to match the
  key the normalization route reads. One shared constant — no mismatch.

Issue 5 fix: headers updated to Sprint 5 / SHARED-2.

Carried forward from T41-4a v1.2:
  fieldId-based Claude response matching (not positional).
  One batched Claude call per run.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# ── Shared KV key — must match routes_normalization.py ───────────────────────
# Issue 1 fix: single constant used by both enrichment (write) and route (read).
KV_NORMALIZATION = "normalization"

# ── Service Cloud entity types (original — unchanged) ─────────────────────────
_SERVICE_CLOUD_ENTITIES = [
    "Application",
    "Service",
    "Workflow",
    "DataObject",
    "User",
    "Other",
]

# ── nCino lending entity types (SHARED-2 addition) ────────────────────────────
# Confirmed from real Org 2 metadata — May 2026.
# Only exposed to Claude when pack_domain='ncino'.
_NCINO_LENDING_ENTITIES = [
    "Loan",
    "Covenant",
    "Checklist",
    "SpreadPeriod",
    "LendingApproval",
]

# ── Combined list ─────────────────────────────────────────────────────────────
# SHARED-1 will filter this via pack_domain at runtime.
# Until SHARED-1 lands, callers pass pack_domain explicitly.
_CANONICAL_ENTITIES = _SERVICE_CLOUD_ENTITIES + _NCINO_LENDING_ENTITIES


def _entities_for_domain(pack_domain: Optional[str]) -> List[str]:
    """
    Return the entity types Claude may classify into for this pack domain.

    Issue 2 fix: gates lending types to nCino runs only.
      pack_domain='ncino'          → all 11 types
      pack_domain='service_cloud'  → 6 Service Cloud types only
      pack_domain=None             → 6 Service Cloud types only (safe default)
    """
    if pack_domain == "ncino":
        return _CANONICAL_ENTITIES
    return _SERVICE_CLOUD_ENTITIES


def _build_batch_prompt(
    fields: List[Dict[str, Any]],
    pack_domain: Optional[str] = None,
) -> str:
    """
    Build a single prompt for all AMBIGUOUS fields.
    Entity types available to Claude depend on pack_domain.
    """
    allowed = _entities_for_domain(pack_domain)
    entities_str = ", ".join(allowed)

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
        f'[{{"fieldId":"map_001","entity_type":"{allowed[0]}","confidence":"HIGH",'
        f'"reasoning":"Field contains application names."}},\n'
        f'{{"fieldId":"map_003","entity_type":"{allowed[1]}","confidence":"MEDIUM",'
        f'"reasoning":"Values appear to be team or user identifiers."}}]'
    )


def _call_claude_batch(
    fields: List[Dict[str, Any]],
    pack_domain: Optional[str] = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    One Claude API call for all AMBIGUOUS fields.

    Returns None on total failure.
    Returns {} if Claude responded but all items were invalid.
    Returns {fieldId: result_dict} for valid resolutions.

    Issue 2 fix: valid entity types gated by pack_domain.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not fields:
        return None

    allowed = set(_entities_for_domain(pack_domain))
    valid_ids = {f["id"] for f in fields}
    prompt = _build_batch_prompt(fields, pack_domain)

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

            by_field_id: Dict[str, Dict[str, Any]] = {}
            for item in results:
                if not isinstance(item, dict):
                    continue
                field_id = item.get("fieldId", "")
                if not field_id or field_id not in valid_ids:
                    continue
                if item.get("entity_type") not in allowed:
                    continue  # reject types outside this pack's allowed set
                if item.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
                    continue
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
    pack_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    One Claude call per run (batched) to resolve AMBIGUOUS field mappings.

    pack_domain gates which entity types Claude classifies into:
      'ncino'         → Loan, Covenant, Checklist, SpreadPeriod, LendingApproval
                        + all 6 Service Cloud types
      'service_cloud' → Application, Service, Workflow, DataObject, User, Other
      None            → same as service_cloud (safe default)

    Issue 1 fix: writes to KV_NORMALIZATION (shared key with route).
    Issue 2 fix: pack_domain gates entity type set.
    """
    if not raw_mappings:
        return []

    ambiguous = [
        m for m in raw_mappings
        if m.get("status") == "AMBIGUOUS" or m.get("confidence") == "AMBIGUOUS"
    ]
    non_ambiguous = [
        m for m in raw_mappings
        if m.get("status") != "AMBIGUOUS" and m.get("confidence") != "AMBIGUOUS"
    ]

    enrichment_log: List[Dict[str, Any]] = []
    resolved_mappings: List[Dict[str, Any]] = []

    if ambiguous:
        by_field_id = _call_claude_batch(ambiguous, pack_domain)

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
                resolved_mappings.append(mapping)
    else:
        resolved_mappings = []

    updated = non_ambiguous + resolved_mappings

    try:
        # Issue 1 fix: store {"rows": [...], ...metadata} so the route can read
        # rows via stored["rows"]. The route checks isinstance(stored, list) for
        # legacy compatibility — we also support the dict shape here.
        existing = db.run_kv_get(KV_NORMALIZATION, run_id, {}) or {}
        db.run_kv_set(
            KV_NORMALIZATION,
            run_id,
            {
                **existing,
                "rows":              updated,          # actual normalised rows — route reads this
                "resolvedFields":    enrichment_log,
                "resolvedCount":     len(enrichment_log),
                "remainingAmbiguous": sum(
                    1 for m in updated if m.get("status") == "AMBIGUOUS"
                ),
                "batchCallMade":     bool(ambiguous),
                "claudeAvailable":   bool(os.environ.get("ANTHROPIC_API_KEY")),
                "packDomain":        pack_domain or "service_cloud",
            },
        )
    except Exception:
        pass

    return updated
