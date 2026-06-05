"""Compatibility import path for the SQL Server operational signal scorer."""
from __future__ import annotations

try:
    from backend.discovery.packs.sqlserver_opsignal_scorer import (
        get_score,
        is_sqlserver_opsignal_detector,
        score_opportunity,
        score_sqlserver_opsignal,
    )
except ModuleNotFoundError:
    from discovery.packs.sqlserver_opsignal_scorer import (
        get_score,
        is_sqlserver_opsignal_detector,
        score_opportunity,
        score_sqlserver_opsignal,
    )

__all__ = [
    "get_score",
    "is_sqlserver_opsignal_detector",
    "score_opportunity",
    "score_sqlserver_opsignal",
]
