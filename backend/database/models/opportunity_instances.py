"""Opportunity-instance model (R16-B1, Part Two / T4).

An *opportunity identity* (R16-B1 §2, ``discovery/opportunity_identity.py``) is
the stable, deterministic id of the underlying real-world problem — it persists
across all runs. An *opportunity instance* is ONE run's observation of that
problem: its score, confidence, evidence and narrative at that point in time
(R16-B1 §2a). Many instances over time share one identity — and that pairing is
exactly what before/after outcome comparison (2.0) needs.

This module owns the ``opportunity_instances`` table schema (the single source
of truth for its DDL). The migration ``0017_create_opportunity_instances.py``
and the runtime ``ensure_opportunity_instances_table()`` helper both execute the
exact statements in ``ALL_OPPORTUNITY_INSTANCES_DDL`` — mirroring the locked
entities pattern (``database/models/entities.py``) so the migration-applied and
runtime-created schemas can never drift.

Primary key ``(opportunity_identity, run_id)``: a run observes a given
opportunity at most once, so the same identity recurs once per run. Querying by
``opportunity_identity`` then returns the full time series of instances for one
problem — the foundation outcome tracking draws from.

Run-VARYING measures (impact, effort, score, confidence, tier, evidence,
narrative) live on the instance because they change between runs for the SAME
identity. Run-INVARIANT identity inputs (org, pack, signal/detector) are NOT
re-derived here — they are stamped alongside so an instance is self-describing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# R16-B1 §4 fallback: the pack version stamped on an instance when the upstream
# opportunity has not declared one (e.g. on a branch where the T5 pack-version
# stamp is not yet present). When T5 is in place the real ``packVersion`` from
# the opportunity is used instead; this only guarantees the AC6 column is never
# empty. Kept local so this module has no import dependency on the packs layer.
DEFAULT_PACK_VERSION = "1.0.0"

CONFIDENCE_VALUES = frozenset({"LOW", "MEDIUM", "HIGH"})

CREATE_OPPORTUNITY_INSTANCES_TABLE = """
    CREATE TABLE IF NOT EXISTS opportunity_instances (
        opportunity_identity  VARCHAR(64)   NOT NULL,
        run_id                VARCHAR(64)   NOT NULL,
        org_id                VARCHAR(64)   NOT NULL,
        pack_id               VARCHAR(64)   NOT NULL,
        pack_version          VARCHAR(32)   NOT NULL,
        detector_id           VARCHAR(128)  NOT NULL,
        signal_source         VARCHAR(128),
        opportunity_ref       VARCHAR(64),
        impact                INTEGER,
        effort                INTEGER,
        score                 FLOAT,
        confidence            VARCHAR(16),
        tier                  VARCHAR(32),
        evidence_ids          TEXT,
        evidence_count        INTEGER       NOT NULL DEFAULT 0,
        narrative             TEXT,
        metadata              TEXT,
        created_at            TIMESTAMP     NOT NULL,
        is_deleted            BOOLEAN       NOT NULL DEFAULT FALSE,
        PRIMARY KEY (opportunity_identity, run_id)
    )
"""

# Cross-run time series for one problem — the read outcome tracking (2.0) needs.
# Composite with is_deleted so the active-instances query (the common read) is
# index-served: the soft-delete pattern established in migration 0016 means a
# logically deleted instance (e.g. an invalidated run) is filtered, not DELETEd,
# preserving the immutable-audit-trail principle applied to telemetry/audit_log.
CREATE_OPPORTUNITY_INSTANCES_IDX_IDENTITY = """
    CREATE INDEX IF NOT EXISTS idx_opp_instances_identity
        ON opportunity_instances (opportunity_identity, is_deleted)
"""

# All instances observed in a single run, org-scoped.
CREATE_OPPORTUNITY_INSTANCES_IDX_ORG_RUN = """
    CREATE INDEX IF NOT EXISTS idx_opp_instances_org_run
        ON opportunity_instances (org_id, run_id)
