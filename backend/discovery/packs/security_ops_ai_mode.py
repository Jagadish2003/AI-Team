"""
security_ops_ai_mode.py — MSP-B12 T4: the explicit AI-mode gate for the Security
Operations pack.

Security-derived content participates in AI-assisted context assembly ONLY when the
active model-provider mode keeps that content inside a controlled boundary:

  * ``in_boundary``     — the model runs inside the controlled deployment boundary,
                          so deterministic detector output MAY drive full assembly.
  * ``customer_tenant`` — model processing occurs through the customer's own
                          infrastructure/credentials, so full assembly is allowed.
  * ``hosted``          — the five deterministic detectors still run and their
                          findings remain available, but Security-Operations data is
                          NEVER sent for AI narrative generation. Each finding carries
                          an explicit label; the restriction is stated, never a
                          silent empty narrative or a quietly reduced finding.

The gate reads the ACTIVE generation-provider mode already exposed by the AgentIQ
model gateway (``MODEL_GENERATION_PROVIDER`` → ``get_generation_provider().name``);
it introduces no new provider concept and never bypasses the gateway. In the two
permitted modes, narrative generation still flows through the gateway's instrumented
``generate()`` (existing routing + no-bypass controls intact); in hosted mode the
narrative call is simply not made for SecOps content.

Vulnerability workload data does not leave the boundary for AI, ever — for federal
deployments that is the selling point, not a limitation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# The explicit hosted-mode label (MSP-B12 AC4). Verbatim — the restriction must be
# stated on the finding, never represented as an unexplained empty narrative.
HOSTED_NARRATIVE_LABEL = "AI-assisted narrative unavailable in this mode."

MODE_HOSTED = "hosted"
MODE_IN_BOUNDARY = "in_boundary"
MODE_CUSTOMER_TENANT = "customer_tenant"

# The only modes in which security-derived content may participate in AI assembly.
AI_ASSEMBLY_MODES = frozenset({MODE_IN_BOUNDARY, MODE_CUSTOMER_TENANT})


def active_ai_mode() -> str:
    """Return the active AI (generation) mode from the model gateway.

    Resolves ``get_generation_provider().name`` — the provider selected by
    ``MODEL_GENERATION_PROVIDER``. On any resolution failure it fails SAFE to
    ``hosted`` (the most restrictive mode: no AI for SecOps content), so a
    misconfiguration can never accidentally send vulnerability data to AI.
    """
    try:
        try:
            from backend.app.model_gateway import get_generation_provider
        except ModuleNotFoundError:  # pragma: no cover - import shim
            from app.model_gateway import get_generation_provider
        return str(get_generation_provider().name)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("security_ops AI-mode gate: provider resolution failed (%s); "
                       "defaulting to hosted (no AI for SecOps)", exc)
        return MODE_HOSTED


def ai_assembly_allowed(mode: Optional[str] = None) -> bool:
    """True when SecOps content may drive full AI assembly in ``mode``.

    ``mode`` defaults to the active gateway mode. Only ``in_boundary`` and
    ``customer_tenant`` permit assembly; ``hosted`` (and anything unrecognised)
    does not.
    """
    return (mode or active_ai_mode()) in AI_ASSEMBLY_MODES


def ai_narrative_blocked_for_pack(pack_id: Optional[str], mode: Optional[str] = None) -> bool:
    """True when AI narrative must be withheld for this pack in this mode.

    Blocked exactly when the pack is Security Operations AND assembly is not
    permitted in the active mode (i.e. hosted). Other packs are unaffected.
    """
    try:
        from backend.discovery.packs.pack_config import is_security_ops_pack
    except ModuleNotFoundError:  # pragma: no cover - import shim
        from discovery.packs.pack_config import is_security_ops_pack
    return is_security_ops_pack(pack_id) and not ai_assembly_allowed(mode)


# ── Finding-level label stamping (applied at the pack boundary) ──────────────────


def _raw_of(result: Any) -> Optional[Dict[str, Any]]:
    raw = getattr(result, "raw_evidence", None)
    if raw is None and isinstance(result, dict):
        raw = result
    return raw if isinstance(raw, dict) else None


def apply_ai_mode_gate(results: Any, mode: Optional[str] = None) -> Dict[str, Any]:
    """Stamp the AI-mode disposition onto every emitted finding.

    In a permitted mode: ``ai_narrative_available=True`` (findings are eligible for
    full assembly). In hosted mode: ``ai_narrative_available=False`` plus the
    explicit :data:`HOSTED_NARRATIVE_LABEL` — on both the raw payload and the
    four-part contract's evidence, so the restriction is visible on the finding
    surface, never silent. Returns a summary for the run log. The deterministic
    findings themselves are untouched and remain available in every mode.
    """
    mode = mode or active_ai_mode()
    allowed = mode in AI_ASSEMBLY_MODES
    labelled = 0
    count = 0
    for result in results or []:
        raw = _raw_of(result)
        if raw is None:
            continue
        count += 1
        raw["ai_mode"] = mode
        raw["ai_narrative_available"] = allowed
        contract = raw.get("finding_contract")
        evidence = contract.get("evidence") if isinstance(contract, dict) else None
        if not allowed:
            raw["ai_mode_label"] = HOSTED_NARRATIVE_LABEL
            labelled += 1
            if isinstance(evidence, dict):
                evidence["ai_narrative_available"] = False
                evidence["ai_mode_label"] = HOSTED_NARRATIVE_LABEL
        else:
            raw.pop("ai_mode_label", None)
            if isinstance(evidence, dict):
                evidence["ai_narrative_available"] = True
                evidence.pop("ai_mode_label", None)
    return {"mode": mode, "ai_assembly_allowed": allowed, "labelled": labelled, "count": count}


# ── Narrative assembly gate (the single decision point for outbound AI) ──────────


def assemble_narrative(
    findings: List[Any],
    *,
    generate_fn: Callable[[Any], Any],
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce AI narrative for SecOps findings ONLY in a permitted mode.

    ``generate_fn`` is the outbound AI call (in production the model gateway's
    ``generate()``). In hosted mode it is NEVER invoked — no request carrying
    Security-Operations data leaves the boundary — and a labelled, AI-free result is
    returned. In ``in_boundary`` / ``customer_tenant`` it is invoked per finding
    (full assembly). This is the single decision point the enrichment path consults,
    which makes the "no outbound AI in hosted mode" guarantee directly testable.
    """
    mode = mode or active_ai_mode()
    if mode not in AI_ASSEMBLY_MODES:
        return {
            "mode": mode,
            "ai_assembled": False,
            "ai_narrative_available": False,
            "label": HOSTED_NARRATIVE_LABEL,
            "narratives": {},
        }
    narratives: Dict[str, Any] = {}
    for i, finding in enumerate(findings or []):
        raw = _raw_of(finding) or {}
        key = str(raw.get("finding_ref") or i)
        narratives[key] = generate_fn(finding)
    return {
        "mode": mode,
        "ai_assembled": True,
        "ai_narrative_available": True,
        "narratives": narratives,
    }


def hosted_enrichment_result(mode: Optional[str] = None) -> Dict[str, Any]:
    """The labelled, AI-free enrichment record persisted for a hosted SecOps run.

    Carries the explicit label (snake- and camel-case for the API/FE) and no
    AI-generated narrative — deterministic findings remain the payload, the
    restriction is stated, and NO model call was made.
    """
    return {
        "llm_enriched": False,
        "ai_narrative_available": False,
        "ai_mode": mode or active_ai_mode(),
        "ai_mode_label": HOSTED_NARRATIVE_LABEL,
        "aiModeLabel": HOSTED_NARRATIVE_LABEL,
        "opportunities": {},
    }
