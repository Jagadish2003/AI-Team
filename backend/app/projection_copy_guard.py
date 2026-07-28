"""2.0-A1 T5 — the serve-path application of the projection vocabulary guard.

``discovery/projection/vocabulary.py`` decides WHAT may not be said. This module
decides WHERE that decision is applied on the way out of the API, and knows the
one thing the pure guard must not: the shape of an opportunity record.

AC3 covers "API, UI, report, or export", so the guard runs on every serve path
that emits opportunity prose — the opportunities list the Opportunity Review
renders, and the executive report — not only inside the report engine. A claim
that never passes through ``build_executive_report`` (the live
``GET /executive-report`` route composes its response from stored opps directly)
would otherwise reach a customer unchecked.

Everything here returns COPIES. The stored opportunity a run persisted is never
rewritten: a replay must serve what the run produced, and silently editing
history to satisfy a guard would be its own kind of dishonesty. The guard is a
serve-time overlay, exactly like the R191-R1 connector-roadmap flags.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Narrative fields on an opportunity that reach a customer-facing surface.
#: Measured fields (impact, effort, evidence ids, the projection's own numbers)
#: are untouched — the guard is about claims, not about numbers.
NARRATIVE_FIELDS = ("title", "aiRationale", "aiSummary")


def scrub_opportunity_narrative(opp: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``opp`` with projection claims stripped from its prose.

    The unchanged object is returned as-is when it is already clean, so the
    common path allocates nothing.
    """
    from discovery.projection.vocabulary import contains_prohibited, sanitize_text

    if not isinstance(opp, dict):
        return opp
    if not any(contains_prohibited(opp.get(field)) for field in NARRATIVE_FIELDS):
        return opp

    cleaned = dict(opp)
    for field in NARRATIVE_FIELDS:
        value = cleaned.get(field)
        if isinstance(value, str) and value:
            cleaned[field] = sanitize_text(value)
    return cleaned


def scrub_opportunity_narratives(
    opps: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Scrub every opportunity bound for a customer-facing surface."""
    return [scrub_opportunity_narrative(opp) for opp in opps or []]


def scrub_executive_summary(summary: Optional[str]) -> str:
    """Strip projection claims from the executive summary paragraph.

    Applied at the report boundary in addition to the guard that already runs
    when the summary is generated, so a summary arriving by any future path
    cannot carry a savings claim into the executive report.
    """
    from discovery.projection.vocabulary import sanitize_text

    return sanitize_text(summary or "")


__all__ = [
    "NARRATIVE_FIELDS",
    "scrub_executive_summary",
    "scrub_opportunity_narrative",
    "scrub_opportunity_narratives",
]
