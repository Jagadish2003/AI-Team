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

MODEL            = "claude-sonnet-4-5"
MAX_TOKENS_OPP   = 1024
MAX_TOKENS_EXEC  = 512
API_URL          = "https://api.anthropic.com/v1/messages"
API_VERSION      = "2023-06-01"
KV_LLM_ENRICHMENT = "llm_enrichment"

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _opp_prompt(opp: Dict[str, Any], evidence: List[Dict[str, Any]],
                pack_id: Optional[str] = None) -> str:
    """
    ENG-AIQ-NC-5: pack-aware opportunity prompt.
    ncino pack uses banking operations language and compliance instruction.
    service_cloud pack uses original SC prompt unchanged.
    """
    from discovery.packs.pack_config import is_ncino_pack, get_llm_context
    ev_snippets = [
        e.get("snippet", "")
        for e in evidence
        if e.get("id") in (opp.get("evidenceIds") or [])
        and e.get("snippet")
    ]
    debug = opp.get("_debug", {})

    if is_ncino_pack(pack_id):
        # nCino banking-language prompt — SF-NC-4 approved labels
        llm_ctx = get_llm_context("ncino")
        # Separate nCino evidence from Jira/SN corroboration
        ncino_snippets = [s for s in ev_snippets if "Jira" not in s and "ServiceNow" not in s]
        corr_snippets  = [s for s in ev_snippets if "Jira" in s or "ServiceNow" in s]

        return f"""You are a commercial banking operations analyst writing insights for a Head of Commercial Lending or CRO.

## Context
{llm_ctx}

## Lending Pattern Detected
Title: {opp.get("title", "")}
Category: {opp.get("category", "")}
Tier: {opp.get("tier", "")}
Impact: {opp.get("impact", "")}/10
Confidence: {opp.get("confidence", "")}
Detector: {debug.get("detector_id", "")}

## nCino Evidence (from Salesforce/nCino data)
{chr(10).join(f"- {s}" for s in ncino_snippets) if ncino_snippets else "- No nCino signals available"}

## Corroborating Evidence (from Jira / ServiceNow)
{chr(10).join(f"- {s}" for s in corr_snippets) if corr_snippets else "- No corroborating evidence"}

## Instructions
You are writing for a CRO or Head of Commercial Lending — not a Salesforce admin.
Use banking operations language: loan origination, covenant compliance, credit analysis.
NEVER suggest automated credit decisions. All credit decisions require human approval.
Return a JSON object with exactly these four fields. No preamble, no markdown — JSON only.

{{
  "aiSummary": "2-4 sentences in plain banking operations language. What the lending friction is and how an Agentforce agent addresses it — without making credit decisions.",
  "aiWhyBullets": [
    "Specific measured lending friction fact from nCino evidence",
    "Corroborating signal from Jira or ServiceNow if available",
    "Business impact in banking terms (loan cycle time, compliance risk, borrower experience)"
  ],
  "aiRisks": [
    "What specifically happens to loan pipeline or compliance if not addressed",
    "Regulatory or relationship risk of inaction"
  ],
  "aiSuggestedNextSteps": [
    "Specific Agentforce capability for this lending pattern",
    "Concrete next action — escalation path or pilot scope"
  ]
}}"""

    else:
        # Original Service Cloud prompt — unchanged
        return f"""You are an AI analyst generating business explanations for a Salesforce automation discovery report.

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

## Existing Rationale (context only)
{opp.get("aiRationale", "")}

## Instructions
Return a JSON object with exactly these four fields. No preamble, no markdown — JSON only.

{{
  "aiSummary": "2-4 sentences in plain business language. What the problem is and how an Agentforce agent fixes it.",
  "aiWhyBullets": [
    "Bullet with a specific measured fact from evidence",
    "Bullet with another fact or consequence",
    "Bullet connecting to business impact"
  ],
  "aiRisks": [
    "What specifically happens if not addressed",
    "Downstream business consequence of inaction"
  ],
  "aiSuggestedNextSteps": [
    "Specific Agentforce capability that addresses this",
    "Concrete next action the team should take"
  ]
}}"""


def _exec_summary_prompt(opps: List[Dict[str, Any]], sources_analyzed: Dict[str, Any],
                          pack_id: Optional[str] = None) -> str:
    """ENG-AIQ-NC-5: pack-aware executive summary prompt."""
    from discovery.packs.pack_config import is_ncino_pack
    top_opps = opps[:3]
    opp_lines = "\n".join(
        f"- {o.get('title', '')} (Impact {o.get('impact', '')}/10, {o.get('tier', '')})"
        for o in top_opps
    )

    if is_ncino_pack(pack_id):
        return f"""You are writing a one-paragraph executive summary for a commercial banking CRO or Head of Commercial Lending.

## Discovery Context
Sources analyzed: {sources_analyzed.get("totalConnected", 0)} connected systems (Salesforce nCino, Jira, ServiceNow)
Top lending friction patterns:
{opp_lines}

## Instructions
Write exactly one paragraph (3-5 sentences) for a CRO audience.
- Use commercial banking language: loan origination, covenant compliance, credit decisions
- Open with the most significant lending friction detected
- Reference the number of systems that corroborate the finding
- Include projected outcome using "could reduce" / "estimated" language
- Close with a recommended next step for the commercial lending team
- NEVER suggest automated credit decisions — humans make all credit decisions
- Return only the paragraph text, nothing else"""

    else:
        return f"""You are writing a one-paragraph executive summary for a Salesforce automation discovery report.

## Discovery Context
Sources analyzed: {sources_analyzed.get("totalConnected", 0)} connected systems
Top opportunities:
{opp_lines}

## Instructions
Write exactly one paragraph (3-5 sentences) for a CXO audience.
- Open with the most significant automation opportunity
- Include a projected outcome using "could reduce" / "estimated" language
- Close with a clear recommended next step
- Return only the paragraph text, nothing else"""


