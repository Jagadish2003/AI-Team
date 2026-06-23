"""Causal hypotheses model for Stage 3 Causal Inference (T3-S16-A).

Schema is locked after T3-S16-A merges. Downstream stories depend on these
column names and types:
- T3-S17-A (intervention modelling) reads preliminary=False rows only.
- T3-S18-A (outcome tracking) validates hypotheses using gate_run_count.

Column renames or removals require coordinated updates across T3-S17-A and
T3-S18-A simultaneously.

falsifiability_condition is NOT NULL by design — it is the database-level
enforcement of the rule that a hypothesis without a falsifiability condition
must never be stored (Section 3 of the spec).

preliminary and preliminary_reason drive the amber 'analyst review required'
banner in T7 and T9:
- preliminary: never null.
- preliminary_reason: null only when all three quality gates pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


CREATE_CAUSAL_HYPOTHESES_TABLE = """
    CREATE TABLE IF NOT EXISTS causal_hypotheses (
        id                      VARCHAR(36)  NOT NULL PRIMARY KEY,
        org_id                  VARCHAR(64)  NOT NULL,
        opportunity_id          VARCHAR(64)  NOT NULL,
        run_id                  VARCHAR(64)  NOT NULL,
        cause_chain             TEXT         NOT NULL,
        evidence_links          TEXT         NOT NULL,
        temporal_support        TEXT,
        confidence              FLOAT        NOT NULL,
        inferred                BOOLEAN      NOT NULL,
        falsifiability_condition TEXT        NOT NULL,
        preliminary             BOOLEAN      NOT NULL,
        preliminary_reason      TEXT,
        gate_run_count          INTEGER      NOT NULL,
        generated_by            VARCHAR(32)  NOT NULL,
        created_at              TIMESTAMP    NOT NULL
    )
"""

# Supports T8 endpoint lookup and T7 enrichment-population query.
CREATE_CH_IDX_ORG_OPP = """
    CREATE INDEX IF NOT EXISTS idx_ch_org_opp
        ON causal_hypotheses (org_id, opportunity_id)
"""

# Partial index for gate-monitoring queries that find pending hypotheses
# without scanning confirmed rows. PostgreSQL BOOLEAN predicate (AT-288 Fix 1):
# `WHERE preliminary` instead of SQLite's integer `WHERE preliminary = 1`.
CREATE_CH_IDX_ORG_PRELIMINARY = """
    CREATE INDEX IF NOT EXISTS idx_ch_org_preliminary
        ON causal_hypotheses (org_id, preliminary)
        WHERE preliminary
"""

ALL_CAUSAL_HYPOTHESES_DDL: tuple[str, ...] = (
    CREATE_CAUSAL_HYPOTHESES_TABLE,
    CREATE_CH_IDX_ORG_OPP,
    CREATE_CH_IDX_ORG_PRELIMINARY,
)


@dataclass
class CausalHypothesis:
    org_id: str
    opportunity_id: str
    run_id: str
    cause_chain: list[str]
    evidence_links: list[str]
    confidence: float
    inferred: bool
    falsifiability_condition: str
    preliminary: bool
    gate_run_count: int
    generated_by: str
    id: UUID = field(default_factory=uuid4)
    temporal_support: Optional[dict[str, Any]] = None
    preliminary_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if isinstance(self.id, str):
            self.id = UUID(self.id)
        for fname in ("org_id", "opportunity_id", "run_id", "generated_by"):
            val = getattr(self, fname)
            if not val or not val.strip():
                raise ValueError(f"{fname} is required")
        if not self.falsifiability_condition or not self.falsifiability_condition.strip():
            raise ValueError("falsifiability_condition is required")
        if not isinstance(self.cause_chain, list) or len(self.cause_chain) == 0:
            raise ValueError("cause_chain must be a non-empty list")
        if len(self.cause_chain) > 5:
            raise ValueError("cause_chain must have at most 5 steps")
        if not isinstance(self.evidence_links, list):
            raise ValueError("evidence_links must be a list")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.gate_run_count < 0:
            raise ValueError("gate_run_count must be non-negative")

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "org_id": self.org_id,
            "opportunity_id": self.opportunity_id,
            "run_id": self.run_id,
            "cause_chain": json.dumps(self.cause_chain),
            "evidence_links": json.dumps(self.evidence_links),
            "temporal_support": json.dumps(self.temporal_support) if self.temporal_support is not None else None,
            "confidence": self.confidence,
            # PostgreSQL BOOLEAN columns — bind real bools, not 0/1.
            "inferred": bool(self.inferred),
            "falsifiability_condition": self.falsifiability_condition,
            "preliminary": bool(self.preliminary),
            "preliminary_reason": self.preliminary_reason,
            "gate_run_count": self.gate_run_count,
            "generated_by": self.generated_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "CausalHypothesis":
        cause_chain = row["cause_chain"]
        if isinstance(cause_chain, str):
            cause_chain = json.loads(cause_chain)
        evidence_links = row["evidence_links"]
        if isinstance(evidence_links, str):
            evidence_links = json.loads(evidence_links)
        temporal_support = row.get("temporal_support")
        if isinstance(temporal_support, str):
            temporal_support = json.loads(temporal_support)
        return cls(
            id=UUID(row["id"]),
            org_id=row["org_id"],
            opportunity_id=row["opportunity_id"],
            run_id=row["run_id"],
            cause_chain=cause_chain,
            evidence_links=evidence_links,
            temporal_support=temporal_support,
            confidence=float(row["confidence"]),
            inferred=bool(row["inferred"]),
            falsifiability_condition=row["falsifiability_condition"],
            preliminary=bool(row["preliminary"]),
            preliminary_reason=row.get("preliminary_reason"),
            gate_run_count=int(row["gate_run_count"]),
            generated_by=row["generated_by"],
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
        )
