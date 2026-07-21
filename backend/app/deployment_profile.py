"""R1.9.1-H1 T4 — deployment-profile detection (production vs dev/standalone).

Single source of truth for "is this process production". Mirrors the two
signals already used across the backend before this module existed:

  * an explicit ``ENVIRONMENT=production``, or
  * ``REQUIRE_CONNECTOR_SECRETS=1`` — the flag ``app/main.py``'s lifespan
    already treats as the production signal for ``validate_all_secrets()``
    and for gating whether the static role tokens (VIEWER_JWT/ANALYST_JWT/
    ADMIN_JWT) are seeded.

Every "is this prod" check in the backend should read :func:`is_production`
rather than re-implementing the two-signal test locally. A second, drifted
definition is exactly how a production guard can silently stop covering a
real deployment — the same class of gap R1.9.1-H1 T1/T2 closed for
credential env-fallbacks, applied here to the profile check itself.
"""
from __future__ import annotations

import os


def is_production() -> bool:
    """True when this process should be treated as production.

    Read live (not cached) — cheap to resolve and must reflect the current
    environment, e.g. in tests that toggle it via ``monkeypatch``.
    """
    return (
        os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        or os.getenv("REQUIRE_CONNECTOR_SECRETS") == "1"
    )
