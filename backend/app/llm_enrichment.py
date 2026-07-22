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

from app.provenance import EvidencePointer

logger = logging.getLogger(__name__)

MODEL            = "claude-sonnet-4-5"
MAX_TOKENS_OPP   = 1536
MAX_TOKENS_EXEC  = 512
KV_LLM_ENRICHMENT = "llm_enrichment"


# R16-B1: deterministic 'observed at' for the INFERRED enrichment pointer. The
# narrative is produced BY the discovery run, so its source-observation time is
# the run's own recorded time. Resolving it from the run record (rather than a
# wall-clock default) keeps the no-LLM enrichment output deterministic — the S15
# hardening contract requires two identical runs to serialise identically, and a
# fresh utc_now() in every pointer would break that.
_ENRICHMENT_TS_FALLBACK = "1970-01-01T00:00:00+00:00"


def _resolve_run_observed_at(run_id: str) -> str:
    """Return the run's recorded UTC time for use as the enrichment pointer's
    deterministic ``source_timestamp``.

    Reads the run record (``completedAt`` then ``startedAt``). Falls back to a
    stable sentinel only when no run record exists — e.g. isolated unit tests —
    never a wall clock, which would make the enrichment output non-deterministic.
    Imported locally so this module keeps no import-time dependency on ``db``.
    """
    try:
        from app import db

        run = db.get_run(run_id)
        if isinstance(run, dict):
            ts = run.get("completedAt") or run.get("startedAt")
            if ts:
                return str(ts)
    except Exception:  # noqa: BLE001 — provenance timestamp is best-effort.
        pass

    # Sentinel fired: no run record / no timestamp. The value is deterministic
    # (S15 contract), but a 1970-01-01 source_timestamp stamped onto stored
    # provenance is misleading — an analyst can't tell it from real data. Surface
    # it as a WARNING so the condition is observable in real environments, while
    # staying quiet under tests (isolated runs with no seeded record are expected
    # there, and the contract suite leaves ENVIRONMENT unset).
    if (
        os.getenv("ENVIRONMENT", "").strip().lower() != "test"
        and "PYTEST_CURRENT_TEST" not in os.environ
    ):
        logger.warning(
            "R16-B1: no run record/timestamp for run %s — stamping enrichment "
            "provenance source_timestamp with epoch sentinel %s; evidence-trace "
            "timestamps for this run will be misleading.",
            run_id, _ENRICHMENT_TS_FALLBACK,
        )
    return _ENRICHMENT_TS_FALLBACK


def _attach_enrichment_provenance(
    artifact: Dict[str, Any], *, run_id: str, opp: Dict[str, Any],
    source_timestamp: Optional[str] = None,
) -> None:
    """Attach an INFERRED EvidencePointer + grounding evidence ids to an enrichment
    artifact (R16-B1).

    Enrichment narratives are model/heuristic-generated, so origin='inferred' and
    the discovery run is named as the extraction job (AC2). grounding_evidence_ids
    records the evidence the narrative was grounded in, so 1.9's full evidence
    trace can later walk a narrative back to its sources. Both are written into the
    artifact dict persisted under the run-scoped enrichment KV — a JSON blob, so no
    schema change is needed.

    ``source_timestamp`` is the run's deterministic observation time (see
    :func:`_resolve_run_observed_at`). When omitted the pointer falls back to its
    wall-clock default — fine for direct unit calls, but ``run_llm_enrichment``
    always supplies the deterministic value so the pipeline output stays stable.
    """
    grounding = [str(e) for e in (opp.get("evidenceIds") or [])]
    pointer = EvidencePointer.inferred(
        source_system="agentiq",
        source_artifact=str(opp.get("id") or "opportunity"),
        extraction_job_id=run_id,
        source_timestamp=source_timestamp,
    )
    if pointer.is_valid():
        artifact["evidence_pointer"] = pointer.to_dict()
    else:
        # Inferred output with no job id must never be surfaced as observed truth.
        logger.error(
            "Enrichment provenance invalid for opp %s — pointer omitted",
            opp.get("id"),
        )
    artifact["grounding_evidence_ids"] = grounding

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# T3-S16-A — Causal prompt section (fifth section, Section 2b)
# ─────────────────────────────────────────────────────────────────────────────
# Appended to _opp_prompt() only when a valid CausalContext is available.
# Placeholders are filled by format_dependency_paths() / format_temporal_support()
# before the section is concatenated onto the base prompt.
# Rule 3's verbatim sentence is load-bearing — T4 and T5 parse for it.
# ─────────────────────────────────────────────────────────────────────────────

