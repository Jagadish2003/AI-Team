"""Application-level configuration flags.

Flags here are read from environment variables and apply to the whole
deployment — not per-org. Per-org control is a future story.

T3-S13-A T5 — INFERRED_RELATIONSHIPS_ENABLED
  The primary architectural switch that separates graph TRUTH (directly
  observed edges) from graph HYPOTHESES (inferred co-firing edges) in every
  response the platform produces.

  It is a *surfacing* control, never a storage gate. Inferred edges are always
  written to entity_relationships (see relationship_mapper.map_inferred_from_detectors).
  This flag only decides whether those edges are returned to the caller (e.g.
  in OppEnrichment.relationships and the evidence trace).

  Default is False: when the variable is absent, or set to anything other than
  'true' (case-insensitive), inferred edges are withheld until Stage 3 causal
  analysis (T3-S16-A) can validate them.
"""
from __future__ import annotations

import os

_FLAG_ENV = "INFERRED_RELATIONSHIPS_ENABLED"


def inferred_relationships_enabled() -> bool:
    """Return True only when INFERRED_RELATIONSHIPS_ENABLED == 'true' (case-insensitive).

    Read at call time (not import time) so the flag can be toggled per request
    or per test without re-importing the module. This is the runtime check the
    OppEnrichment population step uses.
    """
    return os.getenv(_FLAG_ENV, "false").strip().lower() == "true"


# Import-time snapshot matching the spec's documented pattern. Callers that need
# a live value (and tests that toggle the env var) must use the function above.
INFERRED_RELATIONSHIPS_ENABLED = inferred_relationships_enabled()
