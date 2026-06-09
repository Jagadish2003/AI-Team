"""Entity relationships model for Stage 2 Knowledge Graph (T3-S13-A).

Schema is locked after T3-S13-A merges. Downstream stories (T3-S14-A and
T3-S15-A) depend on these column names and types.

inferred and confidence are load-bearing columns:
- T3-S14-A uses inferred to filter every graph query.
- T3-S15-A uses inferred to distinguish observed facts from co-firing
  hypotheses in the LLM prompt.

Column renames or removals require coordinated updates in all downstream
stories simultaneously.

SQLite note: from_entity_id and to_entity_id declare foreign keys, but SQLite
does not enforce them unless each connection runs PRAGMA foreign_keys = ON.
Current graph queries INNER JOIN entities, so orphaned relationship rows are
not surfaced, but cleanup jobs should enable the PRAGMA before deleting
entities.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


RELATIONSHIP_TYPES = frozenset({"owns", "member_of", "escalates_to", "depends_on", "routes_to"})

# Confidence defaults per edge category. These are the only permitted values —
# never derive confidence from an arbitrary float in Sprint 13.
OBSERVED_CONFIDENCE = 0.9   # directly observed from source system fields
INFERRED_CONFIDENCE = 0.6   # inferred from detector co-firing

_RELATIONSHIP_TYPE_CHECK = ", ".join(f"'{t}'" for t in sorted(RELATIONSHIP_TYPES))

CREATE_ENTITY_RELATIONSHIPS_TABLE = f"""
    CREATE TABLE IF NOT EXISTS entity_relationships (
        id                  VARCHAR(36)  NOT NULL PRIMARY KEY,
        org_id              VARCHAR(64)  NOT NULL,
        from_entity_id      VARCHAR(36)  NOT NULL REFERENCES entities(id),
        to_entity_id        VARCHAR(36)  NOT NULL REFERENCES entities(id),
        relationship_type   VARCHAR(32)  NOT NULL CHECK (relationship_type IN ({_RELATIONSHIP_TYPE_CHECK})),
        confidence          FLOAT        NOT NULL,
        inferred            BOOLEAN      NOT NULL,
        evidence            TEXT,
        first_seen_run_id   VARCHAR(64)  NOT NULL,
        last_seen_run_id    VARCHAR(64)  NOT NULL,
        run_count           INTEGER      NOT NULL,
        created_at          TIMESTAMP    NOT NULL
    )
"""

CREATE_ER_IDX_ORG_FROM = """
    CREATE INDEX IF NOT EXISTS idx_er_org_from
        ON entity_relationships (org_id, from_entity_id)
"""

CREATE_ER_IDX_ORG_TO = """
    CREATE INDEX IF NOT EXISTS idx_er_org_to
        ON entity_relationships (org_id, to_entity_id)
"""

CREATE_ER_IDX_ORG_TYPE = """
    CREATE INDEX IF NOT EXISTS idx_er_org_type
        ON entity_relationships (org_id, relationship_type, inferred)
"""

ALL_ENTITY_RELATIONSHIPS_DDL: tuple[str, ...] = (
    CREATE_ENTITY_RELATIONSHIPS_TABLE,
    CREATE_ER_IDX_ORG_FROM,
    CREATE_ER_IDX_ORG_TO,
    CREATE_ER_IDX_ORG_TYPE,
)


@dataclass
class EntityRelationship:
    org_id: str
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    confidence: float
    inferred: bool
    first_seen_run_id: str
    last_seen_run_id: str
    id: UUID = field(default_factory=uuid4)
    evidence: Optional[dict[str, Any]] = None
    run_count: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if isinstance(self.id, str):
            self.id = UUID(self.id)
        if isinstance(self.from_entity_id, str):
            self.from_entity_id = UUID(self.from_entity_id)
        if isinstance(self.to_entity_id, str):
            self.to_entity_id = UUID(self.to_entity_id)
        if self.relationship_type not in RELATIONSHIP_TYPES:
            raise ValueError(f"relationship_type must be one of {sorted(RELATIONSHIP_TYPES)}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        for fname in ("org_id", "first_seen_run_id", "last_seen_run_id"):
            val = getattr(self, fname)
            if not val or not val.strip():
                raise ValueError(f"{fname} is required")

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "org_id": self.org_id,
            "from_entity_id": str(self.from_entity_id),
            "to_entity_id": str(self.to_entity_id),
            "relationship_type": self.relationship_type,
            "confidence": self.confidence,
            "inferred": int(self.inferred),
            "evidence": json.dumps(self.evidence) if self.evidence is not None else None,
            "first_seen_run_id": self.first_seen_run_id,
            "last_seen_run_id": self.last_seen_run_id,
            "run_count": self.run_count,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "EntityRelationship":
        evidence = row.get("evidence")
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        return cls(
            id=UUID(row["id"]),
            org_id=row["org_id"],
            from_entity_id=UUID(row["from_entity_id"]),
            to_entity_id=UUID(row["to_entity_id"]),
            relationship_type=row["relationship_type"],
            confidence=float(row["confidence"]),
            inferred=bool(row["inferred"]),
            evidence=evidence,
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            run_count=int(row["run_count"]),
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
        )
