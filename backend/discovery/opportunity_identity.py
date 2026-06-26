"""
R16-B1 (T3) — Stable Opportunity Identity.

An opportunity is easy to identify *within* one run, but later features
(outcome tracking, human feedback capture, before/after comparison) need to
know whether the SAME real-world problem reappears in a *different* run. That
requires a stable identity that the same underlying problem carries run after
run — independent of the run-varying measures of it.

``compute_opportunity_identity()`` derives that identity deterministically from
the finding's defining, run-invariant characteristics:

    - org_id
    - pack_id / detector identity that produced it
    - the signal_key / finding type
    - the resolved primary entity/entities it concerns (STABLE entity keys)

It deliberately EXCLUDES everything that drifts between runs for the same
opportunity — score, confidence, run timestamp, narrative text, evidence ids.
This is the single subtlest design rule in the story: if the id changed
whenever the confidence changed, every run would look like a brand-new
opportunity and nothing could be tracked over time (see R16-B1 Section 2).

Stability note on "primary entity ids": the persisted ``Entity.id`` is a random
``uuid4`` minted per row, so it is NOT stable across runs. The stable identity
of a system/process entity is its resolution key — ``(entity_type,
canonical_name)`` — which is exactly what ``entity_resolution`` dedupes on. This
module feeds those *resolution keys* into the signature, not the random UUIDs,
so the same accounts/process resolve to the same opportunity_identity every run
even though their underlying entity rows (and ids) may be recreated.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Iterable, Optional

__all__ = [
    "compute_opportunity_identity",
    "stable_entity_key",
    "primary_entity_keys_for_detector",
]

# Mirror entity_resolution._canonicalize so the entity keys this module builds
# match the canonical_name the resolver actually persists. Kept as a local copy
# (rather than importing entity_resolution) so identity computation has no
# dependency on the DB layer and stays usable in pure-offline discovery.
def _canonicalize(value: str) -> str:
    """Lowercase, strip, and collapse internal whitespace — same rule the
    entity resolver uses to derive a canonical match key."""
    return " ".join(str(value).split()).lower()


def _clean(value: object, *, field: str) -> str:
    """Coerce a required identity input to a non-empty, whitespace-stripped str.

    The signature must never silently fold a missing org_id / pack_id /
    signal_key into the hash (the reference ``'|'.join`` would crash on ``None``
    and an empty string would make two different findings collide), so a missing
    required input is a programming error and is raised, not swallowed.
    """
    if value is None:
        raise ValueError(f"compute_opportunity_identity: {field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"compute_opportunity_identity: {field} is required")
    return text


def stable_entity_key(entity_type: str, name: str) -> str:
    """Build the run-invariant key for one primary entity.

    Uses ``(entity_type, canonical_name)`` — the same pair the resolver dedupes
    on — rather than the random per-row ``Entity.id`` UUID, so the key is stable
    across runs. Example: ``stable_entity_key("process", "PR_REVIEW_BOTTLENECK")``
    → ``"process:pr_review_bottleneck"``.
    """
    etype = _canonicalize(entity_type)
    cname = _canonicalize(name)
    return f"{etype}:{cname}"


def compute_opportunity_identity(
    org_id: str,
    pack_id: str,
    signal_key: str,
    primary_entity_ids: Optional[Iterable[str]] = None,
) -> str:
    """Deterministic, stable identity for the underlying opportunity.

    Derived only from run-invariant inputs (R16-B1 Section 2). The same finding
    — same org, pack, signal, and primary entities — resolves to the same
    ``opp_…`` id every run, even as its score and confidence move between runs.

    Args:
        org_id: Owning organisation. Required.
        pack_id: Detector pack that produced the finding. Required.
        signal_key: The detector identity / finding type (e.g. the detector_id).
            Required.
        primary_entity_ids: Stable keys of the entities the finding concerns
            (use :func:`stable_entity_key` — NOT random ``Entity.id`` UUIDs).
            Order-independent and de-duplicated. May be empty: some findings are
            org/pack/signal-level and concern no specific resolved entity.

    Returns:
        ``"opp_" + sha256(...)[:24]`` — a stable hex identity string.
    """
    org = _clean(org_id, field="org_id")
    pack = _clean(pack_id, field="pack_id")
    signal = _clean(signal_key, field="signal_key")

    # Normalise the entity keys defensively: drop empties, de-duplicate, and
    # sort so the identity is independent of the order entities were discovered
    # in. Two runs that surface the same entities in a different order MUST
    # produce the same id.
    keys = sorted({
        str(k).strip()
        for k in (primary_entity_ids or [])
        if k is not None and str(k).strip()
    })

    basis = "|".join([org, pack, signal, ":".join(keys)])
    return "opp_" + sha256(basis.encode("utf-8")).hexdigest()[:24]


def primary_entity_keys_for_detector(detector_id: str, signal_source: str) -> list[str]:
    """Derive the stable primary-entity keys for an opportunity from its
    detector result.

    Entity extraction (``app/entity_extractor.py``) mints, per detector firing,
    one ``process`` entity keyed on ``detector_id`` and one ``system`` entity
    keyed on ``signal_source``. Those two resolution keys are the run-invariant
    entities a single-detector opportunity concerns, so identity can be derived
    straight from the detector result — and stays stable even on runs where the
    (non-blocking) extraction step failed and wrote no entity rows.
    """
    keys: list[str] = []
    if detector_id and str(detector_id).strip():
        keys.append(stable_entity_key("process", detector_id))
    if signal_source and str(signal_source).strip():
        keys.append(stable_entity_key("system", signal_source))
    return keys