# ─────────────────────────────────────────────────────────────────────────────
# Claude API caller
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int) -> Optional[str]:
    import urllib.request
    import urllib.error

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — LLM enrichment skipped")
        return None

    payload = json.dumps({
        "model":      MODEL,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
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
            l for l in cleaned.split("\n")
            if not l.strip().startswith("```")
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
        "aiSummary":             str,
        "aiWhyBullets":          list,
        "aiRisks":               list,
        "aiSuggestedNextSteps":  list,
    }
    for field, expected_type in required.items():
        if field not in parsed:
            logger.warning("Opp %s: missing field '%s'", opp_id, field)
            return False
        if not isinstance(parsed[field], expected_type):
            logger.warning(
                "Opp %s: field '%s' is %s, expected %s",
                opp_id, field, type(parsed[field]).__name__, expected_type.__name__
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

def _fallback(opp: Dict[str, Any], pack_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Pack-aware fallback when LLM enrichment is unavailable.
    nCino pack uses banking-language fallback summary.
    Service Cloud uses original aiRationale text.
    Issue 6 fix: prevents nCino screens showing generic SC language on fallback.
    """
    from discovery.packs.pack_config import is_ncino_pack
    if is_ncino_pack(pack_id) and not opp.get("aiRationale"):
        title = opp.get("title", "Lending friction detected")
        summary = (
            f"{title}. Connect to the Anthropic API to generate a full banking "
            f"operations analysis with evidence-backed insights for this pattern."
        )
    else:
        summary = opp.get("aiRationale", "")
    return {
        "aiSummary":             summary,
        "aiWhyBullets":          [],
        "aiRisks":               [],
        "aiSuggestedNextSteps":  [],
        "llmGenerated":          False,
        "llmModel":              None,
        "complianceGuardrailApplied": is_ncino_pack(pack_id) if pack_id else False,
    }


def _enrich_opportunity(
    opp: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    opp_id   = opp.get("id", "unknown")
    fb       = _fallback(opp, pack_id=pack_id)
    prompt   = _opp_prompt(opp, evidence, pack_id=pack_id)
    raw      = _call_claude(prompt, MAX_TOKENS_OPP)

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
        "aiSummary":             parsed["aiSummary"],
        "aiWhyBullets":          [str(b) for b in parsed["aiWhyBullets"][:3]],
        "aiRisks":               [str(b) for b in parsed["aiRisks"][:2]],
        "aiSuggestedNextSteps":  [str(b) for b in parsed["aiSuggestedNextSteps"][:2]],
        "llmGenerated":          True,
        "llmModel":              MODEL,
        # ENG-AIQ-NC-5: Sprint 5 compliance approach.
        # Guardrail is prompt-instruction only — not post-validated.
        # Post-generation validation of prohibited phrases is deferred to post-Sprint 5.
        "complianceGuardrailApplied": pack_id == "ncino",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Executive summary
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_executive_summary(
    opps: List[Dict[str, Any]],
    sources_analyzed: Dict[str, Any],
    pack_id: Optional[str] = None,
) -> str:
    if not opps:
        return ""
    raw = _call_claude(_exec_summary_prompt(opps, sources_analyzed, pack_id=pack_id), MAX_TOKENS_EXEC)
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
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ENG-AIQ-NC-5: pack_id parameter added.
    When pack_id='ncino', uses banking-language prompts and nCino UI labels.
    When pack_id='service_cloud' or None, uses original SC prompts unchanged.
    """
    """
    Synchronous post-processing enrichment step.

    Called AFTER all deterministic artifacts (opps, evidence, roadmap,
    executive_report) are persisted. Runs inline inside
    run_trackb_and_persist() — the run status transitions to complete
    only after enrichment finishes.

    Total latency: ~10-15 seconds for 7 opportunities with API key set.
    Without ANTHROPIC_API_KEY: instant fallback, no API calls.
    """
    logger.info("T6 enrichment starting for run %s — %d opportunities", run_id, len(opps))
    start = time.time()

    per_opp: Dict[str, Any] = {}
    enriched = 0
    failed   = 0

    for opp in opps:
        opp_id = opp.get("id", "")
        try:
            result = _enrich_opportunity(opp, evidence, pack_id=pack_id)
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
        exec_summary = _enrich_executive_summary(opps, sources_analyzed or {}, pack_id=pack_id)
    except Exception as e:
        logger.error("Executive summary error: %s", e)

    elapsed = round(time.time() - start, 1)
    logger.info("T6 enrichment done: %d enriched, %d fallback, %.1fs", enriched, failed, elapsed)

    return {
        "perOpportunity":        per_opp,
        "executiveSummary":      exec_summary,
        "generatedAt":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "llmModel":              MODEL,
        "opportunitiesEnriched": enriched,
        "opportunitiesFailed":   failed,
        "elapsedSeconds":        elapsed,
    }
