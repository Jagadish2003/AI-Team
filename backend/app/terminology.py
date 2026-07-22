"""
terminology.py — R18-C1 T4: template-driven domain terminology at serve time

When a Stack Builder template is active on a run, the user-facing WORDING across
findings, roadmap, blueprint/agent-recommendation copy, and executive reporting
should speak the template's domain language. For the Commercial Lending template
that means generic automation wording becomes lending wording: customer →
borrower, account → facility, obligation → covenant, rationale → credit memo,
approval → approval gate (the `terminology` dict declared on the template in
discovery/packs/template_registry.py).

Design (matches the R18-C1 scope principle — templates are bundles of editable
defaults, never forks of the engine):

  * SERVE-TIME, not materialize-time. This is a pure read-time rewrite of string
    VALUES on the response, mirroring the existing `opportunity_display.with_display`
    precedent. Detector logic, scoring, and the stored KV artifacts (`opps`,
    `roadmap`, `executive_report`, `llm_enrichment`) are never mutated — the same
    operational issue is detected; only the final wording shown to the user is
    adapted. Re-runs and replays stay byte-stable.

  * CONFIG-DRIVEN, not scattered per-page strings. The word map lives on the
    template (registry), so a future template brings its own business language
    with no code change here. A run with no template — or a template whose
    terminology dict is empty (service_operations / revenue_operations) — yields
    an empty map and the transform is a guaranteed no-op (backward compatible).

  * CONTRACT-SAFE. Only string values under an explicit allowlist of narrative
    fields are rewritten. Dict KEYS are never touched (field names/shapes are
    preserved), and technical/enum/identifier fields (Salesforce object API
    names, detectorId, tier, decision, confidence, evidenceIds, permission API
    labels, …) are deliberately NOT in the allowlist, so terminology can never
    corrupt them.

Public API:
  resolve_run_terminology(run_id) -> dict[str, str]   # the run's active map ({} if none)
  apply_terminology(obj, terminology) -> obj          # deep, allowlist-scoped rewrite
  rewrite_text(text, terminology) -> str              # single-string convenience
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# ── User-facing narrative fields eligible for rewriting ───────────────────────
# Only string values stored under these keys (directly, or as items of a list
# under one of these keys, e.g. aiWhyBullets) are rewritten. Everything else is
# recursed-into (to reach nested opportunity objects) but left verbatim.
TERMINOLOGY_TEXT_FIELDS = frozenset(
    {
        # opportunity / finding narrative
        "title",
        "category",
        "description",
        "s9_roadmap",
        "s10_exec",
        "aiRationale",
        "compliance_guardrail",
        # LLM enrichment narrative
        "aiSummary",
        "aiWhyBullets",
        "aiRisks",
        "aiSuggestedNextSteps",
        "corroboration_label",
        "preliminary_reason",
        "executiveSummary",
        "aiExecutiveSummary",
        # roadmap stage narrative
        "summary",
        # blueprint / agent recommendation narrative
        "agentName",
        "agentTopic",
        "action",
        "detail",
        "guardrails",
    }
)


# ── Pluralisation (small, domain-correct) ─────────────────────────────────────
def _pluralize(word: str) -> str:
    """Pluralise the last token of a (possibly multi-word) term.

    Handles the irregular y→ies case (facility → facilities) and the common
    -s/-x/-z/-ch/-sh → -es case, so both the generic and domain plural forms are
    right (accounts → facilities, approvals → approval gates, credit memo →
    credit memos)."""
    if not word:
        return word
    head, _, last = word.rpartition(" ")
    if not last:
        return word
    lower = last.lower()
    if lower.endswith("y") and len(lower) >= 2 and lower[-2] not in "aeiou":
        plural_last = last[:-1] + "ies"
    elif lower.endswith(("s", "x", "z", "ch", "sh")):
        plural_last = last + "es"
    else:
        plural_last = last + "s"
    return f"{head} {plural_last}" if head else plural_last


def _match_case(source: str, replacement: str) -> str:
    """Case-preserve the replacement against the matched source word."""
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _build_pattern(
    terminology: Dict[str, str]
) -> Tuple[Optional["re.Pattern[str]"], Dict[str, str]]:
    """Compile a whole-word, case-insensitive matcher over the generic terms
    (singular + plural), plus a lookup of matched-term → domain replacement."""
    expanded: Dict[str, str] = {}
    for generic, domain in terminology.items():
        g = (generic or "").strip()
        d = (domain or "").strip()
        if not g or not d:
            continue
        expanded[g.lower()] = d
        expanded[_pluralize(g).lower()] = _pluralize(d)
    if not expanded:
        return None, {}
    # Longest first so a plural ("accounts") is preferred over its singular.
    terms = sorted(expanded.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b",
        re.IGNORECASE,
    )
    return pattern, expanded


def _rewrite(text: str, pattern: "re.Pattern[str]", expanded: Dict[str, str]) -> str:
    def _repl(match: "re.Match[str]") -> str:
        matched = match.group(0)
        replacement = expanded.get(matched.lower())
        if replacement is None:
            return matched
        return _match_case(matched, replacement)

    return pattern.sub(_repl, text)


def _walk(
    obj: Any,
    pattern: "re.Pattern[str]",
    expanded: Dict[str, str],
    *,
    in_text_field: bool = False,
) -> Any:
    if isinstance(obj, dict):
        # Keys are never rewritten — shapes/field names are preserved. Each value
        # is rewritten only when its own key is an allowlisted narrative field.
        return {
            key: _walk(
                value,
                pattern,
                expanded,
                in_text_field=key in TERMINOLOGY_TEXT_FIELDS,
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_walk(v, pattern, expanded, in_text_field=in_text_field) for v in obj]
    if isinstance(obj, str):
        return _rewrite(obj, pattern, expanded) if in_text_field else obj
    return obj


def apply_terminology(obj: Any, terminology: Optional[Dict[str, str]]) -> Any:
    """Return a copy of ``obj`` with narrative string values rewritten to the
    template's domain language. No-op (returns ``obj`` unchanged) when the map is
    empty/None. Dict keys, numbers, booleans, enums, and non-allowlisted string
    fields are left untouched."""
    if not terminology:
        return obj
    pattern, expanded = _build_pattern(terminology)
    if pattern is None:
        return obj
    return _walk(obj, pattern, expanded)


def rewrite_text(text: Optional[str], terminology: Optional[Dict[str, str]]) -> Optional[str]:
    """Rewrite a single narrative string (convenience for call sites that hold a
    lone string rather than a dict). No-op on empty map/None text."""
    if not text or not terminology:
        return text
    pattern, expanded = _build_pattern(terminology)
    if pattern is None:
        return text
    return _rewrite(text, pattern, expanded)


def resolve_run_terminology(run_id: str) -> Dict[str, str]:
    """Resolve the terminology map for a run's ACTIVE template.

    Reads the template id recorded on the run at launch (run record `templateId`,
    with the `setup_context` KV as a fallback), then returns that template's
    `terminology` dict from the registry. Returns ``{}`` when the run has no
    template or the template declares no terminology — the safe no-op default.
    Never raises: a lookup problem degrades to an empty map (generic wording)."""
    try:
        from . import db
    except ImportError:  # pragma: no cover - project-root execution
        import app.db as db  # type: ignore

    try:
        run = db.get_run(run_id) or {}
    except Exception:
        return {}

    terminology_by_pack = resolve_run_terminology_by_pack(run_id, run=run)
    if len(terminology_by_pack) == 1:
        return dict(next(iter(terminology_by_pack.values())))
    # A combined run has intentionally separate vocabulary. Returning the
    # primary map here would incorrectly relabel another pack, so legacy callers
    # receive the safe no-op and pack-aware callers use apply_run_terminology.
    if len(terminology_by_pack) > 1:
        return {}

    template_id = run.get("templateId")
    if not template_id:
        try:
            ctx = db.run_kv_get("setup_context", run_id, {}) or {}
        except Exception:
            ctx = {}
        template_id = ctx.get("template_id")

    if not template_id:
        return {}

    try:
        from discovery.packs.template_registry import get_template
    except ModuleNotFoundError:  # pragma: no cover
        from backend.discovery.packs.template_registry import get_template  # type: ignore

    defn = get_template(template_id)
    if defn and getattr(defn, "terminology", None):
        return dict(defn.terminology)
    return {}


def resolve_run_terminology_by_pack(
    run_id: str,
    *,
    run: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, str]]:
    """Return immutable launch terminology keyed by pack ID."""
    try:
        from . import db
    except ImportError:  # pragma: no cover
        import app.db as db  # type: ignore

    if run is None:
        try:
            run = db.get_run(run_id) or {}
        except Exception:
            return {}

    provenance = run.get("templateProvenance") or {}
    boundaries = provenance.get("pack_boundaries") or run.get("packBoundaries")
    if not boundaries:
        try:
            context = db.run_kv_get("setup_context", run_id, {}) or {}
        except Exception:
            context = {}
        boundaries = context.get("pack_boundaries") or (
            context.get("template_provenance") or {}
        ).get("pack_boundaries")

    result: Dict[str, Dict[str, str]] = {}
    for boundary in boundaries or []:
        if not isinstance(boundary, dict):
            continue
        pack_id = str(boundary.get("pack_id") or "").strip()
        terminology = boundary.get("terminology")
        if pack_id and isinstance(terminology, dict):
            result[pack_id] = dict(terminology)
    return result


def apply_run_terminology(obj: Any, run_id: str) -> Any:
    """Apply the correct template vocabulary to each pack-owned result."""
    terminology_by_pack = resolve_run_terminology_by_pack(run_id)
    if not terminology_by_pack:
        return apply_terminology(obj, resolve_run_terminology(run_id))
    if len(terminology_by_pack) == 1:
        return apply_terminology(obj, next(iter(terminology_by_pack.values())))

    def _apply(value: Any) -> Any:
        if isinstance(value, list):
            return [_apply(item) for item in value]
        if isinstance(value, dict):
            pack_id = value.get("packId") or value.get("pack_id")
            terminology = terminology_by_pack.get(str(pack_id))
            if terminology:
                return apply_terminology(value, terminology)
            return {key: _apply(item) for key, item in value.items()}
        return value

    return _apply(obj)
