"""2.0-A1 T6 — the stored projection's provenance spine.

This task is load-bearing for 2.0-A2: without a stored projection there is
nothing to compare a measured outcome against, and the flywheel never starts.
A projection that is stored but cannot be *found again* — or cannot be tied to
the opportunity it describes, across runs — is no better than one that was never
stored at all. This module owns the identifying stamp that makes it findable.

    runId + oppId                → which observation this projection describes
    opportunityIdentity          → which real-world problem, ACROSS runs
    createdAt                    → when the projection was made
    orgId / packId / packVersion → whose, and by which pack logic
    schema versions              → whether a stored projection is still
                                   comparable with a freshly computed one

**Why provenance is stamped at STORE time, not build time.** ``build_projection``
is pure: no clock, no run context, no DB. That is what makes 2.0-A1 AC5 hold —
"re-running against unchanged signal reproduces identical bands and bases". A
timestamp inside the computed payload would make every recomputation differ from
its stored twin and quietly destroy that guarantee. So the *content* stays
deterministic and the *stamp* is applied by the pipeline hook that actually knows
the run id and the wall clock.

The consequence for 2.0-A2, stated plainly because it is easy to get wrong:
compare :func:`projection_core` values, not whole payloads. Two projections of
the same unchanged signal have identical cores and *different* provenance — that
is the design, not a bug.

Pure and dependency-free: no DB, no ``app`` import, and no clock read *inside*
this module (``created_at`` is passed in by the caller, which is also what makes
these functions testable without freezing time).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

#: Bumped when the provenance shape changes in a way 2.0-A2 must notice.
PROVENANCE_SCHEMA_VERSION = "1.0.0"

#: The key the provenance block occupies on a stored projection.
PROVENANCE_KEY = "provenance"

#: Every field a stored projection's provenance must carry. A stored projection
#: missing any of these cannot be reliably matched to a later measurement, which
#: is the whole point of storing it — so this tuple is asserted by contract test
#: rather than left as documentation.
REQUIRED_PROVENANCE_FIELDS = (
    "runId",
    "oppId",
    "opportunityIdentity",
    "createdAt",
    "orgId",
    "packId",
    "packVersion",
    "provenanceSchemaVersion",
    "projectionSchemaVersion",
)


def _clean(value: Any) -> Optional[str]:
    """Coerce to a non-empty trimmed string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_provenance(
    *,
    run_id: str,
    opp_id: str,
    created_at: str,
    org_id: Optional[str] = None,
    pack_id: Optional[str] = None,
    pack_version: Optional[str] = None,
    opportunity_identity: Optional[str] = None,
    projection_schema_version: Optional[str] = None,
    band_width_model_version: Optional[str] = None,
    recommendation_schema_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the provenance stamp for one stored projection.

    ``created_at`` is supplied by the caller rather than read here so this stays
    a pure function — the pipeline hook owns the clock.

    ``opportunity_identity`` is the STABLE cross-run key (R16-B1 §2). It may be
    ``None`` when the upstream opportunity was never stamped with one; the
    projection is still stored (a projection findable by run+opp is far better
    than none), but 2.0-A2 can only follow it across runs when the identity is
    present, so the absence is recorded explicitly rather than papered over.
    """
    return {
        "provenanceSchemaVersion": PROVENANCE_SCHEMA_VERSION,
        "runId": _clean(run_id),
        "oppId": _clean(opp_id),
        "opportunityIdentity": _clean(opportunity_identity),
        "createdAt": _clean(created_at),
        "orgId": _clean(org_id),
        "packId": _clean(pack_id),
        "packVersion": _clean(pack_version),
        "projectionSchemaVersion": _clean(projection_schema_version),
        "bandWidthModelVersion": _clean(band_width_model_version),
        "recommendationSchemaVersion": _clean(recommendation_schema_version),
        # Explicit rather than inferred from a null identity: a reader should not
        # have to know that "identity is None" means "not comparable across runs".
        "crossRunComparable": bool(_clean(opportunity_identity)),
    }


def projection_core(projection: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The deterministic part of a projection — everything except provenance.

    This is what 2.0-A2 compares, and what the AC5 reproducibility tests compare.
    Two projections computed from the same unchanged signal have equal cores and
    unequal provenance; comparing whole payloads would report a false difference
    on every single recomputation.
    """
    if not isinstance(projection, Mapping):
        return {}
    return {k: v for k, v in projection.items() if k != PROVENANCE_KEY}


def get_provenance(projection: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The provenance block off a stored projection, or ``{}`` when absent.

    ``{}`` rather than ``None`` so a caller can read a field without a null
    check; a projection stored before T6 simply has no stamp.
    """
    if not isinstance(projection, Mapping):
        return {}
    block = projection.get(PROVENANCE_KEY)
    return dict(block) if isinstance(block, Mapping) else {}


def missing_provenance_fields(projection: Optional[Mapping[str, Any]]) -> list:
    """Which required provenance fields a stored projection lacks.

    Empty list means the projection is fully identified and 2.0-A2 can match it.
    """
    provenance = get_provenance(projection)
    return [f for f in REQUIRED_PROVENANCE_FIELDS if not provenance.get(f)]


def is_storable(projection: Optional[Mapping[str, Any]]) -> bool:
    """True when a projection carries everything 2.0-A2 needs to compare it.

    Deliberately does NOT require ``opportunityIdentity``: cross-run tracking
    needs it, but a projection identified by run + opportunity is still worth
    storing and still comparable within its own run.
    """
    provenance = get_provenance(projection)
    return bool(
        provenance.get("runId")
        and provenance.get("oppId")
        and provenance.get("createdAt")
    )


def stamp_projection(
    projection: Optional[Mapping[str, Any]], provenance: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return a copy of ``projection`` carrying ``provenance``.

    A copy, not a mutation, so the caller's deterministic payload is never
    altered in place — the core must stay comparable with a fresh computation.
    """
    if not isinstance(projection, Mapping):
        return None
    stamped = dict(projection)
    stamped[PROVENANCE_KEY] = dict(provenance)
    return stamped


__all__ = [
    "PROVENANCE_SCHEMA_VERSION",
    "PROVENANCE_KEY",
    "REQUIRED_PROVENANCE_FIELDS",
    "build_provenance",
    "get_provenance",
    "is_storable",
    "missing_provenance_fields",
    "projection_core",
    "stamp_projection",
]
