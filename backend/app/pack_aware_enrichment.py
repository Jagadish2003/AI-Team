"""Pack-aware AI enrichment for single- and multi-pack discovery runs.

Each pack is enriched separately so its prompt language and boundary rules stay
intact. In hosted mode, Security Operations input is withheld without preventing
other selected packs from receiving enrichment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from discovery.packs.security_ops_ai_mode import (
    ai_narrative_blocked_for_pack,
    hosted_enrichment_result,
)


def run_pack_aware_enrichment(
    *,
    run_id: str,
    opportunities: List[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
    sources_analyzed: Dict[str, Any],
    pack_ids: List[str],
    org_id: str,
    enrichment_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Enrich each pack independently and merge the response deterministically."""
    from .llm_enrichment import run_llm_enrichment

    if enrichment_fn is None:
        enrichment_fn = run_llm_enrichment
    using_default_enrichment = enrichment_fn is run_llm_enrichment

    hosted_security_boundary = any(
        ai_narrative_blocked_for_pack(pack_id) for pack_id in pack_ids
    )

    per_pack_results: List[Dict[str, Any]] = []
    merged_per_opportunity: Dict[str, Any] = {}
    executive_summaries: List[str] = []
    withheld_pack_ids: List[str] = []
    total_enriched = 0
    total_failed = 0
    total_elapsed = 0.0
    generated_at_values: List[str] = []
    llm_models: List[str] = []

    for pack_id in pack_ids:
        pack_opportunities = [
            opportunity
            for opportunity in opportunities
            if opportunity.get("packId") == pack_id
        ]
        referenced_evidence_ids = {
            evidence_id
            for opportunity in pack_opportunities
            for evidence_id in (opportunity.get("evidenceIds") or [])
        }
        pack_evidence = [
            item
            for item in evidence
            if item.get("packId") == pack_id
            or (
                not item.get("packId")
                and item.get("id") in referenced_evidence_ids
            )
        ]

        if ai_narrative_blocked_for_pack(pack_id):
            pack_result = hosted_enrichment_result()
            pack_result["packId"] = pack_id
            label = str(pack_result["ai_mode_label"])
            # Keep every deterministic finding addressable through the normal
            # detail API. Hosted mode withholds only AI input; it must not make
            # the finding disappear or turn its detail route into a 404.
            pack_result["perOpportunity"] = {
                str(opportunity.get("id")): {
                    "packId": pack_id,
                    "aiSummary": label,
                    "aiWhyBullets": [],
                    "aiRisks": [],
                    "aiSuggestedNextSteps": [],
                    "llmGenerated": False,
                    "llmModel": None,
                    "aiNarrativeAvailable": False,
                    "aiModeLabel": label,
                    "preliminary": True,
                    "preliminary_reason": label,
                }
                for opportunity in pack_opportunities
                if opportunity.get("id")
            }
            withheld_pack_ids.append(pack_id)
            executive_summaries.append(f"Security Operations: {label}")
        else:
            enrichment_kwargs: Dict[str, Any] = {
                "run_id": run_id,
                "opps": pack_opportunities,
                "evidence": pack_evidence,
                "sources_analyzed": sources_analyzed,
                "pack_id": pack_id,
                "org_id": org_id,
            }
            if using_default_enrichment:
                # A combined hosted run may enrich the non-security pack, but it
                # must not assemble a shared graph that can contain security
                # records. Cloud findings/evidence remain available to its prompt.
                enrichment_kwargs["allow_graph_context"] = not hosted_security_boundary
            pack_result = enrichment_fn(
                **enrichment_kwargs,
            )
            pack_result["packId"] = pack_id
            summary = str(pack_result.get("executiveSummary") or "").strip()
            if summary:
                executive_summaries.append(summary)
            total_enriched += int(pack_result.get("opportunitiesEnriched") or 0)
            total_failed += int(pack_result.get("opportunitiesFailed") or 0)
            total_elapsed += float(pack_result.get("elapsedSeconds") or 0.0)

        # Stamp the producing pack onto the UI-facing detail object. This keeps
        # terminology and evidence boundaries correct after pack results merge.
        for opportunity_id, opportunity_result in (
            pack_result.get("perOpportunity", {}) or {}
        ).items():
            if not isinstance(opportunity_result, dict):
                continue
            stamped = dict(opportunity_result)
            stamped.setdefault("packId", pack_id)
            merged_per_opportunity[str(opportunity_id)] = stamped

        generated_at = str(pack_result.get("generatedAt") or "").strip()
        if generated_at:
            generated_at_values.append(generated_at)
        llm_model = str(pack_result.get("llmModel") or "").strip()
        if llm_model and llm_model not in llm_models:
            llm_models.append(llm_model)
        per_pack_results.append(pack_result)

    result: Dict[str, Any] = {
        "perOpportunity": merged_per_opportunity,
        "executiveSummary": "\n\n".join(executive_summaries),
        "opportunitiesEnriched": total_enriched,
        "opportunitiesFailed": total_failed,
        "elapsedSeconds": round(total_elapsed, 1),
        # Preserve the established single-pack API metadata. Per-pack records
        # remain available below when a combined run ever uses different models.
        "generatedAt": (
            max(generated_at_values)
            if generated_at_values
            else datetime.now(timezone.utc).isoformat()
        ),
        "llmModel": (
            llm_models[0]
            if len(llm_models) == 1
            else ("multiple" if llm_models else None)
        ),
        "packResults": per_pack_results,
        "withheldPackIds": withheld_pack_ids,
    }
    if withheld_pack_ids:
        restriction = hosted_enrichment_result()
        result.update(
            {
                "ai_mode": restriction["ai_mode"],
                "ai_mode_label": restriction["ai_mode_label"],
                "aiModeLabel": restriction["aiModeLabel"],
            }
        )
    return result


__all__ = ["run_pack_aware_enrichment"]
