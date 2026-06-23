"""ENT-3 / T3-S15-A — Hallucination guard with graceful recovery.

When the first-pass LLM emits an ``aiWhyBullets`` entry that references a proper
noun (person / team / system) that is *not* among the run's resolved entity
names, that name is a hallucination. This module repairs or drops such bullets
so that no fabricated name ever reaches stored artifacts or the UI.

Entry point
-----------
    validate_and_recover(bullet, resolved_names, org_id, run_id) -> str | None

Returns a corrected bullet, or ``None`` when the bullet must be dropped. It
NEVER returns a bullet with a hallucinated name left intact.

Recovery ladder (deterministic, cheap-first)
--------------------------------------------
    1. Clean       — no hallucinated names → return unchanged (fast path).
    2. Rule rewrite — :func:`rule_based_rewrite` restructures the sentence with
                      four pattern handlers. If the result :func:`is_coherent`,
                      log ``rule_rewrite`` and return it. No LLM call.
    3. Drop generic — if the bullet is not :func:`is_worth_saving` (no real
                      graph content), log ``dropped_generic`` and return None.
    4. LLM rewrite  — :func:`llm_rewrite_bullet` with a hard
                      :data:`REWRITE_TIMEOUT_MS` budget and a single attempt.
                      On success log ``llm_rewrite``; on timeout log
                      ``dropped_timeout`` and return None.

Each terminal path calls :func:`log_hallucination`, which emits telemetry
through the registered ``hallucination_guard.*`` event types (T7).

PII / telemetry safety: hallucinated names and bullet text are NEVER written to
telemetry — only counts, reason/method codes, and run/org identifiers. The
module is deterministic in steps 1–3 (no network); only step 4 touches the LLM,
isolated behind :func:`llm_rewrite_bullet` so tests can mock or time it out.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Hard timeout on the second-pass LLM rewrite (milliseconds). The rewrite runs
# in a worker thread and is abandoned if it has not returned within this budget.
REWRITE_TIMEOUT_MS = 500
# One rewrite attempt only — the guard never retries the LLM.
REWRITE_MAX_RETRIES = 1

# Maximum number of second-pass rewrite threads allowed to be in flight at once,
# process-wide. The rewrite runs in a worker thread that we abandon after
# REWRITE_TIMEOUT_MS, but a slow Claude API call keeps the abandoned thread alive
# (holding an HTTPS connection + memory) until it finally returns — Python does
# not garbage-collect a running daemon thread. Under enrichment load (many opps ×
# many bullets) those abandoned threads would otherwise accumulate without bound.
# This bounded semaphore caps the live rewrite threads: the worker releases its
# slot in a ``finally`` even when it returns long after being abandoned, and a
# caller that cannot get a slot within its timeout budget drops the bullet rather
# than spawning an (N+1)-th thread. See ENT-4 review #1 (resource leak under load).
REWRITE_MAX_CONCURRENCY = 5
_rewrite_semaphore = threading.BoundedSemaphore(REWRITE_MAX_CONCURRENCY)

# Capitalised word, then zero or more further capitalised words: matches both
# single proper nouns ("Acme") and multi-word names ("Jane Doe", "Billing Ops").
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9&'./-]*(?:\s+[A-Z][a-zA-Z0-9&'./-]*)*\b")

# Leading [OBSERVED] / [INFERRED: ...] tags are stripped before proper-noun
# extraction so the guard never mistakes a tag for a hallucinated name.
_TAG_PREFIX_RE = re.compile(r"^\s*\[(?:OBSERVED|INFERRED[^\]]*)\]\s*", re.IGNORECASE)

# Single capitalised words that are ordinary sentence openers / generic terms,
# not proper nouns. Filtering these avoids flagging "Tickets are piling up" as a
# hallucination just because the sentence starts with a capital letter.
#
# MAINTENANCE OBLIGATION (review #8): this list must be reviewed whenever the
# system is onboarded to a customer whose schema uses entity names that are also
# common English words — e.g. an nCino loan product literally called "Standard",
# or a team called "Operations". Such a real entity name, if it ever appears here
# as a single word, would be filtered out and never validated against
# resolved_names. Conversely, words missing from this list inflate false
# positives. Keep it conservative and revisit per new customer naming convention;
# without this review, guard false-positive rates silently drift upward.
_COMMON_WORDS: Set[str] = {
    "the", "this", "these", "those", "that", "a", "an", "and", "but", "or",
    "when", "while", "after", "before", "if", "because", "since", "as",
    "tickets", "cases", "records", "users", "agents", "members", "teams",
    "systems", "requests", "approvals", "queues", "incidents", "accounts",
    "loans", "applications", "benefits", "members", "no", "several", "many",
    "multiple", "average", "current", "recent", "high", "low", "without",
}

# A bullet whose last word is one of these reads as dangling / truncated.
_DANGLING_TRAILING: Set[str] = {
    "of", "to", "and", "with", "the", "a", "an", "by", "for", "from", "in",
    "on", "at", "is", "are", "owns", "own", "member",
}


# ---------------------------------------------------------------------------
# Stats collector — the hand-off object between guard results and T5 fields
# ---------------------------------------------------------------------------

@dataclass
class GuardStats:
    """Accumulates what the guard did across one opportunity's bullets.

    Maps directly onto the new ``OppEnrichment`` fields (T5):
      rule_rewrites      -> hallucination_rewrites
      llm_rewrites       -> hallucination_llm_rewrites
      removals (reasons) -> hallucination_removals

    ``removals`` stores reason codes ('dropped_generic' / 'dropped_timeout'),
    never the dropped bullet text, so the response carries no fabricated names.
    """

    rule_rewrites: int = 0
    llm_rewrites: int = 0
    removals: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Proper-noun extraction
# ---------------------------------------------------------------------------

def _strip_tag(bullet: str) -> str:
    """Remove a leading [OBSERVED]/[INFERRED: X] tag for analysis purposes."""
    return _TAG_PREFIX_RE.sub("", bullet or "")


def extract_proper_nouns(bullet: str) -> List[str]:
    """Extract candidate proper nouns (capitalised phrases) from a bullet.

    A leading observation tag is stripped first. Single capitalised words that
    are common sentence openers / generic nouns (:data:`_COMMON_WORDS`) are
    discarded to avoid false positives; multi-word capitalised phrases are
    always kept. Order is preserved and duplicates removed.
    """
    text = _strip_tag(bullet)
    seen: Set[str] = set()
    result: List[str] = []
    for match in _PROPER_NOUN_RE.findall(text):
        phrase = match.strip()
        if not phrase:
            continue
        words = phrase.split()
        if len(words) == 1 and words[0].lower() in _COMMON_WORDS:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(phrase)
    return result


def _normalise_resolved(resolved_names: Iterable[str]) -> Set[str]:
    """Coerce resolved names to a lowercased set for membership tests."""
    return {str(n).strip().lower() for n in (resolved_names or []) if str(n).strip()}


# ---------------------------------------------------------------------------
# Coherence + worth-saving checks
# ---------------------------------------------------------------------------

def is_coherent(text: Optional[str]) -> bool:
    """Basic structural coherence: > 5 words and no dangling trailing reference.

    Also rejects a rewrite that still contains a stutter such as
    'a team member a team member' which the fallback can otherwise produce.
    """
    if not text or not text.strip():
        return False
    cleaned = text.strip()
    if re.search(r"a team member(\s+a team member)+", cleaned, re.IGNORECASE):
        return False
    words = cleaned.split()
    if len(words) <= 5:
        return False
    if words[-1].lower().rstrip(".,;:") in _DANGLING_TRAILING:
        return False
    return True


def is_worth_saving(bullet: str, resolved_names: Iterable[str]) -> bool:
    """A bullet is worth a second-pass LLM rewrite only if it has graph content.

    'Graph content' means it references at least one resolved entity name. A
    generic bullet with no resolved-name reference is dropped without an LLM
    call (AC5).
    """
    resolved = _normalise_resolved(resolved_names)
    if not resolved:
        return False
    nouns = extract_proper_nouns(bullet)
    return any(n.lower() in resolved for n in nouns)


# ---------------------------------------------------------------------------
# Rule-based rewrite — four deterministic pattern handlers (Section 3)
# ---------------------------------------------------------------------------

def rule_based_rewrite(bullet: str, names: List[str]) -> str:
    """Remove hallucinated ``names`` and restructure the sentence.

    Patterns handled (Section 3):
      '{Name} owns X'                 -> 'X remain unresolved'
      '{Name} is a member of {Team}'  -> 'A team member is assigned to {Team}'
      '{Name} and {Name2} own X'      -> 'Team members own X'
      fallback: replace each hallucinated name with 'a team member'.

    Only the hallucinated ``names`` are removed — any proper noun NOT in
    ``names`` (e.g. a resolved {Team}) is preserved verbatim.
    """
    text = _strip_tag(bullet).strip()
    if not names:
        return text

    # Longest names first so re.escape alternation prefers the fuller match.
    ordered = sorted({n for n in names if n}, key=len, reverse=True)
    if not ordered:
        return text
    joined = "|".join(re.escape(n) for n in ordered)

    # Pattern: "{Name} and {Name2} own X" -> "Team members own X"
    m = re.match(rf"^\s*(?:{joined})\s+and\s+(?:{joined})\s+own\s+(.+?)\.?\s*$", text, re.IGNORECASE)
    if m:
        return f"Team members own {m.group(1).strip()}"

    # Pattern: "{Name} owns X" -> "X remain unresolved"
    m = re.match(rf"^\s*(?:{joined})\s+owns?\s+(.+?)\.?\s*$", text, re.IGNORECASE)
    if m:
        obj = m.group(1).strip().rstrip(".")
        return f"{obj} remain unresolved"

    # Pattern: "{Name} is a member of {Team}" -> "A team member is assigned to {Team}"
    m = re.match(rf"^\s*(?:{joined})\s+is\s+a\s+member\s+of\s+(.+?)\.?\s*$", text, re.IGNORECASE)
    if m:
        team = m.group(1).strip().rstrip(".")
        return f"A team member is assigned to {team}"

    # Fallback: replace each hallucinated name with 'a team member', then
    # collapse any resulting stutter.
    for n in ordered:
        text = re.sub(re.escape(n), "a team member", text)
    text = re.sub(r"(a team member)(\s+a team member)+", r"\1", text, flags=re.IGNORECASE)
    return text.strip()


# ---------------------------------------------------------------------------
# Second-pass LLM rewrite — isolated, timeout-bounded, mockable
# ---------------------------------------------------------------------------

def _invoke_rewrite_llm(prompt: str) -> Optional[str]:
    """Single LLM call used by the second-pass rewrite.

    Isolated so tests can monkeypatch it (to simulate a slow/timed-out call or a
    clean rewrite) without any network dependency. Routes through the model gateway
    (R16-D1 T3) with the 500ms timeout matching REWRITE_TIMEOUT_MS.
    """
    from app.llm_enrichment import MAX_TOKENS_OPP
    from app.model_gateway import GenerationRequest, get_generation_provider

    result = get_generation_provider().generate(
        GenerationRequest(
            prompt=prompt,
            max_tokens=MAX_TOKENS_OPP,
            timeout_ms=REWRITE_TIMEOUT_MS,
        )
    )
    return result.text


def _build_rewrite_prompt(bullet: str, remove_names: List[str], resolved_names: Iterable[str]) -> str:
    resolved = sorted(_normalise_resolved(resolved_names))
    return (
        "Rewrite the following bullet so it no longer references these names, "
        "which do not exist in the source data: "
        f"{', '.join(remove_names)}.\n"
        "You may reference only these confirmed entities: "
        f"{', '.join(resolved) if resolved else '(none)'}.\n"
        "Keep it to one concise sentence. Return only the rewritten bullet text.\n\n"
        f"Bullet: {_strip_tag(bullet).strip()}"
    )


def llm_rewrite_bullet(
    bullet: str,
    remove_names: List[str],
    resolved_names: Iterable[str],
    timeout_ms: int = REWRITE_TIMEOUT_MS,
) -> str:
    """Second-pass rewrite of ``bullet`` with a hard timeout.

    The LLM call runs in a worker thread; if it has not returned within
    ``timeout_ms`` a :class:`TimeoutError` is raised (the caller drops the
    bullet). A returned rewrite that still contains a hallucinated name, or that
    is empty, is treated as a failed rewrite (TimeoutError) so the guard's
    contract — never return a hallucinated name — always holds.

    Concurrency is bounded by :data:`REWRITE_MAX_CONCURRENCY` via
    ``_rewrite_semaphore``. A slot is acquired before the worker starts and is
    released by the worker itself in a ``finally`` — so an abandoned (timed-out)
    thread still frees its slot when the slow API eventually returns, capping the
    number of live rewrite threads / open connections process-wide. If no slot is
    free within the timeout budget the rewrite is treated as a timeout and the
    bullet is dropped, never spawning an unbounded extra thread.
    """
    prompt = _build_rewrite_prompt(bullet, remove_names, resolved_names)
    result: dict = {}
    timeout_s = timeout_ms / 1000.0

    # Fail-fast if the rewrite pool is saturated — drop rather than pile on.
    if not _rewrite_semaphore.acquire(timeout=timeout_s):
        raise TimeoutError(
            f"llm_rewrite_bullet: no rewrite slot free within {timeout_ms}ms "
            f"(>= {REWRITE_MAX_CONCURRENCY} in flight)"
        )

    def _worker() -> None:
        try:
            result["value"] = _invoke_rewrite_llm(prompt)
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = exc
        finally:
            # Always release, even if this thread was abandoned by the caller.
            _rewrite_semaphore.release()

    thread = threading.Thread(
        target=_worker, name="hallucination-rewrite", daemon=True
    )
    thread.start()
    thread.join(timeout_s)

    if thread.is_alive():
        # Worker still running past the budget — abandon it. It keeps its
        # semaphore slot until it finishes, which is exactly the cap we want.
        raise TimeoutError(f"llm_rewrite_bullet exceeded {timeout_ms}ms")
    if "error" in result:
        raise TimeoutError(f"llm_rewrite_bullet failed: {result['error']}")

    rewritten = result.get("value")
    if not rewritten or not str(rewritten).strip():
        raise TimeoutError("llm_rewrite_bullet returned empty output")

    rewritten = str(rewritten).strip()
    # Guarantee no hallucinated name survived the rewrite.
    lowered = rewritten.lower()
    if any(name.lower() in lowered for name in remove_names):
        raise TimeoutError("llm_rewrite_bullet left a hallucinated name intact")
    return rewritten


# ---------------------------------------------------------------------------
# Telemetry — counts/reasons only, never names or bullet text
# ---------------------------------------------------------------------------

# reason code -> (event_type, payload key, payload value)
_REWRITE_REASONS = {"rule_rewrite", "llm_rewrite"}
_REMOVE_REASONS = {"dropped_generic", "dropped_timeout"}


def log_hallucination(
    hallucinated: List[str],
    reason: str,
    org_id: Optional[str],
    run_id: Optional[str],
) -> None:
    """Emit a ``hallucination_guard.*`` telemetry event. Never raises.

    ``reason`` is one of: 'rule_rewrite', 'llm_rewrite' (→ rewritten) or
    'dropped_generic', 'dropped_timeout' (→ removed). Only the COUNT of
    hallucinated names is emitted — never the names themselves.
    """
    try:
        from app.telemetry import record_event

        count = len(hallucinated or [])
        if reason in _REWRITE_REASONS:
            record_event(
                "hallucination_guard.rewritten",
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "method": reason,
                    "hallucinated_count": count,
                    "source": "hallucination_guard",
                },
            )
        elif reason in _REMOVE_REASONS:
            record_event(
                "hallucination_guard.removed",
                {
                    "org_id": org_id,
                    "run_id": run_id,
                    "reason": reason,
                    "hallucinated_count": count,
                    "source": "hallucination_guard",
                },
            )
        else:  # pragma: no cover - guarded by callers
            logger.debug("log_hallucination: unknown reason %r", reason)
    except Exception as exc:
        logger.warning("hallucination telemetry failed (%s): %s", reason, exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_and_recover(
    bullet: str,
    resolved_names: Iterable[str],
    org_id: Optional[str],
    run_id: Optional[str],
    stats: Optional[GuardStats] = None,
) -> Optional[str]:
    """Validate and, if needed, repair or drop a single ``why`` bullet.

    Returns the (possibly rewritten) bullet, or ``None`` if it must be dropped.
    Never returns a bullet with a hallucinated name intact.

    ``stats`` is an optional :class:`GuardStats` collector mutated in place so
    the pipeline (T3) can populate the ``OppEnrichment`` hallucination fields;
    callers that only need the contract return value (tests, AC2–AC5) may omit
    it.

    SCOPE LIMITATION (review #7): each bullet is validated in isolation — the
    guard has no cross-bullet context. A pronoun co-reference such as bullet 2
    "She approved the covenant review" following bullet 1 "Alice Smith escalated
    the approval" is NOT resolved: "She" is not a proper noun, so it is neither
    flagged nor linked back to the resolved entity "Alice Smith". This is an
    accepted limitation of per-bullet processing for the current scope; a future
    improvement would thread prior-bullet entity context through this call.
    """
    resolved = _normalise_resolved(resolved_names)

    proper_nouns = extract_proper_nouns(bullet)
    hallucinated = [n for n in proper_nouns if n.lower() not in resolved]

    if not hallucinated:
        return bullet  # clean — no action needed

    # Step 1: rule-based rewrite (deterministic, no LLM).
    rewritten = rule_based_rewrite(bullet, hallucinated)
    if is_coherent(rewritten):
        log_hallucination(hallucinated, "rule_rewrite", org_id, run_id)
        if stats is not None:
            stats.rule_rewrites += 1
        return rewritten

    # Step 2a: not worth an LLM call — drop a generic bullet outright.
    if not is_worth_saving(bullet, resolved):
        log_hallucination(hallucinated, "dropped_generic", org_id, run_id)
        if stats is not None:
            stats.removals.append("dropped_generic")
        return None

    # Step 2b: conditional second-pass LLM rewrite, hard-bounded.
    try:
        rewritten = llm_rewrite_bullet(
            bullet=bullet,
            remove_names=hallucinated,
            resolved_names=resolved,
            timeout_ms=REWRITE_TIMEOUT_MS,
        )
        log_hallucination(hallucinated, "llm_rewrite", org_id, run_id)
        if stats is not None:
            stats.llm_rewrites += 1
        return rewritten
    except TimeoutError:
        log_hallucination(hallucinated, "dropped_timeout", org_id, run_id)
        if stats is not None:
            stats.removals.append("dropped_timeout")
        return None
