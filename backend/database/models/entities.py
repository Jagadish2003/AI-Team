"""Entity model for Stage 2 Knowledge Graph (T3-S12-A).

Schema is locked after T3-S12-A merges. Downstream stories (T3-S13-A through
T3-S15-A) depend on these column names and types.

resolution_confidence and resolution_status are load-bearing for graph quality:
- T3-S13-A relationship mapper only draws edges from resolved entities.
- Ambiguous entities are retained as separate rows — never force-merged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


ENTITY_TYPES = frozenset({"person", "team", "project", "object", "process", "system"})
RESOLUTION_STATUSES = frozenset({"resolved", "ambiguous", "unresolved"})

CREATE_ENTITIES_TABLE = """
    CREATE TABLE IF NOT EXISTS entities (
        id                    VARCHAR(36)   NOT NULL PRIMARY KEY,
        org_id                VARCHAR(64)   NOT NULL,
        entity_type           VARCHAR(32)   NOT NULL,
        canonical_name        VARCHAR(256)  NOT NULL,
        display_name          VARCHAR(256)  NOT NULL,
        source_system         VARCHAR(64)   NOT NULL,
        source_record_id      VARCHAR(256),
        resolution_confidence FLOAT         NOT NULL,
        resolution_status     VARCHAR(32)   NOT NULL,
        first_seen_run_id     VARCHAR(64)   NOT NULL,
        last_seen_run_id      VARCHAR(64)   NOT NULL,
        run_count             INTEGER       NOT NULL,
        metadata              TEXT,
        created_at            TIMESTAMP     NOT NULL,
        updated_at            TIMESTAMP     NOT NULL
    )
"""


@dataclass
class Entity:
    org_id: str
    entity_type: str
    canonical_name: str
    display_name: str
    source_system: str
    resolution_confidence: float
    resolution_status: str
    first_seen_run_id: str
    last_seen_run_id: str
    id: UUID = field(default_factory=uuid4)
    source_record_id: Optional[str] = None
    run_count: int = 1
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if isinstance(self.id, str):
            self.id = UUID(self.id)
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {sorted(ENTITY_TYPES)}")
        if self.resolution_status not in RESOLUTION_STATUSES:
            raise ValueError(f"resolution_status must be one of {sorted(RESOLUTION_STATUSES)}")
        if not (0.0 <= self.resolution_confidence <= 1.0):
            raise ValueError("resolution_confidence must be between 0.0 and 1.0")
        for fname in ("org_id", "canonical_name", "display_name", "source_system",
                      "first_seen_run_id", "last_seen_run_id"):
            val = getattr(self, fname)
            if not val or not val.strip():
                raise ValueError(f"{fname} is required")

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "org_id": self.org_id,
            "entity_type": self.entity_type,
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "resolution_confidence": self.resolution_confidence,
            "resolution_status": self.resolution_status,
            "first_seen_run_id": self.first_seen_run_id,
            "last_seen_run_id": self.last_seen_run_id,
            "run_count": self.run_count,
            "metadata": json.dumps(self.metadata) if self.metadata is not None else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "Entity":
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return cls(
            id=UUID(row["id"]),
            org_id=row["org_id"],
            entity_type=row["entity_type"],
            canonical_name=row["canonical_name"],
            display_name=row["display_name"],
            source_system=row["source_system"],
            source_record_id=row.get("source_record_id"),
            resolution_confidence=float(row["resolution_confidence"]),
            resolution_status=row["resolution_status"],
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            run_count=int(row["run_count"]),
            metadata=metadata,
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
            updated_at=datetime.fromisoformat(row["updated_at"]) if isinstance(row["updated_at"], str) else row["updated_at"],
        )
