"""
Sprint 4 T6 — LLM Enrichment Layer (Claude API)  v1.1

Changes from v1.0:
  Fix 7: Type validation added after JSON parse. Each LLM response field is
          checked for correct type (str, list). Wrong types trigger fallback
          rather than leaking bad data into the UI.

Hard rules (non-negotiable):
  - LLM never changes impact / effort / tier / decision / evidenceIds
  - LLM runs after ALL deterministic artifacts are persisted (opps, evidence,
    roadmap, executive_report) — synchronous post-processing step
  - LLM failure never fails the run — fallback to existing aiRationale
  - LLM output stored once per run — never re-generated on read
  - Replay returns stored LLM text — no API call on replay

What is actually enforced in code (Issue 8 — accurate statement):
  - No scoring fields in the enrichment response shape (routes_sprint4_t6.py)
  - Original opp object is never mutated by enrichment code
  - Claude response validated for correct field types before acceptance
  - "No invented numbers" is a prompt instruction — not post-checked programmatically
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
MAX_TOKENS_OPP = 1024
MAX_TOKENS_EXEC = 512
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
KV_LLM_ENRICHMENT = "llm_enrichment"

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────


def _opp_prompt(opp: Dict[str, Any], evidence: List[Dict[str, Any]]) -> str:
    ev_snippets = [
        e.get("snippet", "")
        for e in evidence
        if e.get("id") in (opp.get("evidenceIds") or []) and e.get("snippet")
    ]
    debug = opp.get("_debug", {})
    return f"""You are an AI analyst generating business-friendly banking operations insights for a commercial lending discovery report.

## Opportunity Data (read-only — do not change any values)
Title: {opp.get("title", "")}
Category: {opp.get("category", "")}
Tier: {opp.get("tier", "")}
Impact: {opp.get("impact", "")}/10
Effort: {opp.get("effort", "")}/10
Confidence: {opp.get("confidence", "")}
Detector: {debug.get("detector_id", "")}

## Evidence Snippets (use these facts, do not invent numbers)
{chr(10).join(f"- {s}" for s in ev_snippets) if ev_snippets else "- No evidence snippets available"}

## Compliance Guardrail (NON-NEGOTIABLE)
The agent strictly monitors and escalates. It NEVER makes or recommends credit decisions. It surfaces information to a human Relationship Manager or Credit Officer. Do not suggest automated credit actions.

## Instructions
Return a JSON object with exactly these four fields. No preamble, no markdown — JSON only.

{{
  "aiSummary": "2-4 sentences in business-friendly banking language. Explain the friction, impact on loan cycle time, and how an Agentforce agent assists the human team.",
  "aiWhyBullets": [
    "Fact from evidence regarding the lending bottleneck",
    "Consequence on origination or compliance",
    "Business impact for the bank"
  ],
  "aiRisks": [
    "Specific regulatory or operational risk if not monitored",
    "Downstream consequence for the relationship or compliance"
  ],
  "aiSuggestedNextSteps": [
    "Agentforce capability to surface this alert to the relationship manager",
    "Concrete human-led action required to resolve the stall"
  ]
}}"""


def _exec_summary_prompt(
    opps: List[Dict[str, Any]], sources_analyzed: Dict[str, Any]
) -> str:
    top_opps = opps[:3]
    opp_lines = "\n".join(
        f"- {o.get('title', '')} (Impact {o.get('impact', '')}/10, {o.get('tier', '')})"
        for o in top_opps
    )
    return f"""You are writing a one-paragraph executive summary for a commercial lending discovery report.

## Discovery Context
Sources analyzed: {sources_analyzed.get("totalConnected", 0)} connected systems
Top opportunities:
{opp_lines}

