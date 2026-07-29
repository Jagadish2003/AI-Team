"""MSP-B4 / T2 — deterministic ITSM resolution + incident-identity signatures.

This is the B4 counterpart to the B0 ``event_signature`` contract
([`event_signature.py`](event_signature.py)) and follows the **same signature
discipline**: deterministic, explainable, tested, and conservative. Where B0
fingerprints *which recurring cloud event this is*, B4 fingerprints two things
about a ServiceNow incident — **what kind of incident it is** and **how it was
resolved** — using STRUCTURED FIELDS ONLY. No semantic similarity, no fuzzy text
matching, no embeddings: two records group only when their structured identity
and resolution pattern truly match. Semantic matching of free-text resolutions
is MSP-B5, not this.

Two separate signatures (the detector in T3 groups on the pair)
--------------------------------------------------------------
* :func:`compute_incident_identity_signature` — *what kind of incident occurred*.
  Components: ``category``, the CI/CI-class the incident concerns (where
  available), and the normalised short-description token set. This is stable
  incident identity, independent of how the incident happened to be worded.
* :func:`compute_resolution_signature` — *how the incident was resolved*.
  Components: ``category``, ``close_code``, CI/CI-class (where available), and
  the resolved-by assignment **group** (a queue, never a person). This is the
  identity of "resolved the same way".

Recurrence detection (B6) groups on ``(incident_identity_signature,
resolution_signature)``; runbook matching (B5) reads the resolution signature to
know what resolution pattern was observed without re-deriving it.

Normalisation rules (explicit and documented — T2 completion criterion)
-----------------------------------------------------------------------
Every component is reduced by :func:`normalize_token` before hashing:

* **Case** — casefolded (``"Solved (Permanently)"`` → ``"solved (permanently)"``).
* **Whitespace** — leading/trailing stripped and internal runs collapsed to a
  single space, so ``"  Level 2   Support "`` and ``"Level 2 Support"`` agree.
* **Empty / missing** — ``None``, ``""``, and whitespace-only all fold to the
  empty token ``""``. A missing component participates as empty, never as a
  guessed value.
* **ServiceNow reference display values** — a raw reference/choice object
  (``{"value": ..., "display_value": ...}``) is reduced deterministically,
  preferring the stable ``value`` over the mutable ``display_value``. Callers
  SHOULD pass pre-extracted scalars (``servicenow.py`` does: a CI reference is
  extracted to its stable ``sys_id``; a choice field to its label); this is a
  defensive, documented fallback so a stray reference object can never change a
  signature's meaning.

CI / CI-class fallback (``_ci_component`` — T2 + AC5 groundwork)
---------------------------------------------------------------
The CI component prefers the **CI class** when it is known (broader "this class
of thing"), else the specific **CI id**, else empty ("unlocated"). Each carries
an explicit ``class:`` / ``ci:`` marker so a CI id can never collide with a CI
class that happens to share its text, and an unlocated incident (no CMDB join —
the B3-absent case) still produces a stable signature that groups with other
unlocated incidents of the same pattern. When B3 later supplies the CI class,
the same incident's signature sharpens from ``ci:`` to ``class:`` — a version
bump territory, hence the explicit versions below.

Conservative grouping / near-miss separation
---------------------------------------------
Because ``category``, ``close_code``, and the CI component all participate,
near-misses stay separate by construction: the same category with a different
close code, or the same close code with a different CI class, hash differently.

Versioning
----------
:data:`RESOLUTION_SIGNATURE_VERSION` / :data:`INCIDENT_IDENTITY_SIGNATURE_VERSION`
prefix every signature. Bump the relevant one whenever its component recipe or a
normalisation rule changes, so signatures from different rule versions never
silently compare equal (identical rule to B0's ``EVENT_SIGNATURE_VERSION``).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

#: Bump when the resolution-signature recipe or a normalisation rule changes.
RESOLUTION_SIGNATURE_VERSION = "1"
#: Bump when the incident-identity recipe or a normalisation rule changes.
INCIDENT_IDENTITY_SIGNATURE_VERSION = "1"

#: Component field-separator (ASCII unit separator) — a control char that cannot
#: appear in a normalised component, so component boundaries are unambiguous
#: (mirrors ``event_signature._SEP``).
_SEP = "\x1f"

#: Short-description tokeniser: maximal alphanumeric runs, casefolded upstream.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Minimal, fixed, documented grammatical-filler stop set removed from the
#: short-description token set. Deliberately conservative — only true structural
#: filler (articles / conjunctions / prepositions / pronouns / copulas), never
#: ITSM content words — so genuinely different incidents stay distinct.
_SHORT_DESC_STOPWORDS: frozenset = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "at",
        "by", "with", "from", "is", "are", "be", "was", "were", "this", "that",
        "it", "as", "no", "not", "its",
    }
)

#: Tokens shorter than this are dropped from the identity token set.
_MIN_TOKEN_LEN = 2


def _scalar(value: Any) -> str:
    """Reduce a value (incl. a ServiceNow reference/choice object) to a string.

    Prefers the stable ``value`` over the mutable ``display_value`` for a
    reference object. Callers should pass pre-extracted scalars; this is the
    documented defensive fallback (see module docstring).
    """
    if isinstance(value, dict):
        value = (
            value.get("value")
            or value.get("display_value")
            or value.get("displayName")
            or value.get("name")
        )
    return "" if value is None else str(value)


def normalize_token(value: Any) -> str:
    """Canonicalise one component: case, whitespace, and empty/missing handling."""
    return " ".join(_scalar(value).split()).casefold()


def normalize_short_description(text: Any) -> Tuple[str, ...]:
    """Reduce a short description to a deterministic, order-independent token set.

    Casefold → maximal alphanumeric tokens → drop grammatical filler and
    sub-minimum-length tokens → de-duplicate → sort. Order-, case-, and
    punctuation-independent, but purely structural: no stemming, edit distance,
    or semantic expansion (that is MSP-B5). Empty input yields an empty tuple.
    """
    scalar = _scalar(text).casefold()
    tokens = {
        token
        for token in _TOKEN_RE.findall(scalar)
        if len(token) >= _MIN_TOKEN_LEN and token not in _SHORT_DESC_STOPWORDS
    }
    return tuple(sorted(tokens))


def _ci_component(ci_class: Any, ci_id: Any) -> str:
    """Resolve the CI component, preferring CI class over CI id, else unlocated.

    Explicit ``class:`` / ``ci:`` markers keep the two kinds from colliding; an
    empty return means the incident is unlocated (no CMDB join available).
    """
    ci_class_token = normalize_token(ci_class)
    if ci_class_token:
        return f"class:{ci_class_token}"
    ci_id_token = normalize_token(ci_id)
    if ci_id_token:
        return f"ci:{ci_id_token}"
    return ""


def _digest(components: List[str]) -> str:
    joined = _SEP.join(components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]  # 128-bit


def compute_resolution_signature(
    *,
    category: Any,
    close_code: Any,
    resolved_by_group: Any,
    ci_class: Any = None,
    ci_id: Any = None,
) -> str:
    """Deterministic fingerprint of HOW an incident was resolved.

    Pure function of its inputs — no clock, no randomness — so the same
    structured resolution always yields the identical signature, while any
    change to a participating component (close code, category, CI, group)
    changes it. CI participates via :func:`_ci_component` (class preferred).
    """
    components = [
        normalize_token(category),
        normalize_token(close_code),
        _ci_component(ci_class, ci_id),
        normalize_token(resolved_by_group),
    ]
    return f"{RESOLUTION_SIGNATURE_VERSION}:{_digest(components)}"


def compute_incident_identity_signature(
    *,
    category: Any,
    short_description: Any,
    ci_class: Any = None,
    ci_id: Any = None,
) -> str:
    """Deterministic fingerprint of WHAT KIND of incident occurred.

    Structured identity only: category, CI/CI-class (where available), and the
    normalised short-description token set. No fuzzy/semantic matching.
    """
    tokens = normalize_short_description(short_description)
    components = [
        normalize_token(category),
        _ci_component(ci_class, ci_id),
        " ".join(tokens),  # tokens are alphanumeric-only → space-join is unambiguous
    ]
    return f"{INCIDENT_IDENTITY_SIGNATURE_VERSION}:{_digest(components)}"


def resolution_signature_components(
    *,
    category: Any,
    close_code: Any,
    resolved_by_group: Any,
    ci_class: Any = None,
    ci_id: Any = None,
) -> Dict[str, Any]:
    """Return the resolved components behind a resolution signature (audit aid).

    Lets a consumer (or a debugging engineer) explain WHY two incidents share or
    differ in a resolution signature without re-deriving the rules.
    """
    return {
        "version": RESOLUTION_SIGNATURE_VERSION,
        "category": normalize_token(category),
        "close_code": normalize_token(close_code),
        "ci_component": _ci_component(ci_class, ci_id),
        "resolved_by_group": normalize_token(resolved_by_group),
    }


def incident_identity_signature_components(
    *,
    category: Any,
    short_description: Any,
    ci_class: Any = None,
    ci_id: Any = None,
) -> Dict[str, Any]:
    """Return the resolved components behind an incident-identity signature."""
    return {
        "version": INCIDENT_IDENTITY_SIGNATURE_VERSION,
        "category": normalize_token(category),
        "ci_component": _ci_component(ci_class, ci_id),
        "short_description_tokens": list(normalize_short_description(short_description)),
    }
