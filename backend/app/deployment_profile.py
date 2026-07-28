"""R1.9.1-H1 T4 - deployment-profile detection.

Single source of truth for "is this process production": only an explicit
``ENVIRONMENT=production`` selects the production profile. Operational flags such
as ``REQUIRE_CONNECTOR_SECRETS=1`` may enforce startup secret presence, but they
do not redefine the deployment profile for staging, CI, or standalone runs.
"""
from __future__ import annotations

import os


def is_production() -> bool:
    """True when this process should be treated as production.

    Read live (not cached) — cheap to resolve and must reflect the current
    environment, e.g. in tests that toggle it via ``monkeypatch``.
    """
    return os.getenv("ENVIRONMENT", "").strip().lower() == "production"