"""

ALL_OPPORTUNITY_INSTANCES_DDL: tuple[str, ...] = (
    CREATE_OPPORTUNITY_INSTANCES_TABLE,
    CREATE_OPPORTUNITY_INSTANCES_IDX_IDENTITY,
    CREATE_OPPORTUNITY_INSTANCES_IDX_ORG_RUN,
)

# Column order for INSERT / SELECT round-trips — the single ordering both the
# writer and reader use, so they can never disagree on positional mapping.
OPPORTUNITY_INSTANCE_COLUMNS: tuple[str, ...] = (
    "opportunity_identity",
    "run_id",
    "org_id",
    "pack_id",
    "pack_version",
    "detector_id",
    "signal_source",
    "opportunity_ref",
    "impact",
    "effort",
    "score",
    "confidence",
    "tier",
    "evidence_ids",
    "evidence_count",
    "narrative",
    "metadata",
    "created_at",
    "is_deleted",
)


@dataclass
class OpportunityInstance:
    """One run's observation of an opportunity (R16-B1 §2a).

    The mandatory identity spine — ``opportunity_identity``, ``run_id``,
    ``org_id``, ``pack_id``, ``pack_version`` — is required (AC6). Run-varying
    measures are optional because a degraded run may not produce all of them.
    """

    opportunity_identity: str
    run_id: str
    org_id: str
    pack_id: str
    pack_version: str
    detector_id: str
    signal_source: Optional[str] = None
    opportunity_ref: Optional[str] = None
    impact: Optional[int] = None
    effort: Optional[int] = None
    score: Optional[float] = None
    confidence: Optional[str] = None
    tier: Optional[str] = None
    evidence_ids: list[str] = field(default_factory=list)
    evidence_count: int = 0
    narrative: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Soft-delete flag (R16-B1 review / migration-0016 pattern). False for live
    # instances; a logically deleted instance is filtered from reads, never
    # DELETEd, so the per-run observation history stays immutable.
    is_deleted: bool = False

    def __post_init__(self) -> None:
        for fname in ("opportunity_identity", "run_id", "org_id", "pack_id",
                      "pack_version", "detector_id"):
            val = getattr(self, fname)
            if not val or not str(val).strip():
                raise ValueError(f"{fname} is required on an opportunity instance")

    def to_db_row(self) -> dict[str, Any]:
        """Map to a column→value dict ready for parameterised INSERT.

        ``evidence_ids`` and ``metadata`` are JSON-encoded into their TEXT
        columns; ``created_at`` is stored as an ISO-8601 UTC string.
        """
        return {
            "opportunity_identity": self.opportunity_identity,
            "run_id": self.run_id,
            "org_id": self.org_id,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "detector_id": self.detector_id,
            "signal_source": self.signal_source,
            "opportunity_ref": self.opportunity_ref,
            "impact": self.impact,
            "effort": self.effort,
            "score": self.score,
            "confidence": self.confidence,
            "tier": self.tier,
            "evidence_ids": json.dumps(list(self.evidence_ids or [])),
            "evidence_count": int(self.evidence_count or 0),
            "narrative": self.narrative,
            "metadata": json.dumps(self.metadata) if self.metadata is not None else None,
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime) else self.created_at,
            "is_deleted": bool(self.is_deleted),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "OpportunityInstance":
        """Rebuild an instance from a stored row (DictCursor or plain dict)."""
        evidence_ids = row.get("evidence_ids")
        if isinstance(evidence_ids, str):
            evidence_ids = json.loads(evidence_ids) if evidence_ids else []
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else None
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            opportunity_identity=row["opportunity_identity"],
            run_id=row["run_id"],
            org_id=row["org_id"],
            pack_id=row["pack_id"],
            pack_version=row["pack_version"],
            detector_id=row["detector_id"],
            signal_source=row.get("signal_source"),
            opportunity_ref=row.get("opportunity_ref"),
            impact=row.get("impact"),
            effort=row.get("effort"),
            score=row.get("score"),
            confidence=row.get("confidence"),
            tier=row.get("tier"),
            evidence_ids=list(evidence_ids or []),
            evidence_count=int(row.get("evidence_count") or 0),
            narrative=row.get("narrative"),
            metadata=metadata,
            created_at=created_at or datetime.now(timezone.utc),
            is_deleted=bool(row.get("is_deleted", False)),
        )