CAUSAL_PROMPT_SECTION = """
=== CAUSAL CONTEXT ===
Process dependency paths in the knowledge graph:
{dependency_paths_summary}

Temporal signal support:
{temporal_support_summary}

RULES FOR CAUSAL CHAIN GENERATION:
1. Each step must be supported by data in the context above.
   Do not invent steps not evidenced by the graph or temporal data.
2. Steps relying on inferred relationships (confidence < 0.8) must be
   labelled [inferred: confidence=X].
3. The chain MUST end with one falsifiability sentence: what specific,
   measurable data would prove this hypothesis wrong.
   This sentence is mandatory. If you cannot state a falsifiability
   condition, do not produce a causal chain.
4. Maximum 5 steps. Prefer shorter, strongly evidenced chains.
5. Use only entity names present in the context above.

=== CAUSAL CHAIN OUTPUT ===
If a causal chain can be produced under the rules above, include two additional
fields in the same JSON object (alongside the four fields already requested):
  "cause_chain": ["step 1", "step 2", ...],  // JSON array of strings, max 5
  "falsifiability_condition": "..."           // plain string — mandatory with cause_chain
If you cannot produce a valid chain under the rules above, omit both fields entirely.
"""


def format_dependency_paths(dependency_paths: list) -> str:
    """Serialise dependency_paths (list[list[str]]) into a token-efficient string.

    Each path is rendered as a numbered arrow-joined chain of entity IDs.
    Returns a placeholder string when there are no paths.
    """
    if not dependency_paths:
        return "  (no process dependency paths found)"
    lines = []
    for i, path in enumerate(dependency_paths, start=1):
        lines.append(f"  {i}. {' -> '.join(str(e) for e in path)}")
    return "\n".join(lines)


def format_temporal_support(temporal_support: dict) -> str:
    """Serialise temporal_support (signal_key → {trend, anomaly, context, run_count})
    into a compact, labelled table — one line per signal.

    Returns a placeholder string when there is no support data.
    """
    if not temporal_support:
        return "  (no temporal support data available)"
    lines = []
    for signal_key, info in temporal_support.items():
        if not isinstance(info, dict):
            continue
        trend = info.get("trend", "unknown")
        anomaly = "anomalous" if info.get("anomaly") else "normal"
        run_count = info.get("run_count", "?")
        context = info.get("context") or ""
        context_str = f" | {context}" if context else ""
        lines.append(
            f"  {signal_key}: trend={trend}, {anomaly}, runs={run_count}{context_str}"
        )
    return "\n".join(lines) if lines else "  (no temporal support data available)"