## Instructions
Write exactly one paragraph (3-5 sentences) for a CXO audience (CRO, Head of Commercial Lending).
- Open with the most significant lending automation opportunity.
- Include a projected outcome using "could reduce" / "estimated" language.
- Close with a clear recommended next step for the human credit committee.
- COMPLIANCE: The summary must reflect that the agent is a monitoring tool for human action, not an automated decision-maker.
- Return only the paragraph text, nothing else."""


# ─────────────────────────────────────────────────────────────────────────────
# Claude API caller
# ─────────────────────────────────────────────────────────────────────────────


def _call_claude(prompt: str, max_tokens: int) -> Optional[str]:
    import urllib.error
    import urllib.request

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — LLM enrichment skipped")
        return None

    payload = json.dumps(
        {
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
        return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        logger.error("Claude API HTTP %s: %s", e.code, body)
        return None
    except Exception as e:
        logger.error("Claude API error: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# JSON parser
# ─────────────────────────────────────────────────────────────────────────────


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            l for l in cleaned.split("\n") if not l.strip().startswith("```")
        ).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Fix 7: Type validation
# ─────────────────────────────────────────────────────────────────────────────


def _validate_opp_fields(parsed: Dict[str, Any], opp_id: str) -> bool:
    """
    Validate that Claude returned the correct types for all required fields.
    Returns False if any field is wrong type — triggers fallback.
    """
    required = {
        "aiSummary": str,
        "aiWhyBullets": list,
        "aiRisks": list,
        "aiSuggestedNextSteps": list,
    }
    for field, expected_type in required.items():
        if field not in parsed:
            logger.warning("Opp %s: missing field '%s'", opp_id, field)
            return False
        if not isinstance(parsed[field], expected_type):
            logger.warning(
                "Opp %s: field '%s' is %s, expected %s",
                opp_id,
                field,
                type(parsed[field]).__name__,
                expected_type.__name__,
            )
            return False
    # Verify aiSummary is non-empty
    if not parsed["aiSummary"].strip():
        logger.warning("Opp %s: aiSummary is empty string", opp_id)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Per-opportunity enrichment
# ─────────────────────────────────────────────────────────────────────────────


def _fallback(opp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "aiSummary": opp.get("aiRationale", ""),
        "aiWhyBullets": [],
        "aiRisks": [],
        "aiSuggestedNextSteps": [],
        "llmGenerated": False,
        "llmModel": None,
    }


def _enrich_opportunity(
    opp: Dict[str, Any],
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    opp_id = opp.get("id", "unknown")
    fb = _fallback(opp)
    prompt = _opp_prompt(opp, evidence)
    raw = _call_claude(prompt, MAX_TOKENS_OPP)

    if raw is None:
        return fb

    parsed = _parse_json(raw)
    if parsed is None:
        logger.warning("Opp %s: JSON parse failed — using fallback", opp_id)
        return fb

    # Fix 7: type validation before accepting response
    if not _validate_opp_fields(parsed, opp_id):
        logger.warning("Opp %s: type validation failed — using fallback", opp_id)
        return fb

    return {
        "aiSummary": parsed["aiSummary"],
        "aiWhyBullets": [str(b) for b in parsed["aiWhyBullets"][:3]],
        "aiRisks": [str(b) for b in parsed["aiRisks"][:2]],
        "aiSuggestedNextSteps": [str(b) for b in parsed["aiSuggestedNextSteps"][:2]],
        "llmGenerated": True,
        "llmModel": MODEL,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Executive summary
# ─────────────────────────────────────────────────────────────────────────────


def _enrich_executive_summary(
    opps: List[Dict[str, Any]],
    sources_analyzed: Dict[str, Any],
) -> str:
    if not opps:
        return ""
    raw = _call_claude(_exec_summary_prompt(opps, sources_analyzed), MAX_TOKENS_EXEC)
    if raw is None:
        return ""
    # Executive summary is plain text — reject if it looks like JSON
    if raw.strip().startswith("{"):
        parsed = _parse_json(raw)
        if parsed and isinstance(parsed.get("summary"), str):
            return parsed["summary"]
        return ""
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Main enrichment runner
# ─────────────────────────────────────────────────────────────────────────────


def run_llm_enrichment(
    run_id: str,
    opps: List[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
    sources_analyzed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Synchronous post-processing enrichment step.

    Called AFTER all deterministic artifacts (opps, evidence, roadmap,
    executive_report) are persisted. Runs inline inside
    run_trackb_and_persist() — the run status transitions to complete
    only after enrichment finishes.

    Total latency: ~10-15 seconds for 7 opportunities with API key set.
    Without ANTHROPIC_API_KEY: instant fallback, no API calls.
    """
    logger.info(
        "T6 enrichment starting for run %s — %d opportunities", run_id, len(opps)
    )
    start = time.time()

    per_opp: Dict[str, Any] = {}
    enriched = 0
    failed = 0

    for opp in opps:
        opp_id = opp.get("id", "")
        try:
            result = _enrich_opportunity(opp, evidence)
            per_opp[opp_id] = result
            if result.get("llmGenerated"):
                enriched += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Opp %s error: %s", opp_id, e)
            per_opp[opp_id] = _fallback(opp)
            failed += 1

    exec_summary = ""
    try:
        exec_summary = _enrich_executive_summary(opps, sources_analyzed or {})
    except Exception as e:
        logger.error("Executive summary error: %s", e)

    elapsed = round(time.time() - start, 1)
    logger.info(
        "T6 enrichment done: %d enriched, %d fallback, %.1fs", enriched, failed, elapsed
    )

    return {
        "perOpportunity": per_opp,
        "executiveSummary": exec_summary,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "llmModel": MODEL,
        "opportunitiesEnriched": enriched,
        "opportunitiesFailed": failed,
        "elapsedSeconds": elapsed,
    }
