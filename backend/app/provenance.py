"""Evidence & Identity Spine — the provenance pointer (R16-B1, Part One / T1).

Every important AgentIQ artifact — each graph node (entity), each graph edge
(relationship), and each enrichment narrative — carries an :class:`EvidencePointer`
that records *where it came from*. Provenance cannot be reconstructed after the run
that should have recorded it, so the pointer is written at the moment the artifact is
formed, not bolted on afterwards.

The pointer has two layers:

* a **MANDATORY SPINE** that must be populated from day one (release 1.6):
  ``source_system``, ``source_artifact``, ``source_timestamp`` and ``origin``; and
* **EXTENSIBLE DETAIL** fields (``chunk_id``, ``retrieval_result_id``,
  ``detector_evidence_id``, ``confidence``) that later modules fill in. They ship
  present-but-null in 1.6 so retrieval (1.8) and the detector layer can populate them
  without a schema migration (AC8).

The load-bearing field is ``origin``: ``'observed'`` (seen directly in a source) vs
``'inferred'`` (produced by a model or a heuristic). Inferred content MUST name the
job that produced it via ``extraction_job_id`` — an inferred pointer without one is
invalid and must not be persisted (AC2). This is the provenance-side enforcement of
the "LLM proposes, never authors truth" rule: model output can never silently
masquerade as directly observed fact.

This module is the single, reusable provenance contract. ``entity_extractor.py``,
``relationship_mapper.py`` and ``llm_enrichment.py`` should build pointers through it
rather than inventing their own source-tracking formats.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

# ---- origin values (the load-bearing observed-vs-inferred distinction) ----
OBSERVED = "observed"
INFERRED = "inferred"
VALID_ORIGINS = (OBSERVED, INFERRED)


def utc_now_iso() -> str:
    """UTC ISO-8601 timestamp — the default 'observed at' moment for a pointer."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EvidencePointer:
    """A provenance pointer from an artifact back to the source it came from.

    The mandatory spine (the first four fields) is required on every pointer in 1.6.
    ``extraction_job_id`` is conditionally required: it must be present whenever
    ``origin == 'inferred'``. The remaining fields are extensible detail — present
    but null in 1.6, ready for retrieval (1.8) and the detector layer to populate.
    """

    # ---- MANDATORY SPINE (populated now, in 1.6) ----
    source_system: str            # 'salesforce', 'jira', 'servicenow', 'agentiq', ...
    source_artifact: str          # stable id of the source record / message / doc
    source_timestamp: str         # when the source artifact was observed (UTC ISO-8601)
    origin: str                   # 'observed' | 'inferred'  <-- load-bearing
    extraction_job_id: Optional[str] = None  # REQUIRED when origin == 'inferred'

    # ---- EXTENSIBLE DETAIL (fill in as modules land; null in 1.6) ----
    chunk_id: Optional[str] = None              # set by retrieval (1.8)
    retrieval_result_id: Optional[str] = None   # set by retrieval (1.8)
    detector_evidence_id: Optional[str] = None  # set by the detector layer later
    confidence: Optional[float] = None
    # Stability guarantee of source_artifact: 'record_id' is a stable source
    # system id (resolves to the same record forever); 'canonical_name' is a
    # mutable, normalised name that can drift across resolution-algorithm
    # changes. Lets a consumer know whether the artifact can be looked up in the
    # source system. None when the producer has not declared it.
    source_artifact_type: Optional[str] = None

    def is_valid(self) -> bool:
        """True iff the mandatory spine is populated and the inferred-job rule holds.

        Enforces the two rules from Section 1:

        1. every mandatory spine field (``source_system``, ``source_artifact``,
           ``source_timestamp``, ``origin``) must be present; and
        2. an ``origin == 'inferred'`` pointer must carry an ``extraction_job_id``.

        Observed pointers validate without a job id.
        """
        if not (self.source_system and self.source_artifact
                and self.source_timestamp and self.origin):
            return False
        if self.origin not in VALID_ORIGINS:
            return False
        if self.origin == INFERRED and not self.extraction_job_id:
            return False  # inferred content MUST name the job that produced it
        return True

    def to_dict(self) -> dict:
        """JSON-serialisable mapping for storage in an artifact's metadata blob.

        Always includes the extensible fields (null in 1.6) so retrieval can fill
        them in 1.8 without a schema change (AC8).
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EvidencePointer":
        """Rebuild a pointer from stored JSON, ignoring any unknown keys."""
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
        source_artifact_type: Optional[str] = None,
    ) -> "EvidencePointer":
        """Build an OBSERVED pointer — knowledge seen directly in a source.

        Observed artifacts need no ``extraction_job_id`` and validate without one.
        ``source_timestamp`` defaults to now (UTC) when the caller does not supply it.
        ``source_artifact_type`` ('record_id' | 'canonical_name') records the
        stability guarantee of ``source_artifact`` for downstream consumers.
        """
        return cls(
            source_system=source_system,
            source_artifact=source_artifact,
            source_timestamp=source_timestamp or utc_now_iso(),
            origin=OBSERVED,
            confidence=confidence,
            detector_evidence_id=detector_evidence_id,
            source_artifact_type=source_artifact_type,
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
        """Build an INFERRED pointer — model or heuristic output.

        ``extraction_job_id`` is mandatory: a missing or empty one yields an INVALID
        pointer (``is_valid()`` returns False) so the write path can refuse to persist
        it (AC2). Empty strings are normalised to ``None`` so the rule cannot be
        defeated by a blank id.
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