def _format_causal_context_section(causal_context: Any) -> str:
    """Render the ENT-6 causal prompt addendum for any opportunity prompt."""
    return CAUSAL_PROMPT_SECTION.format(
        dependency_paths_summary=format_dependency_paths(
            causal_context.dependency_paths
        ),
        temporal_support_summary=format_temporal_support(
            causal_context.temporal_support
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _opp_prompt(opp: Dict[str, Any], evidence: List[Dict[str, Any]],
                pack_id: Optional[str] = None,
                causal_context: Optional[Any] = None) -> str:
    """
    ENG-AIQ-NC-5: pack-aware opportunity prompt.
    ncino pack uses banking operations language and compliance instruction.
    service_cloud pack uses original SC prompt unchanged.

    T3-S16-A: causal_context is optional. When provided, CAUSAL_PROMPT_SECTION
    is appended after the base prompt. When None (no CausalContext available,
    or ANTHROPIC_API_KEY unset), the prompt is returned unchanged.
    """
    from discovery.packs.pack_config import is_ncino_pack, is_strs_benefits_pack, get_llm_context
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

        base = f"""You are a commercial banking operations analyst writing insights for a Head of Commercial Lending or CRO.

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
  "aiSummary": "2-4 sentences in plain banking operations language. What the lending friction is and how an AI agent addresses it — without making credit decisions.",
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
    "Specific AI agent capability for this lending pattern",
    "Concrete next action — escalation path or pilot scope"
  ]
}}"""

    elif is_strs_benefits_pack(pack_id):
        # STRS Benefits Administration prompt — ENG-STRS-3
        from discovery.packs.pack_config import get_llm_context as _glc
        llm_ctx = _glc("strs_benefits")
        pss_snippets  = [s for s in ev_snippets if "Jira" not in s and "ServiceNow" not in s]
        corr_snippets = [s for s in ev_snippets if "Jira" in s or "ServiceNow" in s]
        base = f"""You are a public sector pension fund operations analyst writing insights for a STRS Member Services Director or Chief Operating Officer.

## Context
{llm_ctx}

## Benefit Administration Pattern Detected
Title: {opp.get("title", "")}
Category: {opp.get("category", "")}
Tier: {opp.get("tier", "")}
Impact: {opp.get("impact", "")}/10
Confidence: {opp.get("confidence", "")}
Detector: {debug.get("detector_id", "")}

## Salesforce PSS Evidence
{chr(10).join(f"- {s}" for s in pss_snippets) if pss_snippets else "- No PSS evidence items"}

## Corroborating Evidence (Jira / ServiceNow)
{chr(10).join(f"- {s}" for s in corr_snippets) if corr_snippets else "- No corroborating evidence"}

## Instructions
Write for a pension fund operations audience — not a Salesforce admin.
Use member services language: retirement applications, benefit elections, disbursements, disability reviews.
Reference Ohio Revised Code 3307 when relevant to compliance-override patterns.
NEVER suggest automated benefit decisions. All benefit actions require human approval.
Return a JSON object with exactly these four fields. No preamble, no markdown — JSON only.

{{
  "aiSummary": "2-4 sentences in plain member services language. What the benefit administration friction is and how an AI agent addresses it — surfacing alerts to staff, never making autonomous decisions.",
  "aiWhyBullets": [
    "Specific measured fact from PSS evidence (days pending, count affected)",
    "Member impact in plain language (income delay, irreversible decision, legal obligation)",
    "Corroborating signal from Jira or ServiceNow if available"
  ],
  "aiRisks": [
    "What happens to the member if not addressed (financial hardship, regulatory breach)",
    "Operational or legal risk of inaction for STRS"
  ],
  "aiSuggestedNextSteps": [
    "Specific AI agent capability for this benefit administration pattern",
    "Concrete next action — escalation path or pilot scope with compliance guardrail"
  ]
}}"""

    else:
        # Original Service Cloud prompt — unchanged
        base = f"""You are an AI analyst generating business explanations for a Salesforce automation discovery report.

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
  "aiSummary": "2-4 sentences in plain business language. What the problem is and how an AI agent fixes it.",
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
    "Specific AI agent capability that addresses this",
    "Concrete next action the team should take"
  ]
}}"""

    # T3-S16-A: append causal section when a valid CausalContext was assembled.
    # When causal_context is None (no graph data, InsufficientGraphContextError,
    # or ANTHROPIC_API_KEY unset) the prompt is returned byte-for-byte unchanged.
    if causal_context is not None:
        base += "\n" + _format_causal_context_section(causal_context)

    return base


# ─────────────────────────────────────────────────────────────────────────────
# ENT-3 / T3-S15-A — graph-grounded first-pass prompt (Section 2)
# ─────────────────────────────────────────────────────────────────────────────

def build_grounded_opp_prompt(
    signal_ctx: Dict[str, Any],
    graph_context: "Any",
    pack_llm_context: str,
    org_name: str,
    causal_context: Optional[Any] = None,
) -> str:
    """Assemble the four-section graph-grounded opportunity prompt (Section 2).

    Pure function of (signal context, graph context, pack context, org name) —
    given identical inputs it returns a byte-identical prompt, which is what
    makes the first pass deterministic (AC1). The instruction block forbids
    inventing names and requires each ``aiWhyBullets`` entry to be tagged
    ``[OBSERVED]`` or ``[INFERRED: X]`` so the frontend (T6) can render the
    OBSERVED/INFERRED pills.
    """
    observed = graph_context.observed_summary or "No directly observed entities for this finding."
    truncation_note = (graph_context.truncation_note or "").strip()
    output_intro = (
        "Produce JSON with the fields below. No preamble, no markdown - JSON only."
        if causal_context is not None
        else "Produce JSON with exactly these fields. No preamble, no markdown - JSON only."
    )

    base = f"""You are analysing an operational finding for {org_name}.
Your output appears on an executive dashboard reviewed by operations leaders.
Be specific. Reference only the names and systems listed in the context below.
Do not invent names, teams, or systems not present in the context.

=== SIGNAL CONTEXT ===
Finding: {signal_ctx.get("detector_display_name", "")}
Current value: {signal_ctx.get("metric_value", "n/a")} (threshold: {signal_ctx.get("threshold", "n/a")})
Trend: {signal_ctx.get("trend_direction", "stable")} — {signal_ctx.get("baseline_context", "")}
Corroboration: {signal_ctx.get("corroboration_label", "Not corroborated across sources")}

=== DIRECTLY OBSERVED ENTITIES AND RELATIONSHIPS ===
{observed}
{truncation_note}

=== DOMAIN CONTEXT ===
{pack_llm_context or "No additional domain context available."}

=== OUTPUT INSTRUCTIONS ===
{output_intro}
- aiSummary: 2-3 sentences specific to this organisation. Use the entity names
  above. Do not use placeholders like 'the team' when you know the team name
  from the context.
- aiWhyBullets: 3-5 bullets. Each MUST begin with a tag of either [OBSERVED] or
  [INFERRED: <basis>]. Use only entity names from the context above.
- aiRisks: 2-3 specific risks if this finding is not addressed.
- aiSuggestedNextSteps: 3 concrete actions referencing actual teams/systems.

{{
  "aiSummary": "...",
  "aiWhyBullets": ["[OBSERVED] ...", "[INFERRED: co-firing signals] ..."],
  "aiRisks": ["..."],
  "aiSuggestedNextSteps": ["..."]
}}"""

    if causal_context is not None:
        base += "\n" + _format_causal_context_section(causal_context)

    return base


def _build_signal_context(opp: Dict[str, Any], corroboration_label: Optional[str]) -> Dict[str, Any]:
    """Derive the SIGNAL CONTEXT block inputs from a stored opportunity."""
    debug = opp.get("_debug", {}) if isinstance(opp.get("_debug"), dict) else {}
    metric_value = opp.get("current_value")
    if metric_value is None:
        metric_value = opp.get("metric_value", debug.get("metric_value", "n/a"))
    return {
        "detector_display_name": opp.get("title")
        or debug.get("detector_id")
        or "Operational finding",
        "metric_value": metric_value if metric_value is not None else "n/a",
        "threshold": debug.get("threshold", opp.get("threshold", "n/a")),
        "trend_direction": opp.get("trend_direction") or "stable",
        "baseline_context": opp.get("baseline_context") or "baseline still accumulating",
        "corroboration_label": corroboration_label or "Not corroborated across sources",
    }


def _split_observation_tag(bullet: str) -> tuple:
    """Split a bullet into its leading [OBSERVED]/[INFERRED: X] tag and body.

    Returns (tag, body) where tag includes the brackets and a trailing space, or
    ("", bullet) when no tag is present. The tag is preserved across the
    hallucination guard so the frontend can still render the pill (T6).
    """
    import re as _re

    m = _re.match(r"^\s*(\[(?:OBSERVED|INFERRED[^\]]*)\])\s*", bullet or "", _re.IGNORECASE)
    if not m:
        return "", (bullet or "")
    return m.group(1) + " ", bullet[m.end():]


# Provenance tag applied to any bullet the first-pass LLM emitted WITHOUT a
# recognised [OBSERVED]/[INFERRED: X] prefix. The OUTPUT INSTRUCTIONS require a
# tag on every aiWhyBullets entry, but the model does not always comply; an
# untagged bullet would otherwise reach the frontend with no provenance pill
# (and, after a guard rewrite, the re-attached prefix would be empty). Tagging it
# UNVERIFIED guarantees the payload is consistently tagged. See ENT-4 review #2.
_UNVERIFIED_TAG = "[UNVERIFIED] "


def _corroboration_for(opp: Dict[str, Any]) -> Optional[str]:
    """Resolve the ENT-2 corroboration label for an opportunity, if present."""
    label = opp.get("corroboration_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return None


def _resolve_run_count(org_id: Optional[str], opp: Dict[str, Any]) -> int:
    """Best-effort run_count for the preliminary gate at enrichment time.

    Prefers an explicit run_count on the opp, then the detector baseline, then
    0 (which the gate treats as 'baseline still accumulating'). Never raises.
    """
    rc = opp.get("run_count")
    if isinstance(rc, int) and not isinstance(rc, bool):
        return rc
    debug = opp.get("_debug", {}) if isinstance(opp.get("_debug"), dict) else {}
    detector_id = debug.get("detector_id")
    if org_id and detector_id:
        try:
            from .temporal import get_baseline

            baseline = get_baseline(org_id, detector_id)
            if isinstance(baseline, dict) and baseline.get("run_count") is not None:
                return int(baseline["run_count"])
        except Exception:
            pass
    return 0


def _exec_summary_prompt(opps: List[Dict[str, Any]], sources_analyzed: Dict[str, Any],
                          pack_id: Optional[str] = None) -> str:
    """ENG-AIQ-NC-5: pack-aware executive summary prompt."""
    from discovery.packs.pack_config import is_ncino_pack, is_strs_benefits_pack
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

    elif is_strs_benefits_pack(pack_id):
        return f"""You are writing a one-paragraph executive summary for a STRS Executive Director or Board of Trustees.

## Discovery Context
Sources analyzed: {sources_analyzed.get("totalConnected", 0)} connected systems (Salesforce PSS, Jira, ServiceNow)
Top benefit administration friction patterns:
{opp_lines}

## Instructions
Write exactly one paragraph (3-5 sentences) for a pension fund executive audience.
- Use member services language: retirement applications, benefit elections, disbursements, disability reviews
- Open with the most significant member-impact finding
- Reference ORC 3307 if any compliance-override pattern is present in the findings
- Include projected outcome using "could reduce" / "estimated" language
- Close with a recommended first step — always framing the agent as surfacing alerts to staff, never making autonomous decisions
- Tone: measured, precise, focused on member outcomes and regulatory obligations

Write the paragraph directly. No headings, no bullet points, no markdown."""

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
# Model gateway caller (R16-D1 T3)
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int) -> Optional[str]:
    # Route through the gateway's instrumented generate() so the call is
    # telemetered with the serving provider (R16-D1 T5). text=None on failure
    # is preserved — callers already handle None.
    from app.model_gateway import GenerationRequest, generate

    result = generate(GenerationRequest(prompt=prompt, max_tokens=max_tokens))
    return result.text


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
    from discovery.packs.pack_config import is_ncino_pack, is_strs_benefits_pack
    if is_strs_benefits_pack(pack_id) and not opp.get("aiRationale"):
        title = opp.get("title", "Benefit administration pattern detected")
        summary = (
            f"{title}. Connect to the Anthropic API to generate a full member services "
            f"analysis with evidence-backed insights for this pension administration pattern."
        )
    elif is_ncino_pack(pack_id) and not opp.get("aiRationale"):
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
    causal_context: Optional[Any] = None,
) -> Dict[str, Any]:
    opp_id   = opp.get("id", "unknown")
    fb       = _fallback(opp, pack_id=pack_id)
    prompt   = _opp_prompt(opp, evidence, pack_id=pack_id, causal_context=causal_context)
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

    result = {
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
    if causal_context is not None:
        result["_causal_llm_response"] = parsed
    return result


def _enrich_opportunity_grounded(
    opp: Dict[str, Any],
    graph_context: "Any",
    pack_id: Optional[str],
    org_id: Optional[str],
    run_id: str,
    corroboration_label: Optional[str],
    causal_context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Graph-grounded first pass + hallucination guard for one opportunity.

    Builds the four-section prompt (Section 2), calls the LLM, then runs each
    ``why`` bullet through the hallucination guard (Section 3), preserving the
    [OBSERVED]/[INFERRED] tag across any rewrite and dropping bullets the guard
    returns as None. The guard's rewrite/removal counts are collected into the
    returned dict for the OppEnrichment fields (T5). Never raises — on any
    failure it returns the deterministic fallback with zeroed guard counts.
    """
    from .hallucination_guard import GuardStats, validate_and_recover

    opp_id = opp.get("id", "unknown")
    stats = GuardStats()
    guard_fields = {
        "hallucination_rewrites": 0,
        "hallucination_llm_rewrites": 0,
        "hallucination_removals": [],
    }

    try:
        from discovery.packs.pack_config import get_llm_context

        pack_llm_context = get_llm_context(pack_id) if pack_id else ""
    except Exception:
        pack_llm_context = ""

    org_name = org_id or "your organisation"
    signal_ctx = _build_signal_context(opp, corroboration_label)
    prompt = build_grounded_opp_prompt(
        signal_ctx,
        graph_context,
        pack_llm_context,
        org_name,
        causal_context=causal_context,
    )

    fb = _fallback(opp, pack_id=pack_id)
    fb.update(guard_fields)

    raw = _call_claude(prompt, MAX_TOKENS_OPP)
    if raw is None:
        return fb

    parsed = _parse_json(raw)
    if parsed is None or not _validate_opp_fields(parsed, opp_id):
        logger.warning("Opp %s: grounded response invalid — using fallback", opp_id)
        return fb

    # Hallucination guard: validate every why bullet against resolved names.
    # The leading tag is split off so proper-noun extraction never sees it, then
    # re-attached to whatever the guard returns. A guard failure on one bullet
    # degrades to dropping that bullet — it never fails the enrichment.
    resolved_names = graph_context.resolved_names
    clean_bullets: List[str] = []
    for bullet in parsed["aiWhyBullets"][:5]:
        tag, body = _split_observation_tag(str(bullet))
        # Enforce a recognised provenance tag: if the LLM omitted one, fall back
        # to [UNVERIFIED] so the re-attached prefix is never empty/malformed and
        # the frontend always receives a consistently-tagged bullet (review #2).
        if not tag:
            tag = _UNVERIFIED_TAG
        try:
            recovered = validate_and_recover(body, resolved_names, org_id, run_id, stats=stats)
        except Exception as exc:  # defensive — drop the bullet, keep the run alive
            logger.warning("Opp %s: guard error on a bullet (%s) — dropping", opp_id, exc)
            stats.removals.append("dropped_error")
            recovered = None
        if recovered is None:
            continue
        clean_bullets.append(f"{tag}{recovered}".strip())

    guard_fields = {
        "hallucination_rewrites": stats.rule_rewrites,
        "hallucination_llm_rewrites": stats.llm_rewrites,
        "hallucination_removals": list(stats.removals),
    }

    result = {
        "aiSummary": parsed["aiSummary"],
        "aiWhyBullets": clean_bullets,
        "aiRisks": [str(b) for b in parsed["aiRisks"][:3]],
        "aiSuggestedNextSteps": [str(b) for b in parsed["aiSuggestedNextSteps"][:3]],
        "llmGenerated": True,
        "llmModel": MODEL,
        "complianceGuardrailApplied": pack_id == "ncino",
    }
    if causal_context is not None:
        result["_causal_llm_response"] = parsed
    result.update(guard_fields)
    return result


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
# T3-S16-A — causal context assembly (best-effort, never raises)
# ─────────────────────────────────────────────────────────────────────────────

def _try_build_causal_context_legacy(
    org_id: Optional[str],
    opp: Dict[str, Any],
    pack_id: Optional[str],
) -> Optional[Any]:
    """Try to build a CausalContext for one opportunity. Returns None on any failure.

    Gracefully handles:
    - causal_engine not yet available (ENT-4 / T2 not merged)
    - InsufficientGraphContextError (< 3 entities in neighbourhood)
    - Any other unexpected exception

    When ANTHROPIC_API_KEY is unset, _call_claude() already returns None so the
    causal section is never rendered even if this returns a context — but we
    still return None early to avoid an unnecessary DB traversal.
    """
    if not org_id:
        return None
    if not os.getenv("ANTHROPIC_API_KEY", ""):
        return None

    try:
        from .causal_engine import InsufficientGraphContextError, build_causal_context
    except ImportError:
        # causal_engine not yet merged — silent degradation
        return None

    opp_id = opp.get("id", "")
    # Seed entity IDs: prefer an explicit list on the opp; fall back to the opp
    # id itself as a single seed (which will usually yield InsufficientGraphContextError
    # unless the graph is rich enough around this opportunity's record).
    seed_ids: list = opp.get("entity_ids") or opp.get("entityIds") or []
    if not seed_ids and opp_id:
        seed_ids = [opp_id]

    effective_pack = pack_id or "service_cloud"

    try:
        return build_causal_context(org_id, opp_id, seed_ids, effective_pack)
    except InsufficientGraphContextError:
        logger.debug(
            "Causal context skipped for opp=%s: insufficient graph context", opp_id
        )
        return None
    except Exception as exc:
        logger.debug("Causal context build failed for opp=%s: %s", opp_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ENT-6 runtime bridge helpers used by run_llm_enrichment().

def _entity_id_from_summary(entity: Any) -> Optional[str]:
    if not entity:
        return None
    if isinstance(entity, str):
        return entity
    if isinstance(entity, dict):
        value = entity.get("entity_id") or entity.get("id")
    else:
        value = getattr(entity, "entity_id", None) or getattr(entity, "id", None)
    return str(value) if value else None


def _dedupe(values: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _explicit_opp_entity_ids(opp: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for key in ("entity_ids", "entityIds", "entities"):
        value = opp.get(key)
        if isinstance(value, list):
            ids.extend(
                entity_id
                for entity_id in (_entity_id_from_summary(item) for item in value)
                if entity_id
            )
    return _dedupe(ids)


def _run_entity_ids_from_db(org_id: str, run_id: str) -> List[str]:
    try:
        from . import db as _db

        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id
                FROM entities
                WHERE org_id = %s AND last_seen_run_id = %s
                ORDER BY display_name ASC, id ASC
                LIMIT 50
                """,
                (org_id, run_id),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Causal seed entity DB lookup failed for run=%s: %s", run_id, exc)
        return []
    return _dedupe([str(row[0]) for row in rows if row and row[0]])


def _run_relationship_entity_ids_from_db(org_id: str, run_id: str) -> List[str]:
    """Return endpoints of relationships confirmed in the current run."""
    try:
        from . import db as _db

        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT from_entity_id AS entity_id
                FROM entity_relationships
                WHERE org_id = %s AND last_seen_run_id = %s
                UNION
                SELECT to_entity_id AS entity_id
                FROM entity_relationships
                WHERE org_id = %s AND last_seen_run_id = %s
                ORDER BY entity_id
                LIMIT 50
                """,
                (org_id, run_id, org_id, run_id),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Causal relationship seed lookup failed for run=%s: %s", run_id, exc)
        return []
    return _dedupe([str(row[0]) for row in rows if row and row[0]])


def _detector_process_entity_id(
    org_id: str,
    run_id: str,
    detector_id: Optional[str],
) -> Optional[str]:
    if not detector_id:
        return None
    try:
        from . import db as _db

        conn = _db.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id
                FROM entities
                WHERE org_id = %s
                  AND last_seen_run_id = %s
                  AND entity_type = 'process'
                  AND (
                        lower(display_name) = lower(%s)
                     OR lower(COALESCE(source_record_id, '')) = lower(%s)
                  )
                ORDER BY id
                LIMIT 1
                """,
                (org_id, run_id, detector_id, detector_id),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Causal detector seed lookup failed for run=%s: %s", run_id, exc)
        return None
    return str(row[0]) if row and row[0] else None


def _opportunity_detector_id(opp: Dict[str, Any]) -> Optional[str]:
    """Read the detector ID from runner or persisted Track A opportunity shapes."""
    debug = opp.get("_debug") if isinstance(opp.get("_debug"), dict) else {}
    value = (
        opp.get("detector_id")
        or opp.get("detectorId")
        or debug.get("detector_id")
        or debug.get("detectorId")
    )
    detector_id = str(value).strip() if value is not None else ""
    return detector_id or None


def _causal_seed_entity_ids(
    org_id: str,
    run_id: str,
    opp: Dict[str, Any],
    graph_entities: List[Dict[str, Any]],
) -> List[str]:
    explicit = _explicit_opp_entity_ids(opp)
    if explicit:
        return explicit

    relationship_ids = _run_relationship_entity_ids_from_db(org_id, run_id)
    detector_entity_id = _detector_process_entity_id(
        org_id,
        run_id,
        _opportunity_detector_id(opp),
    )
    # Always seed with ALL relationship endpoints so depth-3 BFS sees the full
    # connected component, not just the single detector process entity. A single
    # seed can reach only direct neighbours; multiple co-located seeds pool their
    # neighbourhoods, letting chains like A→B and C→A surface 3-entity context.
    if relationship_ids:
        if detector_entity_id and detector_entity_id in relationship_ids:
            # Put detector entity first so causal context stays opportunity-focused.
            seeds = _dedupe([detector_entity_id] + relationship_ids)
        else:
            seeds = relationship_ids
        return seeds

    from_kv = [
        entity_id
        for entity_id in (_entity_id_from_summary(entity) for entity in graph_entities)
        if entity_id
    ]
    # The run KV is filtered for UI display (for example run_count >= 3), while
    # causal analysis needs graph-complete seeds. Include DB rows last seen in
    # this run so newly-created process entities can anchor relationship edges.
    from_db = _run_entity_ids_from_db(org_id, run_id)
    return _dedupe((from_kv + from_db)[:50])


def _record_causal_rejection(
    reason: str,
    org_id: Optional[str],
    run_id: str,
    opportunity_id: str,
) -> None:
    if not org_id:
        return
    try:
        from .telemetry import record_event

        record_event(
            "causal.hypothesis_rejected",
            {
                "reason": reason,
                "org_id": org_id,
                "run_id": run_id,
                "opportunity_id": opportunity_id,
            },
        )
    except Exception as exc:
        logger.debug("causal.hypothesis_rejected telemetry failed: %s", exc)


def _try_build_causal_context(
    org_id: Optional[str],
    run_id: str,
    opp: Dict[str, Any],
    pack_id: Optional[str],
    graph_entities: List[Dict[str, Any]],
) -> Optional[Any]:
    if not org_id:
        return None
    if not os.getenv("ANTHROPIC_API_KEY", ""):
        return None

    try:
        from .causal_engine import InsufficientGraphContextError, build_causal_context
    except ImportError:
        return None

    opp_id = opp.get("id", "")
    seed_ids = _causal_seed_entity_ids(org_id, run_id, opp, graph_entities)
    if not seed_ids:
        _record_causal_rejection("insufficient_graph_context", org_id, run_id, opp_id)
        return None

    try:
        return build_causal_context(org_id, opp_id, seed_ids, pack_id or "service_cloud")
    except InsufficientGraphContextError:
        logger.debug(
            "Causal context skipped for opp=%s: insufficient graph context", opp_id
        )
        _record_causal_rejection("insufficient_graph_context", org_id, run_id, opp_id)
        return None
    except Exception as exc:
        logger.debug("Causal context build failed for opp=%s: %s", opp_id, exc)
        return None


def _primary_signal_key_for_opp(
    opp: Dict[str, Any],
    pack_id: Optional[str],
) -> Optional[str]:
    signal_key = opp.get("signal_key")
    if isinstance(signal_key, str) and signal_key.strip():
        return signal_key.strip()
    debug = opp.get("_debug", {}) if isinstance(opp.get("_debug"), dict) else {}
    debug_signal_key = debug.get("signal_key")
    if isinstance(debug_signal_key, str) and debug_signal_key.strip():
        return debug_signal_key.strip()
    detector_id = debug.get("detector_id") or opp.get("detector_id")
    if pack_id and detector_id:
        return f"{pack_id}::{detector_id}::metric_value"
    return None


def _context_entity_ids(causal_context: Any) -> List[str]:
    graph = getattr(causal_context, "graph_context", None)
    entities = getattr(graph, "entities", []) if graph is not None else []
    return sorted(
        {
            str(getattr(entity, "entity_id", "")).strip()
            for entity in entities
            if str(getattr(entity, "entity_id", "")).strip()
        }
    )


def _maybe_store_causal_hypothesis(
    *,
    org_id: Optional[str],
    run_id: str,
    opp: Dict[str, Any],
    pack_id: Optional[str],
    graph_entities: List[Dict[str, Any]],
    causal_context: Optional[Any],
    llm_response: Optional[Dict[str, Any]],
) -> None:
    if not org_id or causal_context is None or not llm_response:
        return

    opportunity_id = str(opp.get("id") or "")
    if not opportunity_id:
        return

    try:
        from .causal_engine import (
            evaluate_causal_quality_gates,
            parse_causal_output,
            store_causal_hypothesis,
        )

        parsed = parse_causal_output(
            llm_response,
            org_id=org_id,
            run_id=run_id,
            opportunity_id=opportunity_id,
            causal_context=causal_context,
        )
        if parsed is None:
            return

        gate_payload = {
            **parsed,
            "evidence_links": _context_entity_ids(causal_context),
            "org_id": org_id,
        }
        gate_result = evaluate_causal_quality_gates(
            gate_payload,
            _primary_signal_key_for_opp(opp, pack_id),
            opportunity_id,
            {"entities": graph_entities, "org_id": org_id},
            causal_context,
        )
        store_causal_hypothesis(
            org_id,
            opportunity_id,
            run_id,
            parsed,
            gate_result,
            causal_context,
        )
    except Exception as exc:
        logger.warning(
            "Causal hypothesis storage skipped for opp=%s run=%s: %s",
            opportunity_id,
            run_id,
            exc,
        )


# Main enrichment runner
# ─────────────────────────────────────────────────────────────────────────────

def run_llm_enrichment(
    run_id: str,
    opps: List[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
    sources_analyzed: Optional[Dict[str, Any]] = None,
    pack_id: Optional[str] = None,
    org_id: Optional[str] = None,
    allow_graph_context: bool = True,
) -> Dict[str, Any]:
    """
    ENG-AIQ-NC-5: pack_id parameter added.
    When pack_id='ncino', uses banking-language prompts and nCino UI labels.
    When pack_id='service_cloud' or None, uses original SC prompts unchanged.

    ENT-3 / T3-S15-A: org_id parameter added to enable graph-grounded
    enrichment. When the run's ENT-4 graph holds >= 3 entities, the first pass
    uses the four-section grounded prompt, every why bullet is run through the
    hallucination guard, and a preliminary quality gate is evaluated. When the
    graph is sparse (< 3 entities) or no org_id is supplied, the pipeline falls
    back to the pre-ENT-3 prompt with llm_grounded=False and no guard (AC10).
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

    # ENT-3: build the run's graph context once. Deterministic, never raises —
    # an empty/sparse context routes every opportunity to the fallback path.
    # R16-B2 (T5/AC8): build_graph_context() now selects that context through the
    # context assembly service, so enrichment no longer picks its own context
    # independently — the budget/ranking/observed-first policy is applied in one
    # place. Enrichment consumes the already-selected context unchanged.
    from . import db
    from .graph_context import build_graph_context
    from .enrichment_quality import evaluate_preliminary_status

    try:
        graph_entities = (
            db.run_kv_get("entities", run_id, []) or []
            if allow_graph_context
            else []
        )
    except Exception:
        graph_entities = []
    graph_context = build_graph_context(
        org_id if allow_graph_context else None,
        run_id,
        entities=graph_entities,
        relationships=None if allow_graph_context else [],
    )
    grounded = bool(org_id) and not graph_context.is_sparse

    graph_fields = {
        "llm_grounded": grounded,
        "graph_entity_count": graph_context.entity_count,
        "graph_entity_count_shown": graph_context.entity_count_shown,
        "graph_truncated": graph_context.truncated,
    }
    logger.info(
        "T6 enrichment run %s: grounded=%s entities=%d shown=%d truncated=%s",
        run_id, grounded, graph_context.entity_count,
        graph_context.entity_count_shown, graph_context.truncated,
    )

    per_opp: Dict[str, Any] = {}
    enriched = 0
    failed   = 0

    # R16-B1: resolve the run's observation time ONCE so every enrichment
    # provenance pointer in this run shares one deterministic source_timestamp
    # (keeps the no-LLM pipeline output reproducible — S15 contract).
    enrichment_source_ts = _resolve_run_observed_at(run_id)

    for opp in opps:
        opp_id = opp.get("id", "")
        corroboration_label = _corroboration_for(opp)
        # T3-S16-A: attempt to build causal context for this opportunity.
        # Returns None when causal_engine is absent, graph is sparse, or no API key.
        causal_context = _try_build_causal_context(
            org_id,
            run_id,
            opp,
            pack_id,
            graph_entities,
        )
        try:
            if grounded:
                result = _enrich_opportunity_grounded(
                    opp,
                    graph_context,
                    pack_id,
                    org_id,
                    run_id,
                    corroboration_label,
                    causal_context=causal_context,
                )
                # Mark this opportunity's first pass as graph-grounded.
                try:
                    from .telemetry import record_event

                    record_event(
                        "llm.enrichment_grounded",
                        {
                            "org_id": org_id,
                            "run_id": run_id,
                            "opp_id": opp_id,
                            "graph_entity_count": graph_context.entity_count,
                            "graph_entity_count_shown": graph_context.entity_count_shown,
                            "graph_truncated": graph_context.truncated,
                            "source": "llm_enrichment",
                        },
                    )
                except Exception as exc:
                    logger.debug("llm.enrichment_grounded telemetry failed: %s", exc)
            else:
                # Sparse graph or no org context — pre-ENT-3 prompt, no guard.
                result = _enrich_opportunity(opp, evidence, pack_id=pack_id,
                                             causal_context=causal_context)
                result.setdefault("hallucination_rewrites", 0)
                result.setdefault("hallucination_llm_rewrites", 0)
                result.setdefault("hallucination_removals", [])

            causal_llm_response = result.pop("_causal_llm_response", None)
            _maybe_store_causal_hypothesis(
                org_id=org_id,
                run_id=run_id,
                opp=opp,
                pack_id=pack_id,
                graph_entities=graph_entities,
                causal_context=causal_context,
                llm_response=causal_llm_response,
            )

            # Shared fields (both paths): graph shape + corroboration.
            result.update(graph_fields)
            result["corroboration_label"] = corroboration_label

            # Preliminary quality gate (T4) — after the guard, before storing.
            run_count = _resolve_run_count(org_id, opp)
            preliminary, reason = evaluate_preliminary_status(
                {"entities": graph_entities}, run_count, org_id
            )
            result["preliminary"] = preliminary
            result["preliminary_reason"] = reason

            _attach_enrichment_provenance(
                result, run_id=run_id, opp=opp, source_timestamp=enrichment_source_ts
            )
            per_opp[opp_id] = result
            if result.get("llmGenerated"):
                enriched += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Opp %s error: %s", opp_id, e)
            fallback = _fallback(opp, pack_id=pack_id)
            fallback.update(graph_fields)
            fallback["llm_grounded"] = False
            fallback["hallucination_rewrites"] = 0
            fallback["hallucination_llm_rewrites"] = 0
            fallback["hallucination_removals"] = []
            fallback["corroboration_label"] = corroboration_label
            fallback["preliminary"] = True
            fallback["preliminary_reason"] = "Enrichment failed — analyst review required"
            _attach_enrichment_provenance(
                fallback, run_id=run_id, opp=opp, source_timestamp=enrichment_source_ts
            )
            per_opp[opp_id] = fallback
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
