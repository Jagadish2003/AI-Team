"""Temporal signal storage API surface.

This module intentionally imports model definitions from the database layer only.
Higher-level runner and route code can import from here without creating a
database-to-app circular import.
"""

try:
    from database.models.signal_snapshots import DetectorEvaluationLike, SignalSnapshot
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports.
    from backend.database.models.signal_snapshots import (
        DetectorEvaluationLike,
        SignalSnapshot,
    )


__all__ = ["DetectorEvaluationLike", "SignalSnapshot"]
