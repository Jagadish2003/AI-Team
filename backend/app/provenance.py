"""Evidence & Identity Spine — provenance pointer (R16-B1, Part One / T1).

Every graph node (entity), every graph edge (relationship), and every enrichment
artifact carries an EvidencePointer back to where it came from. The pointer has a
MANDATORY SPINE that must be populated from day one (release 1.6) and EXTENSIBLE
DETAIL fields that later modules (retrieval in 1.8, the detector layer) fill in —
present-but-null now, so they can be populated without a schema change (AC8).

The load-bearing field is ``origin``: 'observed' (seen directly in a source) vs
'inferred' (produced by a model or a heuristic). Inferred content MUST name the
job that produced it (``extraction_job_id``), so model/heuristic output can never
silently masquerade as directly observed truth. This is the provenance-side
enforcement of the "LLM proposes, never authors truth" rule (AC2).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

OBSERVED = "observed"
INFERRED = "inferred"


def utc_now_iso() -> str:
    """UTC ISO-8601 timestamp — the default 'observed at' moment."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EvidencePointer:
    # ---- MANDATORY SPINE (populated now, in 1.6) ----
    source_system: str            # 'salesforce', 'jira', 'servicenow', 'agentiq', ...
    source_artifact: str          # stable id of the source record/message/doc
    source_timestamp: str         # when the source artifact was observed (UTC ISO-8601)
    origin: str                   # 'observed' | 'inferred'  <-- load-bearing
    extraction_job_id: Optional[str] = None  # REQUIRED when origin == 'inferred'

    # ---- EXTENSIBLE DETAIL (fill in as modules land; null in 1.6) ----
    chunk_id: Optional[str] = None             # set by retrieval (1.8)
    retrieval_result_id: Optional[str] = None  # set by retrieval (1.8)
    detector_evidence_id: Optional[str] = None  # set by the detector layer later
    confidence: Optional[float] = None

    def is_valid(self) -> bool:
        """True iff the mandatory spine is populated and the inferred-job rule holds."""
        if not (self.source_system and self.source_artifact
                and self.source_timestamp and self.origin):
            return False
        if self.origin not in (OBSERVED, INFERRED):
            return False
        if self.origin == INFERRED and not self.extraction_job_id:
            return False   # inferred content MUST name the job that produced it
        return True

    def to_dict(self) -> dict:
        """JSON-serialisable mapping for storage in an artifact's metadata blob.

        Always includes the extensible fields (null in 1.6) so retrieval can fill
        them in 1.8 without a schema change (AC8).
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidencePointer":
        """Rebuild a pointer from stored JSON (ignores unknown keys)."""
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})

    @classmethod
    def observed(
        cls,
        *,
        source_system: str,
        source_artifact: str,
        source_timestamp: Optional[str] = None,
        confidence: Optional[float] = None,
        detector_evidence_id: Optional[str] = None,
    ) -> "EvidencePointer":
        """Build an OBSERVED pointer — knowledge seen directly in a source.

        Observed artifacts need no extraction_job_id and validate without one.
        """
        return cls(
            source_system=source_system,
            source_artifact=source_artifact,
            source_timestamp=source_timestamp or utc_now_iso(),
            origin=OBSERVED,
            confidence=confidence,
            detector_evidence_id=detector_evidence_id,
        )

    @classmethod
    def inferred(
        cls,
        *,
        source_system: str,
        source_artifact: str,
        extraction_job_id: Optional[str],
        source_timestamp: Optional[str] = None,
        confidence: Optional[float] = None,
        detector_evidence_id: Optional[str] = None,
    ) -> "EvidencePointer":
        """Build an INFERRED pointer — model/heuristic output.

        ``extraction_job_id`` is mandatory: a missing/empty one yields an INVALID
        pointer (``is_valid()`` returns False) so the write path can refuse to
        persist it (AC2). Empty strings are normalised to None.
        """
        return cls(
            source_system=source_system,
            source_artifact=source_artifact,
            source_timestamp=source_timestamp or utc_now_iso(),
            origin=INFERRED,
            extraction_job_id=extraction_job_id or None,
            confidence=confidence,
            detector_evidence_id=detector_evidence_id,
        )
